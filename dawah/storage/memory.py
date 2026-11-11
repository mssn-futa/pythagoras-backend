"""In-memory adapter (test double) with the port's observable semantics.

Signed URLs are opaque fake capabilities on a non-resolvable host. Helpers
named ``client_*`` simulate what a browser/public reader can do with them.
"""
from __future__ import annotations

import hashlib
import io
import secrets
import threading
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import (
    BUCKETS, PRIVATE, PUBLIC, ObjectInfo, ObjectNotFound, ObjectStorage, PreconditionFailed, normalize_etag,
)

FAKE_HOST = 'fake-s3.invalid'


class InMemoryObjectStorage(ObjectStorage):
    def __init__(self, public_base_url='https://media.example.test'):
        self.public_base_url = public_base_url.rstrip('/')
        self.objects = {bucket: {} for bucket in BUCKETS}
        self.signed_puts = {}  # token -> (key, content_type)
        self.lock = threading.Lock()
        self.calls = []
        self.fail = {}  # method name -> exception to raise once per call

    # ------------------------------------------------------------ internals

    def _record(self, method, **kwargs):
        self.calls.append((method, kwargs))
        error = self.fail.get(method)
        if error is not None:
            raise error

    def _store(self, bucket, key, data, content_type, **meta):
        info = {'data': bytes(data), 'content_type': content_type,
                'etag': hashlib.md5(data, usedforsecurity=False).hexdigest(), **meta}
        with self.lock:
            self.objects[bucket][key] = info
        return info

    # ------------------------------------------------------------ port

    def presign_put(self, key, *, content_type, content_length, expires_in):
        self._record('presign_put', key=key)
        token = secrets.token_hex(16)
        self.signed_puts[token] = (key, content_type, content_length)
        return (f'https://{FAKE_HOST}/private/{quote(key)}?X-Amz-Expires={expires_in}'
                f'&X-Amz-SignedHeaders=content-length%3Bcontent-type%3Bhost&X-Amz-Signature={token}')

    def presign_get(self, key, *, expires_in, content_type, filename, disposition='inline'):
        self._record('presign_get', key=key, content_type=content_type, disposition=disposition)
        return (f'https://{FAKE_HOST}/private/{quote(key)}?X-Amz-Expires={expires_in}'
                f'&X-Amz-Signature={secrets.token_hex(16)}')

    def head(self, key, *, bucket=PRIVATE):
        self._record('head', key=key, bucket=bucket)
        info = self.objects[bucket].get(key)
        if info is None:
            return None
        return ObjectInfo(size=len(info['data']), etag=info['etag'], content_type=info['content_type'])

    def copy_if_match(self, src_key, dst_key, *, etag, src_bucket=PRIVATE, dst_bucket=PRIVATE, content_type,
                      content_disposition=None, cache_control=None):
        self._record('copy_if_match', src_key=src_key, dst_key=dst_key, dst_bucket=dst_bucket)
        with self.lock:
            source = self.objects[src_bucket].get(src_key)
            if source is None:
                raise ObjectNotFound()
            if source['etag'] != normalize_etag(etag):
                raise PreconditionFailed()
            data = source['data']
        self._store(dst_bucket, dst_key, data, content_type, content_disposition=content_disposition,
                    cache_control=cache_control)
        return self.head(dst_key, bucket=dst_bucket)

    def put_object(self, key, data, *, content_type, bucket=PRIVATE):
        self._record('put_object', key=key, bucket=bucket)
        self._store(bucket, key, data, content_type)
        return self.head(key, bucket=bucket)

    def open_stream(self, key, *, bucket=PRIVATE, byte_range=None, if_match=None):
        self._record('open_stream', key=key, bucket=bucket)
        info = self.objects[bucket].get(key)
        if info is None:
            raise ObjectNotFound()
        if if_match is not None and info['etag'] != normalize_etag(if_match):
            raise PreconditionFailed()
        data = info['data']
        if byte_range is not None:
            data = data[byte_range[0]:byte_range[1] + 1]
        return io.BytesIO(data)

    def delete(self, key, *, bucket):
        self._record('delete', key=key, bucket=bucket)
        with self.lock:
            self.objects[bucket].pop(key, None)

    def public_url(self, key):
        return f'{self.public_base_url}/{quote(key, safe="/")}'

    # ------------------------------------------------------------ simulation helpers

    def client_put(self, upload_url, data, *, content_type):
        """Browser PUT through a signed URL. Returns an HTTP-like status code."""
        parts = urlsplit(upload_url)
        token = parse_qs(parts.query).get('X-Amz-Signature', [''])[0]
        signed = self.signed_puts.get(token)
        if parts.netloc != FAKE_HOST or signed is None:
            return 403
        key, signed_type, signed_length = signed
        if unquote(parts.path) != f'/private/{key}':
            return 403  # the signature covers the exact key
        if content_type != signed_type or len(data) != signed_length:
            return 403  # Content-Type and Content-Length are signed headers
        self._store(PRIVATE, key, data, content_type)
        return 200

    def put_private(self, key, data, content_type='audio/mpeg'):
        return self._store(PRIVATE, key, data, content_type)

    def public_get(self, url, byte_range=None):
        """GET on the public delivery domain: (status, bytes, headers)."""
        prefix = f'{self.public_base_url}/'
        if not url.startswith(prefix):
            return 404, b'', {}
        info = self.objects[PUBLIC].get(unquote(url[len(prefix):]))
        if info is None:
            return 404, b'', {}
        data = info['data']
        headers = {'Content-Type': info['content_type'], 'Accept-Ranges': 'bytes',
                   'Content-Disposition': info.get('content_disposition'),
                   'Cache-Control': info.get('cache_control')}
        if byte_range is None:
            return 200, data, {**headers, 'Content-Length': str(len(data))}
        start, end = byte_range
        chunk = data[start:end + 1]
        return 206, chunk, {**headers, 'Content-Length': str(len(chunk)),
                            'Content-Range': f'bytes {start}-{start + len(chunk) - 1}/{len(data)}'}

    def keys(self, bucket):
        return set(self.objects[bucket])
