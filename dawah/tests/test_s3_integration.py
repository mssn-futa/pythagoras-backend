"""Opt-in live proof against an S3-compatible endpoint (R2 test account in RELEASE-07).

Skipped unless every DAWAH_S3_TEST_* variable is set. Uses throwaway buckets:
``DAWAH_S3_TEST_PUBLIC_BASE_URL`` must serve the public bucket anonymously
(R2: the bucket's custom domain; an emulator: its path-style bucket URL).
Never point it at production buckets.

    DAWAH_S3_TEST_ENDPOINT, DAWAH_S3_TEST_ACCESS_KEY, DAWAH_S3_TEST_SECRET_KEY,
    DAWAH_S3_TEST_PRIVATE_BUCKET, DAWAH_S3_TEST_PUBLIC_BUCKET,
    DAWAH_S3_TEST_PUBLIC_BASE_URL, optional DAWAH_S3_TEST_REGION (default auto)

``DAWAH_S3_TEST_EMULATOR=moto`` marks a run against moto's S3 emulator: the
cases moto does not implement are skipped with a reason *only* then. Against
R2 every assertion runs. An emulator run is not provider proof.
"""
import os
import secrets
import unittest
import urllib.error
import urllib.request

from django.test import SimpleTestCase, TestCase

from dawah import storage as storage_module
from dawah.models import EventResource, UploadIntent
from dawah.storage import PRIVATE, PUBLIC, PreconditionFailed
from dawah.storage.s3 import S3ObjectStorage

from .helpers import PUBLISHED, DawahAPITestCase, make_event, make_resource, mp3_bytes

NAMES = ['ENDPOINT', 'ACCESS_KEY', 'SECRET_KEY', 'PRIVATE_BUCKET', 'PUBLIC_BUCKET', 'PUBLIC_BASE_URL']
CONFIG = {name: os.getenv(f'DAWAH_S3_TEST_{name}', '') for name in NAMES}
EMULATOR = os.getenv('DAWAH_S3_TEST_EMULATOR', '').strip().lower()

# Behaviors moto's S3 emulator does not implement (verified against moto 5.2.5).
MOTO_LIMITS = {
    'signed_headers': 'moto does not verify presigned-URL signatures (default mode accepts any; its IAM '
                      'auth mode fails query-string auth with HTTP 500), so signed Content-Type/key binding '
                      'and unsigned-PUT refusal cannot be observed',
    'copy_if_match': 'moto ignores x-amz-copy-source-if-match (it implements only if-none-match), so a '
                     'stale-ETag copy is not refused; the adapter does send the header',
    'public_head': 'moto authorizes anonymous HEAD as a nonexistent s3:HeadObject action, so a public-read '
                   'policy granting s3:GetObject (sufficient on S3/R2) still returns 403 for HEAD',
}


def emulator_limit(name):
    if EMULATOR == 'moto' and name in MOTO_LIMITS:
        raise unittest.SkipTest(f'emulator limit (moto): {MOTO_LIMITS[name]}')


def http(method, url, data=None, headers=None):
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, response.read(), response.headers  # case-insensitive .get()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def live_adapter():
    return S3ObjectStorage(
        endpoint_url=CONFIG['ENDPOINT'], access_key_id=CONFIG['ACCESS_KEY'],
        secret_access_key=CONFIG['SECRET_KEY'], private_bucket=CONFIG['PRIVATE_BUCKET'],
        public_bucket=CONFIG['PUBLIC_BUCKET'], public_base_url=CONFIG['PUBLIC_BASE_URL'],
        region_name=os.getenv('DAWAH_S3_TEST_REGION', 'auto'))


LIVE = unittest.skipUnless(all(CONFIG.values()), 'DAWAH_S3_TEST_* not configured (live S3-compatible proof is opt-in)')


@LIVE
class LiveApiFlowTests(DawahAPITestCase, TestCase):
    """intent → direct PUT → finalize → preview → publish → Range → unpublish, via the real adapter."""

    def setUp(self):
        super().setUp()
        self.live = live_adapter()
        storage_module.set_storage(self.live)  # replaces the fake installed by the mixin
        self.as_admin()

    def test_end_to_end(self):
        event = make_event(status=PUBLISHED)
        resource = make_resource(event)
        data = mp3_bytes()
        base = f'/dawah/events/{event.pk}/resources/{resource.pk}'
        intent = self.data(self.client.post(f'{base}/audio/', {'file_name': 'k.mp3', 'content_type': 'audio/mpeg',
                                                               'size_bytes': len(data)}, format='json'), 201)
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.addCleanup(self.live.delete, stored.staging_key, bucket=PRIVATE)
        self.addCleanup(self.live.delete, stored.validation_key, bucket=PRIVATE)
        status, body, _ = http('PUT', intent['upload_url'], data, intent['headers'])
        self.assertEqual(status, 200, body[:300])

        with self.captureOnCommitCallbacks(execute=True):
            finalized = self.data(self.client.post(f'/dawah/uploads/{intent["upload_id"]}/finalize/'))
        self.assertEqual((finalized['upload']['status'], finalized['resource']['storage_status'],
                          finalized['resource']['duration']), ('ready', 'ready', 5))
        status, body, _ = http('GET', finalized['preview_url'])
        self.assertEqual((status, body), (200, data))
        self.assertIsNone(self.live.head(stored.staging_key))  # consumed staging object was cleaned up

        with self.captureOnCommitCallbacks(execute=True):
            published = self.data(self.client.post(f'{base}/publish/'))
        public_key = EventResource.objects.get(pk=resource.pk).public_object_key
        self.addCleanup(self.live.delete, public_key, bucket=PUBLIC)
        self.assertEqual(published['media_url'], self.live.public_url(public_key))
        status, body, headers = http('GET', published['media_url'], headers={'Range': 'bytes=10-19'})
        self.assertEqual((status, body, headers.get('Content-Type')), (206, data[10:20], 'audio/mpeg'))

        with self.captureOnCommitCallbacks(execute=True):
            self.data(self.client.post(f'{base}/unpublish/'))
        self.assertIsNone(self.live.head(public_key, bucket=PUBLIC))
        self.assertNotEqual(http('GET', published['media_url'])[0], 200)


@LIVE
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
        self.url = self.storage.presign_put(self.staging, content_type='audio/mpeg', content_length=len(self.data),
                                            expires_in=900)
        self.tampered = self.data[:-8] + b'tampered'  # same length: the signed Content-Length matches

    def put_staged(self):
        status, body, _ = http('PUT', self.url, self.data, {'Content-Type': 'audio/mpeg'})
        self.assertEqual(status, 200, body[:300])
        return self.storage.head(self.staging)

    def test_presigned_put_with_content_type_lands_server_observable_object(self):
        staged = self.put_staged()
        self.assertEqual((staged.size, staged.content_type), (len(self.data), 'audio/mpeg'))
        self.assertTrue(staged.etag)

    def test_signed_content_type_is_enforced(self):
        emulator_limit('signed_headers')
        self.assertEqual(http('PUT', self.url, self.data, {'Content-Type': 'text/html'})[0], 403)
        self.assertIsNone(self.storage.head(self.staging))

    def test_signed_content_length_is_enforced(self):
        emulator_limit('signed_headers')
        self.assertEqual(http('PUT', self.url, self.data + b'x', {'Content-Type': 'audio/mpeg'})[0], 403)
        self.assertIsNone(self.storage.head(self.staging))

    def test_signed_put_cannot_target_validation_or_public_keys(self):
        emulator_limit('signed_headers')
        swapped = self.url.replace(self.staging, self.validation)
        self.assertEqual(http('PUT', swapped, self.tampered, {'Content-Type': 'audio/mpeg'})[0], 403)
        unsigned = self.url.split('?', 1)[0].replace(self.staging, self.validation)
        self.assertIn(http('PUT', unsigned, self.tampered, {'Content-Type': 'audio/mpeg'})[0], (400, 401, 403))
        self.assertIsNone(self.storage.head(self.validation))

    def test_copy_at_observed_etag(self):
        staged = self.put_staged()
        copied = self.storage.copy_if_match(self.staging, self.validation, etag=staged.etag,
                                            content_type='audio/mpeg')
        self.assertEqual((copied.size, copied.content_type), (len(self.data), 'audio/mpeg'))

    def test_conditional_copy_refuses_stale_etag(self):
        emulator_limit('copy_if_match')
        staged = self.put_staged()
        with self.assertRaises(PreconditionFailed):
            self.storage.copy_if_match(self.staging, self.validation, etag='0' * 32, content_type='audio/mpeg')
        self.assertIsNone(self.storage.head(self.validation))
        # After the still-valid PUT URL overwrites staging, the old observed ETag no longer copies.
        self.assertEqual(http('PUT', self.url, self.tampered, {'Content-Type': 'audio/mpeg'})[0], 200)
        with self.assertRaises(PreconditionFailed):
            self.storage.copy_if_match(self.staging, self.validation, etag=staged.etag, content_type='audio/mpeg')

    def test_staging_overwrite_does_not_change_validated_copy(self):
        staged = self.put_staged()
        self.storage.copy_if_match(self.staging, self.validation, etag=staged.etag, content_type='audio/mpeg')
        self.assertEqual(http('PUT', self.url, self.tampered, {'Content-Type': 'audio/mpeg'})[0], 200)
        self.assertNotEqual(self.storage.head(self.staging).etag, staged.etag)
        stream = self.storage.open_stream(self.validation)
        self.assertEqual(stream.read(), self.data)
        stream.close()

    def test_promotion_and_public_range_delivery(self):
        staged = self.put_staged()
        copied = self.storage.copy_if_match(self.staging, self.validation, etag=staged.etag,
                                            content_type='audio/mpeg')
        promoted = self.storage.copy_if_match(
            self.validation, self.public, etag=copied.etag, dst_bucket=PUBLIC, content_type='audio/mpeg',
            content_disposition='inline; filename="k.mp3"', cache_control='public, max-age=31536000, immutable')
        self.assertEqual(promoted.size, len(self.data))
        public_url = self.storage.public_url(self.public)
        status, body, headers = http('GET', public_url, headers={'Range': 'bytes=0-99'})
        self.assertEqual((status, body), (206, self.data[:100]))
        self.assertEqual(headers.get('Content-Range'), f'bytes 0-99/{len(self.data)}')
        self.assertEqual(headers.get('Content-Type'), 'audio/mpeg')
        self.assertEqual(headers.get('Content-Disposition'), 'inline; filename="k.mp3"')
        self.assertEqual(headers.get('Cache-Control'), 'public, max-age=31536000, immutable')
        # Adapter range read on the same object.
        stream = self.storage.open_stream(self.public, bucket=PUBLIC, byte_range=(100, 199))
        self.assertEqual(stream.read(), self.data[100:200])
        stream.close()

    def test_public_head(self):
        emulator_limit('public_head')
        staged = self.put_staged()
        copied = self.storage.copy_if_match(self.staging, self.validation, etag=staged.etag,
                                            content_type='audio/mpeg')
        self.storage.copy_if_match(self.validation, self.public, etag=copied.etag, dst_bucket=PUBLIC,
                                   content_type='audio/mpeg')
        status, _, headers = http('HEAD', self.storage.public_url(self.public))
        self.assertEqual((status, headers.get('Content-Length'), headers.get('Content-Type')),
                         (200, str(len(self.data)), 'audio/mpeg'))

    def test_private_bucket_is_not_anonymously_readable(self):
        staged = self.put_staged()
        self.storage.copy_if_match(self.staging, self.validation, etag=staged.etag, content_type='audio/mpeg')
        private_url = self.url.split('?', 1)[0].replace(self.staging, self.validation)
        self.assertIn(http('GET', private_url)[0], (400, 401, 403))

    def test_preview_url_reads_private_object(self):
        staged = self.put_staged()
        self.storage.copy_if_match(self.staging, self.validation, etag=staged.etag, content_type='audio/mpeg')
        url = self.storage.presign_get(self.validation, expires_in=300, content_type='audio/mpeg', filename='k.mp3')
        status, body, headers = http('GET', url, headers={'Range': 'bytes=0-9'})
        self.assertEqual((status, body), (206, self.data[:10]))
        self.assertEqual(headers.get('Content-Type'), 'audio/mpeg')
