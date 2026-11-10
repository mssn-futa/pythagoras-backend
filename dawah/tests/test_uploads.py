import logging
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from dawah import storage as storage_module
from dawah import uploads
from dawah.models import EventResource, StorageCleanup, UploadIntent
from dawah.storage import PRIVATE, PUBLIC, PreconditionFailed, StorageUnavailable, UnconfiguredStorage

from .helpers import PUBLISHED, UNPUBLISHED, DawahAPITestCase, User, make_event, make_resource, mp3_bytes

MP3 = mp3_bytes()


def intent_url(event_id, resource_id):
    return f'/dawah/events/{event_id}/resources/{resource_id}/audio/'


def finalize_url(upload_id):
    return f'/dawah/uploads/{upload_id}/finalize/'


def body(data=MP3, **overrides):
    payload = {'file_name': 'Friday Khutbah (final).mp3', 'content_type': 'audio/mpeg', 'size_bytes': len(data)}
    payload.update(overrides)
    return payload


class CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        parts = []
        for record in self.records:
            parts.append(record.getMessage())
            parts.extend(str(v) for v in record.__dict__.values())
        return '\n'.join(parts)


class UploadTestCase(DawahAPITestCase, TestCase):
    def setUp(self):
        super().setUp()
        self.event = make_event(status=PUBLISHED)
        self.resource = make_resource(self.event, title='Patience')
        self.as_admin()

    def issue(self, resource=None, data=MP3, **overrides):
        resource = resource or self.resource
        return self.data(self.client.post(intent_url(resource.event_id, resource.pk), body(data, **overrides),
                                          format='json'), 201)

    def upload(self, data=MP3, resource=None, put_type='audio/mpeg', **overrides):
        intent = self.issue(resource, data, **overrides)
        self.assertEqual(self.storage.client_put(intent['upload_url'], data, content_type=put_type), 200)
        return intent

    def finalize(self, upload_id, status=200):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(finalize_url(upload_id))
        return response if status != 200 else self.data(response)

    def ready_resource(self):
        intent = self.upload()
        self.assertEqual(self.finalize(intent['upload_id'])['upload']['status'], 'ready')
        self.resource.refresh_from_db()
        return intent


class IntentTests(UploadTestCase):
    def test_intent_contract(self):
        before = timezone.now()
        data = self.issue()
        self.assertEqual(set(data), {'upload_id', 'resource_id', 'method', 'upload_url', 'headers', 'expires_at',
                                     'max_size_bytes'})
        self.assertEqual((data['resource_id'], data['method'], data['headers'], data['max_size_bytes']),
                         (self.resource.pk, 'PUT', {'Content-Type': 'audio/mpeg'}, 100 * 1024 * 1024))
        self.assertNotIn('Authorization', data['headers'])
        expires = timezone.datetime.fromisoformat(data['expires_at'].replace('Z', '+00:00'))
        self.assertTrue(data['expires_at'].endswith('Z'))
        self.assertAlmostEqual((expires - before).total_seconds(), 900, delta=5)
        self.assertIn('X-Amz-Expires=900', data['upload_url'])
        intent = UploadIntent.objects.get(pk=data['upload_id'])
        self.assertEqual((intent.status, intent.created_by, intent.file_name, intent.size_bytes),
                         ('issued', self.admin, 'Friday-Khutbah-final.mp3', len(MP3)))
        self.assertTrue(intent.staging_key.startswith('staging/audio/'))
        self.assertTrue(intent.validation_key.startswith('media/audio/'))
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.storage_status, 'pending')
        payload = self.data(self.client.get(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/'))
        self.assertEqual(payload['storage_status'], 'pending')

    def test_keys_are_unpredictable_and_unique(self):
        first, second = self.issue(), self.issue()
        a, b = UploadIntent.objects.get(pk=first['upload_id']), UploadIntent.objects.get(pk=second['upload_id'])
        for intent in (a, b):
            self.assertNotIn('Khutbah', intent.staging_key)
            self.assertNotIn(str(self.resource.pk), intent.staging_key)
            self.assertGreaterEqual(len(intent.staging_key.rsplit('/', 1)[1]), 32)
        self.assertNotEqual(a.staging_key, b.staging_key)
        self.assertNotEqual(a.validation_key, b.validation_key)
        a.refresh_from_db()
        self.assertEqual((a.status, a.error_code), ('failed', 'SUPERSEDED'))

    def test_field_validation(self):
        url = intent_url(self.event.pk, self.resource.pk)
        cases = [
            (body(content_type='audio/wav'), 'content_type'),
            (body(content_type='video/mp4'), 'content_type'),
            (body(file_name='khutbah.wav'), 'file_name'),
            (body(file_name='../etc/khutbah.mp3'), 'file_name'),
            (body(size_bytes=0), 'size_bytes'),
            (body(size_bytes=100 * 1024 * 1024 + 1), 'size_bytes'),
            ({'file_name': 'a.mp3', 'content_type': 'audio/mpeg'}, 'size_bytes'),
            ({**body(), 'key': 'media/audio/mine.mp3'}, 'key'),
            ({**body(), 'bucket': 'public'}, 'bucket'),
        ]
        for payload, field in cases:
            cache.clear()  # throttle counters: this test is about validation only
            with self.subTest(field=field, payload=payload):
                data = self.error(self.client.post(url, payload, format='json'), 400, 'VALIDATION_ERROR')
                self.assertIn(field, data['fields'])
        self.assertEqual(UploadIntent.objects.count(), 0)

    def test_parent_type_and_state_rules(self):
        other = make_event()
        self.error(self.client.post(intent_url(other.pk, self.resource.pk), body(), format='json'), 404, 'NOT_FOUND')
        video = make_resource(self.event, type='vid')
        self.error(self.client.post(intent_url(self.event.pk, video.pk), body(), format='json'), 409,
                   'RESOURCE_TYPE_MISMATCH')
        published = make_resource(self.event, status=PUBLISHED, ready=True)
        self.error(self.client.post(intent_url(self.event.pk, published.pk), body(), format='json'), 409,
                   'UNPUBLISH_REQUIRED')
        self.assertEqual(UploadIntent.objects.count(), 0)

    def test_permissions(self):
        url = intent_url(self.event.pk, self.resource.pk)
        for user, status, code in [(None, 401, 'AUTHENTICATION_REQUIRED'), (self.student, 403, 'PERMISSION_DENIED'),
                                   (self.staff_student, 403, 'PERMISSION_DENIED'),
                                   (self.inactive_admin, 403, 'ACCOUNT_INACTIVE')]:
            with self.subTest(user=user):
                self.as_user(user)
                self.error(self.client.post(url, body(), format='json'), status, code)
        self.assertEqual(UploadIntent.objects.count(), 0)

    def test_throttle_is_five_per_minute_per_admin(self):
        for _ in range(5):
            self.issue()
        response = self.client.post(intent_url(self.event.pk, self.resource.pk), body(), format='json')
        self.error(response, 429, 'THROTTLED')
        self.assertIn('Retry-After', response.headers)
        other_admin = User.objects.create_user(email='admin2@example.test', password='Admin-Pass-9876',
                                               first_name='B', last_name='Admin', role='admin')
        self.as_user(other_admin)
        self.issue()

    def test_unconfigured_storage_is_sanitized_503(self):
        storage_module.set_storage(UnconfiguredStorage())
        data = self.error(self.client.post(intent_url(self.event.pk, self.resource.pk), body(), format='json'),
                          503, 'PROVIDER_UNAVAILABLE')
        self.assertNotIn('fields', data)
        self.assertEqual(UploadIntent.objects.count(), 0)


class FinalizeTests(UploadTestCase):
    def test_happy_path_ready_private_metadata_and_preview(self):
        intent = self.upload()
        data = self.finalize(intent['upload_id'])
        self.assertEqual(set(data), {'upload', 'resource', 'preview_url'})
        self.assertEqual(data['upload']['status'], 'ready')
        self.assertEqual((data['upload']['id'], data['upload']['resource_id']),
                         (intent['upload_id'], self.resource.pk))
        resource = data['resource']
        self.assertEqual((resource['storage_status'], resource['status'], resource['content_type'],
                          resource['size_bytes'], resource['duration'], resource['media_url']),
                         ('ready', 'draft', 'audio/mpeg', len(MP3), 5, None))
        self.assertTrue(resource['uploaded_at'].endswith('Z'))
        self.assertTrue(data['preview_url'].startswith('https://fake-s3.invalid/private/media/audio/'))
        self.assertIn('X-Amz-Expires=300', data['preview_url'])
        self.assertEqual(resource['preview_url'], data['preview_url'])

        self.resource.refresh_from_db()
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.assertEqual(self.resource.private_object_key, stored.validation_key)
        self.assertEqual(self.storage.objects[PRIVATE][stored.validation_key]['data'], MP3)
        self.assertEqual(self.resource.file_name, 'Friday-Khutbah-final.mp3')
        self.assertTrue(StorageCleanup.objects.filter(key=stored.staging_key, reason='staging_consumed').exists())
        self.assertNotIn(stored.staging_key, self.storage.keys(PRIVATE))  # processed after commit
        # Ready drafts stay private.
        self.as_user(None)
        self.error(self.client.get(f'/dawah/resources/{self.resource.pk}/'), 404, 'NOT_FOUND')
        self.assertEqual(self.storage.keys(PUBLIC), set())

    def test_validates_server_copy_not_staging(self):
        intent = self.upload()
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        read_keys = []
        original = self.storage.open_stream

        def spy(key, **kwargs):
            read_keys.append(key)
            return original(key, **kwargs)

        with mock.patch.object(self.storage, 'open_stream', spy):
            self.finalize(intent['upload_id'])
        self.assertEqual(read_keys, [stored.validation_key])

    def test_size_mismatch_is_rejected(self):
        intent = self.issue(size_bytes=len(MP3) + 10)
        self.storage.client_put(intent['upload_url'], MP3, content_type='audio/mpeg')
        data = self.finalize(intent['upload_id'])
        self.assertEqual((data['upload']['status'], data['upload']['error_code']), ('rejected', 'SIZE_MISMATCH'))
        self.assertEqual(data['resource']['storage_status'], 'failed')
        self.assertIsNone(data['preview_url'])

    def test_signed_content_type_mismatch_is_refused_and_observed_type_rejected(self):
        intent = self.issue()
        self.assertEqual(self.storage.client_put(intent['upload_url'], MP3, content_type='text/html'), 403)
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.storage.put_private(stored.staging_key, MP3, content_type='text/html')  # server observes the truth
        data = self.finalize(intent['upload_id'])
        self.assertEqual((data['upload']['status'], data['upload']['error_code']),
                         ('rejected', 'CONTENT_TYPE_MISMATCH'))

    def test_etag_changed_during_copy_is_retryable(self):
        intent = self.upload()
        self.storage.fail['copy_if_match'] = PreconditionFailed()
        self.error(self.finalize(intent['upload_id'], status=409), 409, 'UPLOAD_CHANGED')
        self.assertEqual(UploadIntent.objects.get(pk=intent['upload_id']).status, 'issued')
        del self.storage.fail['copy_if_match']
        self.assertEqual(self.finalize(intent['upload_id'])['upload']['status'], 'ready')

    def test_missing_object_is_retryable_until_put(self):
        intent = self.issue()
        self.error(self.finalize(intent['upload_id'], status=409), 409, 'UPLOAD_INCOMPLETE')
        self.assertEqual(UploadIntent.objects.get(pk=intent['upload_id']).status, 'issued')
        self.storage.client_put(intent['upload_url'], MP3, content_type='audio/mpeg')
        self.assertEqual(self.finalize(intent['upload_id'])['upload']['status'], 'ready')

    def test_invalid_mp3_is_rejected_and_cleaned_up(self):
        fake = b'<html><script>alert(1)</script></html>' * 100
        intent = self.upload(data=fake)
        data = self.finalize(intent['upload_id'])
        self.assertEqual((data['upload']['status'], data['upload']['error_code']), ('rejected', 'INVALID_MP3'))
        self.assertEqual(data['upload']['message'], 'The file is not valid MP3 audio.')
        self.resource.refresh_from_db()
        self.assertEqual((self.resource.storage_status, self.resource.private_object_key), ('failed', ''))
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.assertEqual(self.storage.keys(PRIVATE) & {stored.staging_key, stored.validation_key}, set())
        self.error(self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/publish/'),
                   409, 'MEDIA_NOT_READY')

    def test_replay_returns_saved_outcome_without_reattaching(self):
        intent = self.upload()
        first = self.finalize(intent['upload_id'])
        copies = [c for c in self.storage.calls if c[0] == 'copy_if_match']
        UploadIntent.objects.filter(pk=intent['upload_id']).update(expires_at=timezone.now() - timedelta(hours=1))
        second = self.finalize(intent['upload_id'])
        self.assertEqual(second['upload']['status'], 'ready')
        self.assertEqual(second['resource']['uploaded_at'], first['resource']['uploaded_at'])
        self.assertEqual([c for c in self.storage.calls if c[0] == 'copy_if_match'], copies)
        # A rejected outcome replays too.
        bad = self.upload(data=b'x' * 5000)
        self.assertEqual(self.finalize(bad['upload_id'])['upload']['status'], 'rejected')
        self.assertEqual(self.finalize(bad['upload_id'])['upload']['error_code'], 'INVALID_MP3')

    def test_expired_unfinished_intent_is_rejected(self):
        intent = self.upload()
        UploadIntent.objects.filter(pk=intent['upload_id']).update(expires_at=timezone.now() - timedelta(seconds=1))
        data = self.finalize(intent['upload_id'])
        self.assertEqual((data['upload']['status'], data['upload']['error_code']), ('expired', 'UPLOAD_EXPIRED'))
        self.assertEqual(data['resource']['storage_status'], 'failed')
        self.assertFalse([c for c in self.storage.calls if c[0] == 'copy_if_match'])

    def test_ownership_and_permissions(self):
        intent = self.upload()
        other_admin = User.objects.create_user(email='admin2@example.test', password='Admin-Pass-9876',
                                               first_name='B', last_name='Admin', role='admin')
        self.as_user(other_admin)
        self.error(self.client.post(finalize_url(intent['upload_id'])), 404, 'NOT_FOUND')
        self.as_user(self.student)
        self.error(self.client.post(finalize_url(intent['upload_id'])), 403, 'PERMISSION_DENIED')
        self.as_user(None)
        self.error(self.client.post(finalize_url(intent['upload_id'])), 401, 'AUTHENTICATION_REQUIRED')
        self.as_admin()
        self.error(self.client.post(finalize_url(999999)), 404, 'NOT_FOUND')
        self.assertEqual(UploadIntent.objects.get(pk=intent['upload_id']).status, 'issued')

    def test_provider_failure_is_sanitized_and_retryable(self):
        intent = self.upload()
        self.storage.fail['head'] = StorageUnavailable('PROVIDER_UNAVAILABLE')
        data = self.error(self.finalize(intent['upload_id'], status=503), 503, 'PROVIDER_UNAVAILABLE')
        self.assertEqual(set(data), {'code'})
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.assertEqual((stored.status, stored.lease_token), ('validating', ''))
        del self.storage.fail['head']
        self.assertEqual(self.finalize(intent['upload_id'])['upload']['status'], 'ready')

    def test_live_lease_blocks_and_expired_lease_retries_saved_copy(self):
        intent = self.upload()
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        stored.status, stored.lease_token = 'validating', 'other-worker'
        stored.lease_expires_at = timezone.now() + timedelta(minutes=1)
        stored.save()
        self.error(self.finalize(intent['upload_id'], status=409), 409, 'UPLOAD_IN_PROGRESS')
        # A previous attempt made the immutable copy, then its lease lapsed after intent expiry:
        # retry validates the same saved copy (no new copy from staging).
        self.storage.copy_if_match(stored.staging_key, stored.validation_key, etag=self.storage.head(
            stored.staging_key).etag, content_type='audio/mpeg')
        copy = self.storage.head(stored.validation_key)
        UploadIntent.objects.filter(pk=stored.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1), expires_at=timezone.now() - timedelta(hours=1),
            observed_etag=copy.etag, validation_etag=copy.etag, observed_size=copy.size)
        self.storage.calls.clear()
        self.assertEqual(self.finalize(intent['upload_id'])['upload']['status'], 'ready')
        self.assertFalse([c for c in self.storage.calls if c[0] == 'copy_if_match'])

    def test_staging_overwrite_after_finalize_cannot_change_validated_or_published_bytes(self):
        intent = self.ready_resource()
        validated_key = self.resource.private_object_key
        # The signed PUT is still unexpired: the browser (or an attacker holding it) overwrites staging.
        evil = mp3_bytes(frames=50) + b'tampered'
        self.assertEqual(self.storage.client_put(intent['upload_url'], evil, content_type='audio/mpeg'), 200)
        self.assertEqual(self.storage.objects[PRIVATE][validated_key]['data'], MP3)
        self.assertEqual(self.finalize(intent['upload_id'])['upload']['status'], 'ready')  # replay
        self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/publish/'))
        self.resource.refresh_from_db()
        status, data, _ = self.storage.public_get(self.resource.public_media_url)
        self.assertEqual((status, data), (200, MP3))

    def test_signed_put_cannot_target_other_keys(self):
        intent = self.issue()
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        swapped = intent['upload_url'].replace(stored.staging_key, stored.validation_key)
        self.assertEqual(self.storage.client_put(swapped, MP3, content_type='audio/mpeg'), 403)
        self.assertEqual(self.storage.client_put('https://fake-s3.invalid/private/' + stored.validation_key,
                                                 MP3, content_type='audio/mpeg'), 403)
        self.assertNotIn(stored.validation_key, self.storage.keys(PRIVATE))
        presigned = [c[1]['key'] for c in self.storage.calls if c[0] == 'presign_put']
        self.assertTrue(all(key.startswith('staging/') for key in presigned))

    def test_replacing_a_ready_draft_asset(self):
        self.ready_resource()
        old_key, old_uploaded = self.resource.private_object_key, self.resource.uploaded_at
        intent = self.upload(data=mp3_bytes(frames=400))
        payload = self.data(self.client.get(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/'))
        self.assertEqual(payload['storage_status'], 'ready')  # old asset stays usable while the new one uploads
        data = self.finalize(intent['upload_id'])
        self.resource.refresh_from_db()
        self.assertNotEqual(self.resource.private_object_key, old_key)
        self.assertGreater(self.resource.uploaded_at, old_uploaded)
        self.assertEqual(data['resource']['duration'], 10)
        self.assertTrue(StorageCleanup.objects.filter(key=old_key, reason='replaced').exists())
        self.assertNotIn(old_key, self.storage.keys(PRIVATE))

    def test_publish_between_intent_and_finalize_fails_attach(self):
        self.ready_resource()
        intent = self.upload(data=mp3_bytes(frames=300))
        self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/publish/'))
        data = self.finalize(intent['upload_id'])
        self.assertEqual((data['upload']['status'], data['upload']['error_code']), ('failed', 'UNPUBLISH_REQUIRED'))
        published_key = self.resource.private_object_key
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.private_object_key, published_key)


class PublicationTests(UploadTestCase):
    def publish(self, status=200):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/publish/')
        return self.data(response) if status == 200 else response

    def test_publish_promotes_to_unique_public_key_and_sets_media_url(self):
        self.ready_resource()
        data = self.publish()
        self.resource.refresh_from_db()
        key = self.resource.public_object_key
        self.assertRegex(key, r'^audio/[A-Za-z0-9_-]{32}/Friday-Khutbah-final\.mp3$')
        self.assertEqual(data['media_url'], f'https://media.example.test/{key}')
        self.assertEqual(data['status'], 'published')
        stored = self.storage.objects[PUBLIC][key]
        self.assertEqual((stored['data'], stored['content_type'], stored['cache_control'],
                          stored['content_disposition']),
                         (MP3, 'audio/mpeg', 'public, max-age=31536000, immutable',
                          'inline; filename="Friday-Khutbah-final.mp3"'))
        status, chunk, headers = self.storage.public_get(data['media_url'], byte_range=(0, 99))
        self.assertEqual((status, chunk, headers['Content-Range']), (206, MP3[:100], f'bytes 0-99/{len(MP3)}'))
        # Public readers see it with the canonical URL and no private capability.
        self.as_user(None)
        public = self.data(self.client.get(f'/dawah/resources/{self.resource.pk}/'))
        self.assertEqual((public['media_url'], public['preview_url'], public['storage_status']),
                         (data['media_url'], None, 'ready'))
        archive = self.data(self.client.get('/dawah/archive/'))['results']
        self.assertEqual([item['media_url'] for item in archive], [data['media_url']])

    def test_republish_uses_a_new_versioned_key(self):
        self.ready_resource()
        first = self.publish()['media_url']
        with self.captureOnCommitCallbacks(execute=True):
            self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/unpublish/'))
        second = self.publish()['media_url']
        self.assertNotEqual(first, second)
        self.assertEqual(self.storage.public_get(first)[0], 404)
        self.assertEqual(self.storage.public_get(second)[0], 200)

    def test_publish_requires_ready_asset(self):
        self.issue()
        self.error(self.publish(status=409), 409, 'MEDIA_NOT_READY')
        self.assertEqual(self.storage.keys(PUBLIC), set())

    def test_promotion_failure_keeps_draft_and_records_cleanup(self):
        self.ready_resource()
        self.storage.fail['copy_if_match'] = StorageUnavailable('PROVIDER_UNAVAILABLE')
        self.error(self.publish(status=503), 503, 'PROVIDER_UNAVAILABLE')
        self.resource.refresh_from_db()
        self.assertEqual((self.resource.status, self.resource.public_media_url), ('draft', ''))
        self.assertTrue(StorageCleanup.objects.filter(bucket='public', reason='promotion_failed').exists())

    def test_asset_changed_during_promotion_aborts(self):
        self.ready_resource()
        original = self.storage.copy_if_match

        def copy_then_change(*args, **kwargs):
            info = original(*args, **kwargs)
            EventResource.objects.filter(pk=self.resource.pk).update(etag='different')
            return info

        with mock.patch.object(self.storage, 'copy_if_match', copy_then_change):
            self.error(self.publish(status=409), 409, 'MEDIA_CHANGED')
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.status, 'draft')
        row = StorageCleanup.objects.get(bucket='public', reason='publish_aborted')
        self.assertIn(row.key, self.storage.keys(PUBLIC))
        uploads.process_cleanup()
        self.assertNotIn(row.key, self.storage.keys(PUBLIC))

    def test_unpublish_hides_and_records_public_cleanup(self):
        self.ready_resource()
        url = self.publish()['media_url']
        public_key = self.resource.public_object_key or EventResource.objects.get(pk=self.resource.pk).public_object_key
        with self.captureOnCommitCallbacks(execute=True):
            data = self.data(self.client.post(
                f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/unpublish/'))
        self.assertEqual((data['status'], data['media_url'], data['storage_status']), ('unpublished', None, 'ready'))
        row = StorageCleanup.objects.get(bucket='public', key=public_key)
        self.assertEqual((row.reason, row.status), ('unpublished', 'done'))
        self.assertEqual(self.storage.public_get(url)[0], 404)
        self.as_user(None)
        self.error(self.client.get(f'/dawah/resources/{self.resource.pk}/'), 404, 'NOT_FOUND')
        # The private validated asset remains for republication.
        self.resource.refresh_from_db()
        self.assertIn(self.resource.private_object_key, self.storage.keys(PRIVATE))

    def test_failed_immediate_cleanup_stays_recorded_for_command(self):
        self.ready_resource()
        self.publish()
        self.storage.fail['delete'] = StorageUnavailable('PROVIDER_UNAVAILABLE')
        with self.captureOnCommitCallbacks(execute=True):
            self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/unpublish/'))
        row = StorageCleanup.objects.get(reason='unpublished')
        self.assertEqual((row.status, row.attempts, row.last_error_code), ('pending', 1, 'PROVIDER_UNAVAILABLE'))
        del self.storage.fail['delete']
        out = StringIO()
        call_command('dawah_storage_cleanup', stdout=out)
        row.refresh_from_db()
        self.assertEqual(row.status, 'done')
        self.assertNotIn(row.key, out.getvalue())

    def test_event_unpublish_hides_resource_without_rewriting_state(self):
        self.ready_resource()
        self.publish()
        self.data(self.client.post(f'/dawah/events/{self.event.pk}/unpublish/'))
        self.resource.refresh_from_db()
        self.assertEqual((self.resource.status, bool(self.resource.public_media_url)), ('published', True))
        self.as_user(None)
        self.error(self.client.get(f'/dawah/resources/{self.resource.pk}/'), 404, 'NOT_FOUND')
        self.assertEqual(self.data(self.client.get('/dawah/archive/'))['count'], 0)


class CleanupTests(UploadTestCase):
    def test_delete_resource_records_and_processes_cleanup(self):
        self.ready_resource()
        pending = self.issue()  # an unfinished replacement intent too
        stored = UploadIntent.objects.get(pk=pending['upload_id'])
        private_key = self.resource.private_object_key
        with self.captureOnCommitCallbacks(execute=True):
            self.client.delete(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/')
        self.assertFalse(EventResource.objects.filter(pk=self.resource.pk).exists())
        keys = set(StorageCleanup.objects.filter(resource_id=self.resource.pk, reason='resource_deleted')
                   .values_list('key', flat=True))
        self.assertLessEqual({private_key, stored.staging_key, stored.validation_key}, keys)
        self.assertNotIn(private_key, self.storage.keys(PRIVATE))
        self.error(self.client.post(finalize_url(stored.pk)), 404, 'NOT_FOUND')

    def test_delete_event_records_cleanup_for_its_resources(self):
        draft_event = make_event()
        resource = make_resource(draft_event, ready=True)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.delete(f'/dawah/events/{draft_event.pk}/')
        self.assertTrue(StorageCleanup.objects.filter(resource_id=resource.pk, key=resource.private_object_key,
                                                      status='done').exists())

    def test_cleanup_never_deletes_referenced_keys(self):
        self.ready_resource()
        row = uploads.record_cleanup(PRIVATE, self.resource.private_object_key, 'test')
        self.assertEqual(uploads.process_cleanup(), {'done': 0, 'skipped': 1, 'failed': 0})
        row.refresh_from_db()
        self.assertEqual(row.status, 'skipped')
        self.assertIn(self.resource.private_object_key, self.storage.keys(PRIVATE))

    def test_command_expires_stale_intents(self):
        intent = self.upload()
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        UploadIntent.objects.filter(pk=stored.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        out = StringIO()
        call_command('dawah_storage_cleanup', stdout=out)
        stored.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual((stored.status, self.resource.storage_status), ('expired', 'failed'))
        self.assertNotIn(stored.staging_key, self.storage.keys(PRIVATE))
        self.assertIn('intents expired=1', out.getvalue())
        self.assertNotIn('staging/', out.getvalue())

    def test_type_change_supersedes_open_intents(self):
        intent = self.upload()
        self.data(self.client.patch(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/',
                                    {'type': 'doc'}, format='json'))
        data = self.finalize(intent['upload_id'])
        self.assertEqual((data['upload']['status'], data['upload']['error_code']),
                         ('failed', 'RESOURCE_TYPE_CHANGED'))


class LeakageAndVisibilityTests(UploadTestCase):
    def test_no_keys_or_signatures_in_public_payloads_or_logs(self):
        handler = CapturingHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)

        intent = self.ready_resource()
        with self.captureOnCommitCallbacks(execute=True):
            self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/publish/'))
        self.resource.refresh_from_db()
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        secrets = [stored.staging_key, stored.validation_key, self.resource.private_object_key,
                   self.resource.etag, 'X-Amz-Signature', 'fake-s3.invalid']

        self.as_user(None)
        public_bodies = [
            self.client.get(f'/dawah/resources/{self.resource.pk}/').content.decode(),
            self.client.get(f'/dawah/events/{self.event.pk}/').content.decode(),
            self.client.get(f'/dawah/events/{self.event.pk}/resources/').content.decode(),
            self.client.get('/dawah/archive/').content.decode(),
        ]
        self.as_user(self.student)
        public_bodies.append(self.client.get('/dawah/archive/').content.decode())
        for text in public_bodies:
            for secret in secrets:
                self.assertNotIn(secret, text)
            self.assertIn(self.resource.public_media_url, text)
        # Admin payloads expose the preview capability, never object keys/ETags.
        self.as_admin()
        admin_text = self.client.get(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/').content.decode()
        for secret in secrets[:4]:
            if secret != self.resource.private_object_key:
                self.assertNotIn(secret, admin_text)
        self.assertNotIn('private_object_key', admin_text)
        self.assertNotIn('"etag"', admin_text)
        log_text = handler.text()
        for secret in secrets:
            self.assertNotIn(secret, log_text)
        self.assertNotIn(intent['upload_url'], log_text)

    def test_visibility_requires_ready_promoted_asset_and_published_parent(self):
        ready_draft = make_resource(self.event, ready=True)
        assetless = make_resource(self.event, status=PUBLISHED)  # inconsistent row: published but no asset
        EventResource.objects.filter(pk=assetless.pk).update(storage_status='ready')
        promoted = make_resource(self.event, status=PUBLISHED, ready=True)
        hidden_parent = make_event(status=UNPUBLISHED)
        under_hidden = make_resource(hidden_parent, status=PUBLISHED, ready=True)
        visible = set(EventResource.objects.visible().values_list('pk', flat=True))
        self.assertEqual(visible, {promoted.pk})
        for resource in (ready_draft, assetless, under_hidden):
            self.assertNotIn(resource.pk, visible)
        assetless.refresh_from_db()
        self.assertFalse(assetless.media_ready())
        self.assertTrue(ready_draft.media_ready())


@override_settings(R2_UPLOAD_INTENT_TTL_SECONDS=60, R2_PREVIEW_TTL_SECONDS=30)
class TtlSettingTests(UploadTestCase):
    def test_ttls_come_from_settings(self):
        data = self.issue()
        self.assertIn('X-Amz-Expires=60', data['upload_url'])
        self.storage.client_put(data['upload_url'], MP3, content_type='audio/mpeg')
        self.assertIn('X-Amz-Expires=30', self.finalize(data['upload_id'])['preview_url'])


@override_settings(R2_UPLOAD_INTENT_TTL_SECONDS=86400, R2_PREVIEW_TTL_SECONDS=86400)
class TtlCapTests(UploadTestCase):
    def test_ttls_are_capped_at_fifteen_minutes(self):
        data = self.issue()
        self.assertIn('X-Amz-Expires=900', data['upload_url'])
        self.storage.client_put(data['upload_url'], MP3, content_type='audio/mpeg')
        self.assertIn('X-Amz-Expires=900', self.finalize(data['upload_id'])['preview_url'])
