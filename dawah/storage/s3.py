"""S3-compatible adapter, configured for Cloudflare R2.

* Private bucket: browser-writable *staging* keys (via short-lived signed PUT
  with a signed Content-Type) and server-only validated keys.
* Public bucket: versioned delivery keys, served from ``R2_PUBLIC_DOMAIN``
  (an organization custom domain; never the S3 API host or r2.dev).

Presigned URLs use the S3 API hostname. No object ACLs are used (R2 does not
support them); bucket privacy/public access is bucket configuration.
Errors are mapped to secret-free :mod:`dawah.storage` exceptions; botocore
exceptions (which may contain URLs) never propagate to callers or logs.
"""
from __future__ import annotations

from urllib.parse import quote, urlsplit

from django.conf import settings

from . import (
    PRIVATE, PUBLIC, ObjectInfo, ObjectNotFound, ObjectStorage, PreconditionFailed, StorageError,
    StorageUnavailable, normalize_etag,
)

NOT_FOUND_CODES = {'404', 'NoSuchKey', 'NotFound'}
PRECONDITION_CODES = {'412', 'PreconditionFailed'}
# Wrong bucket/credentials/permissions: configuration, not a missing object.
MISCONFIGURED_CODES = {'NoSuchBucket', 'AccessDenied', '403', 'InvalidAccessKeyId', 'SignatureDoesNotMatch',
                       'InvalidBucketName', 'AllAccessDisabled'}


def _https_url(value):
    parts = urlsplit((value or '').strip())
    if parts.scheme != 'https' or not parts.netloc or parts.username or parts.password:
        return ''
    return value.strip().rstrip('/')


def _public_base(value, *, require_https, allow_r2_dev=False):
    value = (value or '').strip().rstrip('/')
    if not value:
        return ''
    if '://' not in value:
        value = f'https://{value}'
    parts = urlsplit(value)
    if not parts.netloc or parts.query or parts.fragment or parts.username or parts.password:
        return ''
    if require_https and parts.scheme != 'https':
        return ''
    host = (parts.hostname or '').lower()
    if not allow_r2_dev and (host == 'r2.dev' or host.endswith('.r2.dev')):
        return ''  # production delivery uses the organization custom domain
    return value


class S3ObjectStorage(ObjectStorage):
    def __init__(self, *, endpoint_url, access_key_id, secret_access_key, private_bucket, public_bucket,
                 public_base_url, region_name='auto', connect_timeout=5, read_timeout=60, max_attempts=3):
        import boto3
        from botocore.config import Config

        self.buckets = {PRIVATE: private_bucket, PUBLIC: public_bucket}
        self.public_base_url = public_base_url.rstrip('/')
        config = Config(
            signature_version='s3v4',
            s3={'addressing_style': 'path'},
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={'max_attempts': max_attempts, 'mode': 'standard'},
            # R2 compatibility: only send/verify checksums the operation requires.
            request_checksum_calculation='when_required',
            response_checksum_validation='when_required',
        )
        self.client = boto3.session.Session().client(
            's3', endpoint_url=endpoint_url, aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key, region_name=region_name, config=config)

    @classmethod
    def from_settings(cls):
        """Adapter from the R2_* settings, or ``None`` when they are incomplete."""
        endpoint = _https_url(settings.R2_S3_ENDPOINT_URL)
        if not settings.R2_S3_ENDPOINT_URL.strip() and settings.R2_ACCOUNT_ID.strip():
            endpoint = f'https://{settings.R2_ACCOUNT_ID.strip()}.r2.cloudflarestorage.com'
        allow_r2_dev = bool(settings.DEBUG and getattr(settings, 'R2_ALLOW_R2_DEV_DOMAIN', False))
        public_base = _public_base(settings.R2_PUBLIC_DOMAIN, require_https=True, allow_r2_dev=allow_r2_dev)
        values = [endpoint, settings.R2_ACCESS_KEY_ID, settings.R2_SECRET_ACCESS_KEY,
                  settings.R2_PRIVATE_BUCKET, settings.R2_PUBLIC_BUCKET, public_base]
        if not all(v.strip() for v in values):
            return None
        if settings.R2_PRIVATE_BUCKET.strip() == settings.R2_PUBLIC_BUCKET.strip():
            return None  # private drafts must never share the public delivery bucket
        return cls(endpoint_url=endpoint, access_key_id=settings.R2_ACCESS_KEY_ID.strip(),
                   secret_access_key=settings.R2_SECRET_ACCESS_KEY.strip(),
                   private_bucket=settings.R2_PRIVATE_BUCKET.strip(),
                   public_bucket=settings.R2_PUBLIC_BUCKET.strip(), public_base_url=public_base)

    # ------------------------------------------------------------ helpers

    def _bucket(self, bucket):
        return self.buckets[bucket]

    def _call(self, method, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError, ConnectionError as BotoConnectionError

        try:
            return getattr(self.client, method)(**kwargs)
        except ClientError as exc:
            error = exc.response.get('Error', {})
            code = str(error.get('Code', ''))
            status = str(exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode', ''))
            if code in MISCONFIGURED_CODES or status == '403':
                raise StorageUnavailable('PROVIDER_MISCONFIGURED') from None
            if code in NOT_FOUND_CODES or status == '404':
                raise ObjectNotFound() from None
            if code in PRECONDITION_CODES or status == '412':
                raise PreconditionFailed() from None
            raise StorageError('PROVIDER_ERROR') from None
        except BotoConnectionError:
            raise StorageUnavailable('PROVIDER_UNAVAILABLE') from None
        except BotoCoreError:
            raise StorageError('PROVIDER_ERROR') from None

    # ------------------------------------------------------------ port

    def presign_put(self, key, *, content_type, content_length, expires_in):
        return self._call('generate_presigned_url', ClientMethod='put_object', ExpiresIn=expires_in,
                          Params={'Bucket': self._bucket(PRIVATE), 'Key': key, 'ContentType': content_type,
                                  'ContentLength': int(content_length)})

    def presign_get(self, key, *, expires_in, content_type, filename, disposition='inline'):
        return self._call('generate_presigned_url', ClientMethod='get_object', ExpiresIn=expires_in,
                          Params={'Bucket': self._bucket(PRIVATE), 'Key': key,
                                  'ResponseContentType': content_type,
                                  'ResponseContentDisposition': content_disposition(disposition, filename)})

    def head(self, key, *, bucket=PRIVATE):
        try:
            response = self._call('head_object', Bucket=self._bucket(bucket), Key=key)
        except ObjectNotFound:
            return None
        return ObjectInfo(size=int(response.get('ContentLength', 0)),
                          etag=normalize_etag(response.get('ETag')),
                          content_type=response.get('ContentType', '') or '')

    def copy_if_match(self, src_key, dst_key, *, etag, src_bucket=PRIVATE, dst_bucket=PRIVATE, content_type,
                      content_disposition=None, cache_control=None):
        params = {
            'Bucket': self._bucket(dst_bucket), 'Key': dst_key,
            'CopySource': {'Bucket': self._bucket(src_bucket), 'Key': src_key},
            'CopySourceIfMatch': f'"{normalize_etag(etag)}"',
            'MetadataDirective': 'REPLACE', 'ContentType': content_type,
        }
        if content_disposition:
            params['ContentDisposition'] = content_disposition
        if cache_control:
            params['CacheControl'] = cache_control
        self._call('copy_object', **params)
        info = self.head(dst_key, bucket=dst_bucket)
        if info is None:
            raise StorageError('COPY_NOT_VISIBLE')
        return info

    def put_object(self, key, data, *, content_type, bucket=PRIVATE):
        self._call('put_object', Bucket=self._bucket(bucket), Key=key, Body=bytes(data), ContentType=content_type,
                   ContentLength=len(data))
        info = self.head(key, bucket=bucket)
        if info is None:
            raise StorageError('PUT_NOT_VISIBLE')
        return info

    def open_stream(self, key, *, bucket=PRIVATE, byte_range=None, if_match=None):
        params = {'Bucket': self._bucket(bucket), 'Key': key}
        if if_match:
            params['IfMatch'] = f'"{normalize_etag(if_match)}"'
        if byte_range is not None:
            params['Range'] = f'bytes={byte_range[0]}-{byte_range[1]}'
        return _SafeStream(self._call('get_object', **params)['Body'])

    def delete(self, key, *, bucket):
        try:
            self._call('delete_object', Bucket=self._bucket(bucket), Key=key)
        except ObjectNotFound:
            pass

    def public_url(self, key):
        return f'{self.public_base_url}/{quote(key, safe="/")}'


class _SafeStream:
    """Maps read failures to StorageError so provider details never leak."""

    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        try:
            return self.body.read(size)
        except Exception:
            raise StorageUnavailable('PROVIDER_READ_FAILED') from None

    def close(self):
        try:
            self.body.close()
        except Exception:
            pass


def content_disposition(disposition, filename):
    """``inline``/``attachment`` with an ASCII-safe quoted filename (callers pass sanitized names)."""
    if disposition not in ('inline', 'attachment'):
        raise ValueError('unsupported disposition')
    safe = ''.join(ch for ch in filename if ch.isascii() and (ch.isalnum() or ch in '._-')) or 'file'
    return f'{disposition}; filename="{safe}"'


def inline_disposition(filename):
    return content_disposition('inline', filename)
