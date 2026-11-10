"""Regression tests for the DAWAH-02 review findings (bomb, parser errors, leases, ...)."""
import io
import json
import random
import struct
import tracemalloc
import urllib.error
import zlib
from datetime import timedelta
from unittest import mock

from botocore.stub import Stubber
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from dawah import media, uploads
from dawah.models import EventResource, StorageCleanup, UploadIntent
from dawah.storage import PRIVATE, PreconditionFailed, StorageUnavailable
from dawah.storage.purge import CloudflarePurger, NoopPurger, PurgeError, purger_from_settings
from dawah.storage.s3 import S3ObjectStorage

from .helpers import User, mp3_bytes
from .test_storage_adapter import R2, adapter
from .test_uploads import MP3, UploadTestCase, finalize_url

BOMB_BYTES = 64 * 1024 * 1024


def syncsafe(n):
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def compressed_id3_bomb(expanded=BOMB_BYTES):
    """ID3v2.3 tag with one zlib-compressed TXXX frame that expands to ``expanded`` bytes."""
    payload = b'\x00bomb\x00' + b'A' * (expanded - 6)
    body = struct.pack('>I', len(payload)) + zlib.compress(payload, 9)
    frame = b'TXXX' + struct.pack('>I', len(body)) + b'\x00\x80' + body  # flag: compressed
    return b'ID3\x03\x00\x00' + syncsafe(len(frame)) + frame


class Mp3HardeningTests(SimpleTestCase):
    def test_compressed_id3_frame_is_never_decompressed(self):
        bomb = compressed_id3_bomb() + mp3_bytes()[10:]  # tag + valid audio frames
        self.assertLess(len(bomb), 1024 * 1024)  # the file is small...
        with mock.patch('mutagen.id3.ID3.__init__', side_effect=AssertionError('ID3 must not be parsed')):
            tracemalloc.start()
            try:
                info = media.validate_mp3(io.BytesIO(bomb), len(bomb))
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual(info.duration_seconds, 5)
        self.assertLess(peak, 8 * 1024 * 1024)  # ...and validation never expands it

    def test_oversized_id3_tag_is_rejected(self):
        header = b'ID3\x03\x00\x00' + syncsafe(media.ID3_MAX_BYTES + 1)
        data = header + b'\x00' * 100 + mp3_bytes()[10:]
        with self.assertRaises(media.MediaRejected) as caught:
            media.validate_mp3(io.BytesIO(data), len(data))
        self.assertEqual(caught.exception.code, 'INVALID_MP3')

    def test_unexpected_parser_error_is_a_rejection(self):
        with mock.patch('mutagen.mp3.MPEGInfo', side_effect=struct.error('unpack requires a buffer')):
            with self.assertRaises(media.MediaRejected) as caught:
                media.validate_mp3(io.BytesIO(MP3), len(MP3))
        self.assertEqual(caught.exception.code, 'INVALID_MP3')

    def test_mutated_inputs_only_validate_or_reject(self):
        rng = random.Random(20261110)
        samples = [MP3, mp3_bytes(frames=120)[10:], compressed_id3_bomb(4096) + MP3[10:]]
        for _ in range(300):
            data = bytearray(rng.choice(samples))
            for _ in range(rng.randint(1, 40)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            data = bytes(data[:rng.randint(1, len(data))])
            try:
                media.validate_mp3(io.BytesIO(data), len(data))
            except media.MediaRejected:
                pass


class FinalizeHardeningTests(UploadTestCase):
    def test_disk_full_during_spool_is_sanitized_503_and_retryable(self):
        intent = self.upload()

        class FullDisk(io.BytesIO):
            def write(self, chunk):
                raise OSError(28, 'No space left on device')

        with mock.patch('dawah.media.tempfile.SpooledTemporaryFile', lambda **kw: FullDisk()):
            data = self.error(self.finalize(intent['upload_id'], status=503), 503, 'PROVIDER_UNAVAILABLE')
        self.assertEqual(set(data), {'code'})
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.assertEqual((stored.status, stored.lease_token, stored.lease_expires_at), ('validating', '', None))
        self.assertTrue(stored.validation_etag)  # the immutable copy is kept for the retry
        self.assertEqual(self.finalize(intent['upload_id'])['upload']['status'], 'ready')

    def test_lease_expired_before_attach_is_not_attached(self):
        intent = self.upload()
        original = self.storage.open_stream

        def slow(key, **kwargs):
            UploadIntent.objects.filter(pk=intent['upload_id']).update(
                lease_expires_at=timezone.now() - timedelta(seconds=1))
            return original(key, **kwargs)

        with mock.patch.object(self.storage, 'open_stream', slow):
            self.error(self.finalize(intent['upload_id'], status=409), 409, 'UPLOAD_LEASE_EXPIRED')
        self.resource.refresh_from_db()
        self.assertEqual((self.resource.storage_status, self.resource.private_object_key), ('pending', ''))
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.assertEqual((stored.status, stored.lease_token), ('validating', ''))
        self.assertEqual(self.finalize(intent['upload_id'])['upload']['status'], 'ready')

    def test_validation_read_is_conditional_on_the_copy_etag(self):
        intent = self.upload()
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        original = self.storage.open_stream

        def swap_then_read(key, **kwargs):
            self.storage.put_private(stored.validation_key, mp3_bytes(frames=300))  # changed underneath
            return original(key, **kwargs)

        with mock.patch.object(self.storage, 'open_stream', swap_then_read):
            data = self.finalize(intent['upload_id'])
        self.assertEqual((data['upload']['status'], data['upload']['error_code']),
                         ('failed', 'VALIDATION_COPY_MISSING'))
        self.assertTrue(StorageCleanup.objects.filter(key=stored.validation_key, reason='upload_failed').exists())
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.private_object_key, '')  # nothing unverified was attached

    def test_out_of_range_duration_clears_previous_value(self):
        EventResource.objects.filter(pk=self.resource.pk).update(duration=100)
        intent = self.upload()
        long_audio = media.AudioInfo(duration_seconds=40000, bitrate=8000, sample_rate=8000)
        with mock.patch('dawah.uploads.media.validate_mp3', return_value=long_audio):
            data = self.finalize(intent['upload_id'])
        self.assertEqual((data['upload']['status'], data['resource']['duration']), ('ready', None))


class NullLeaseCleanupTests(UploadTestCase):
    def copy_then_fail(self):
        intent = self.upload()
        self.storage.fail['open_stream'] = StorageUnavailable('PROVIDER_UNAVAILABLE')
        self.error(self.finalize(intent['upload_id'], status=503), 503, 'PROVIDER_UNAVAILABLE')
        del self.storage.fail['open_stream']
        return UploadIntent.objects.get(pk=intent['upload_id'])

    def test_validating_intent_with_null_lease_is_protected_then_abandoned(self):
        stored = self.copy_then_fail()
        self.assertEqual((stored.status, stored.lease_expires_at), ('validating', None))
        self.assertIn(stored.validation_key, self.storage.keys(PRIVATE))
        # While unfinished, its validation copy is referenced and never deleted.
        uploads.record_cleanup(PRIVATE, stored.validation_key, 'test')
        self.assertEqual(uploads.process_cleanup()['skipped'], 1)
        self.assertIn(stored.validation_key, self.storage.keys(PRIVATE))
        # A day past expiry, the command abandons it and cleans up its keys.
        UploadIntent.objects.filter(pk=stored.pk).update(expires_at=timezone.now() - timedelta(days=2))
        self.assertEqual(uploads.expire_stale_intents(), {'expired': 0, 'abandoned': 1})
        stored.refresh_from_db()
        self.assertEqual((stored.status, stored.error_code), ('failed', 'VALIDATION_ABANDONED'))
        uploads.process_cleanup()
        self.assertNotIn(stored.validation_key, self.storage.keys(PRIVATE))
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.storage_status, 'failed')

    def test_legacy_null_lease_without_copy_expires(self):
        intent = self.upload()
        UploadIntent.objects.filter(pk=intent['upload_id']).update(
            status='validating', lease_token='', lease_expires_at=None,
            expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(uploads.expire_stale_intents(), {'expired': 1, 'abandoned': 0})


class AdminDeletionTests(UploadTestCase):
    def test_deleting_the_owner_keeps_the_intent_for_cleanup(self):
        owner = User.objects.create_user(email='owner@example.test', password='Owner-Pass-9876',
                                         first_name='O', last_name='Admin', role='admin')
        self.as_user(owner)
        intent = self.upload()
        owner.delete()
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.assertIsNone(stored.created_by_id)
        self.as_admin()
        self.error(self.client.post(finalize_url(stored.pk)), 404, 'NOT_FOUND')
        UploadIntent.objects.filter(pk=stored.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        uploads.expire_stale_intents()
        uploads.process_cleanup()
        self.assertNotIn(stored.staging_key, self.storage.keys(PRIVATE))
        self.assertTrue(StorageCleanup.objects.filter(key=stored.staging_key, status='done').exists())


class AdapterHardeningTests(SimpleTestCase):
    def setUp(self):
        self.storage = adapter()
        self.stub = Stubber(self.storage.client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)

    def test_presigned_put_signs_content_length(self):
        from urllib.parse import parse_qs, urlsplit
        url = self.storage.presign_put('staging/audio/t', content_type='audio/mpeg', content_length=1234,
                                       expires_in=900)
        self.assertEqual(parse_qs(urlsplit(url).query)['X-Amz-SignedHeaders'], ['content-length;content-type;host'])

    def test_config_errors_are_unavailable_not_missing(self):
        self.stub.add_client_error('head_object', service_error_code='403', http_status_code=403)
        self.stub.add_client_error('copy_object', service_error_code='NoSuchBucket', http_status_code=404)
        self.stub.add_client_error('delete_object', service_error_code='AccessDenied', http_status_code=403)
        with self.assertRaises(StorageUnavailable) as caught:
            self.storage.head('k')
        self.assertEqual(caught.exception.code, 'PROVIDER_MISCONFIGURED')
        with self.assertRaises(StorageUnavailable):
            self.storage.copy_if_match('a', 'b', etag='x', content_type='audio/mpeg')
        with self.assertRaises(StorageUnavailable):
            self.storage.delete('k', bucket=PRIVATE)

    def test_conditional_read_sends_if_match(self):
        self.stub.add_client_error('get_object', service_error_code='PreconditionFailed', http_status_code=412,
                                   expected_params={'Bucket': 'pyth-private', 'Key': 'k', 'IfMatch': '"abc"'})
        with self.assertRaises(PreconditionFailed):
            self.storage.open_stream('k', if_match='abc')


class SettingsHardeningTests(SimpleTestCase):
    def test_endpoint_must_be_https(self):
        for endpoint in ('http://acct.r2.cloudflarestorage.com', 'acct.r2.cloudflarestorage.com',
                         'https://u:p@acct.r2.cloudflarestorage.com'):
            with self.subTest(endpoint=endpoint), override_settings(**{**R2, 'R2_S3_ENDPOINT_URL': endpoint}):
                self.assertIsNone(S3ObjectStorage.from_settings())
        with override_settings(**{**R2, 'R2_S3_ENDPOINT_URL': 'https://acct.r2.cloudflarestorage.com'}):
            self.assertIsNotNone(S3ObjectStorage.from_settings())

    def test_r2_dev_public_domain_needs_debug_and_explicit_flag(self):
        dev = {**R2, 'R2_PUBLIC_DOMAIN': 'https://pub-abc.r2.dev'}
        for debug, flag, allowed in ((False, False, False), (False, True, False), (True, False, False),
                                     (True, True, True)):
            with self.subTest(debug=debug, flag=flag), override_settings(**dev, DEBUG=debug,
                                                                        R2_ALLOW_R2_DEV_DOMAIN=flag):
                self.assertEqual(S3ObjectStorage.from_settings() is not None, allowed)


class PurgerTests(SimpleTestCase):
    ZONE = '0123456789abcdef0123456789abcdef'

    def test_selection_from_settings(self):
        cases = [({'CLOUDFLARE_ZONE_ID': self.ZONE, 'CLOUDFLARE_PURGE_API_TOKEN': 'tok'}, CloudflarePurger),
                 ({'CLOUDFLARE_ZONE_ID': self.ZONE, 'CLOUDFLARE_PURGE_API_TOKEN': ''}, NoopPurger),
                 ({'CLOUDFLARE_ZONE_ID': '', 'CLOUDFLARE_PURGE_API_TOKEN': 'tok'}, NoopPurger),
                 ({'CLOUDFLARE_ZONE_ID': 'not-a-zone', 'CLOUDFLARE_PURGE_API_TOKEN': 'tok'}, NoopPurger)]
        for values, expected in cases:
            with self.subTest(values=values), override_settings(**values):
                self.assertIsInstance(purger_from_settings(), expected)

    def test_purge_by_url_batches_and_authenticates(self):
        requests = []

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout):
            requests.append((request.full_url, request.get_header('Authorization'), json.loads(request.data),
                             timeout))
            return Response(b'{"success": true}')

        urls = [f'https://media.example.org/audio/{i}/k.mp3' for i in range(31)]
        with mock.patch('dawah.storage.purge.urllib.request.urlopen', fake_urlopen):
            CloudflarePurger(self.ZONE, 'tok').purge(urls)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][0], f'https://api.cloudflare.com/client/v4/zones/{self.ZONE}/purge_cache')
        self.assertEqual(requests[0][1], 'Bearer tok')
        self.assertEqual([len(r[2]['files']) for r in requests], [30, 1])
        self.assertEqual(requests[0][3], 10)

    def test_failures_are_fixed_codes_without_secrets(self):
        def http_403(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, 'Forbidden tok', {}, io.BytesIO(b'tok'))

        class Rejected(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        for side_effect, code in ((http_403, 'PURGE_HTTP_403'),
                                  (lambda r, timeout: Rejected(b'{"success": false}'), 'PURGE_REJECTED'),
                                  (urllib.error.URLError('down'), 'PURGE_UNAVAILABLE')):
            with self.subTest(code=code), mock.patch('dawah.storage.purge.urllib.request.urlopen',
                                                     side_effect=side_effect):
                with self.assertRaises(PurgeError) as caught:
                    CloudflarePurger(self.ZONE, 'tok').purge(['https://media.example.org/a'])
                self.assertEqual(str(caught.exception), code)
                self.assertIsNone(caught.exception.__cause__)
