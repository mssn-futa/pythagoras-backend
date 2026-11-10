"""Opt-in live proof against an S3-compatible endpoint (MinIO locally; R2 in RELEASE-07).

Skipped unless every DAWAH_S3_TEST_* variable is set. Uses throwaway buckets:
``DAWAH_S3_TEST_PUBLIC_BASE_URL`` must serve the public bucket anonymously
(MinIO: ``http://127.0.0.1:19000/<public-bucket>`` with an anonymous download
policy; R2: the bucket's custom domain). Never point it at production buckets.

    DAWAH_S3_TEST_ENDPOINT, DAWAH_S3_TEST_ACCESS_KEY, DAWAH_S3_TEST_SECRET_KEY,
    DAWAH_S3_TEST_PRIVATE_BUCKET, DAWAH_S3_TEST_PUBLIC_BUCKET,
    DAWAH_S3_TEST_PUBLIC_BASE_URL, optional DAWAH_S3_TEST_REGION (default auto)
"""
import os
import secrets
import unittest
import urllib.error
import urllib.request

from django.test import SimpleTestCase

from dawah.storage import PRIVATE, PUBLIC, PreconditionFailed
from dawah.storage.s3 import S3ObjectStorage

from .helpers import mp3_bytes

NAMES = ['ENDPOINT', 'ACCESS_KEY', 'SECRET_KEY', 'PRIVATE_BUCKET', 'PUBLIC_BUCKET', 'PUBLIC_BASE_URL']
CONFIG = {name: os.getenv(f'DAWAH_S3_TEST_{name}', '') for name in NAMES}


def http(method, url, data=None, headers=None):
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


@unittest.skipUnless(all(CONFIG.values()), 'DAWAH_S3_TEST_* not configured (live S3-compatible proof is opt-in)')
class LiveS3CompatibleTests(SimpleTestCase):
    def setUp(self):
        self.storage = S3ObjectStorage(
            endpoint_url=CONFIG['ENDPOINT'], access_key_id=CONFIG['ACCESS_KEY'],
            secret_access_key=CONFIG['SECRET_KEY'], private_bucket=CONFIG['PRIVATE_BUCKET'],
            public_bucket=CONFIG['PUBLIC_BUCKET'], public_base_url=CONFIG['PUBLIC_BASE_URL'],
            region_name=os.getenv('DAWAH_S3_TEST_REGION', 'auto'))
        run = secrets.token_hex(8)
        self.staging = f'staging/audio/it-{run}'
        self.validation = f'media/audio/it-{run}.mp3'
        self.public = f'audio/it-{run}/k.mp3'
        self.data = mp3_bytes()
        for bucket, key in ((PRIVATE, self.staging), (PRIVATE, self.validation), (PUBLIC, self.public)):
            self.addCleanup(self.storage.delete, key, bucket=bucket)

    def test_upload_validate_promote_and_range_delivery(self):
        url = self.storage.presign_put(self.staging, content_type='audio/mpeg', expires_in=900)
        # Signed Content-Type: a different type is refused; no Authorization header is sent.
        self.assertEqual(http('PUT', url, self.data, {'Content-Type': 'text/html'})[0], 403)
        self.assertEqual(http('PUT', url, self.data, {'Content-Type': 'audio/mpeg'})[0], 200)
        staged = self.storage.head(self.staging)
        self.assertEqual((staged.size, staged.content_type), (len(self.data), 'audio/mpeg'))

        # The signature is bound to the staging key: final/validation/public keys cannot be written.
        swapped = url.replace(self.staging, self.validation)
        self.assertEqual(http('PUT', swapped, b'evil', {'Content-Type': 'audio/mpeg'})[0], 403)
        unsigned = url.split('?', 1)[0].replace(self.staging, self.validation)
        self.assertIn(http('PUT', unsigned, b'evil', {'Content-Type': 'audio/mpeg'})[0], (400, 401, 403))
        self.assertIsNone(self.storage.head(self.validation))

        # Conditional copy at the observed ETag; a stale ETag is refused.
        with self.assertRaises(PreconditionFailed):
            self.storage.copy_if_match(self.staging, self.validation, etag='0' * 32, content_type='audio/mpeg')
        copied = self.storage.copy_if_match(self.staging, self.validation, etag=staged.etag,
                                            content_type='audio/mpeg')
        self.assertEqual(copied.size, len(self.data))

        # Overwriting staging (the PUT URL is still valid) does not change the validated copy.
        self.assertEqual(http('PUT', url, b'tampered', {'Content-Type': 'audio/mpeg'})[0], 200)
        stream = self.storage.open_stream(self.validation)
        self.assertEqual(stream.read(), self.data)
        stream.close()

        # Promotion and public Range delivery.
        self.storage.copy_if_match(self.validation, self.public, etag=copied.etag, dst_bucket=PUBLIC,
                                   content_type='audio/mpeg', content_disposition='inline; filename="k.mp3"',
                                   cache_control='public, max-age=31536000, immutable')
        public_url = self.storage.public_url(self.public)
        status, body, headers = http('GET', public_url, headers={'Range': 'bytes=0-99'})
        self.assertEqual((status, body), (206, self.data[:100]))
        self.assertEqual(headers.get('Content-Range'), f'bytes 0-99/{len(self.data)}')
        self.assertEqual(headers.get('Content-Type'), 'audio/mpeg')
        status, _, headers = http('HEAD', public_url)
        self.assertEqual((status, headers.get('Content-Length')), (200, str(len(self.data))))
        # The private bucket is not anonymously readable.
        private_url = url.split('?', 1)[0].replace(self.staging, self.validation)
        self.assertIn(http('GET', private_url)[0], (400, 401, 403))
