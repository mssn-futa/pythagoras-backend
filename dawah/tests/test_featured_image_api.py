"""Event featured images: intent, finalize, promotion/pointer swap, visibility (DAWAH-07)."""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from dawah import services, uploads
from dawah.models import Event, StorageCleanup, UploadIntent
from dawah.storage import PRIVATE, PUBLIC, StorageUnavailable

from . import files
from .helpers import DRAFT, PUBLISHED, DawahAPITestCase, make_event, make_visible_resource
from .test_documents_api import UploadFlowMixin

JPEG = files.image_bytes('JPEG')
PNG = files.image_bytes('PNG', size=(120, 80))


class ImageTestCase(UploadFlowMixin, DawahAPITestCase, TestCase):
    def setUp(self):
        super().setUp()
        self.event = make_event(status=DRAFT)
        self.as_admin()

    @property
    def intent_url(self):
        return f'/dawah/events/{self.event.pk}/featured-image/'

    def issue(self, data=JPEG, name='Friday Photo.jpeg', content_type='image/jpeg', status=201, **overrides):
        payload = {'file_name': name, 'content_type': content_type, 'size_bytes': len(data), **overrides}
        response = self.client.post(self.intent_url, payload, format='json')
        return self.data(response, status) if status == 201 else response

    def upload(self, data=JPEG, **kwargs):
        intent = self.issue(data, **kwargs)
        self.put(intent, data)
        return intent

    def ready(self, data=JPEG, **kwargs):
        cache.clear()
        intent = self.upload(data, **kwargs)
        result = self.finalized(intent['upload_id'])
        self.assertEqual(result['upload']['status'], 'ready', result)
        self.event.refresh_from_db()
        return result

    def publish(self):
        with self.captureOnCommitCallbacks(execute=True):
            data = self.data(self.client.post(f'/dawah/events/{self.event.pk}/publish/'))
        self.event.refresh_from_db()
        return data

    def unpublish(self):
        with self.captureOnCommitCallbacks(execute=True):
            data = self.data(self.client.post(f'/dawah/events/{self.event.pk}/unpublish/'))
        self.event.refresh_from_db()
        return data

    def public_event(self):
        self.as_user(None)
        try:
            return self.client.get(f'/dawah/events/{self.event.pk}/')
        finally:
            self.as_admin()


class ImageIntentTests(ImageTestCase):
    def test_intent_contract(self):
        intent = self.issue()
        self.assertEqual(set(intent), {'upload_id', 'resource_id', 'event_id', 'method', 'upload_url', 'headers',
                                       'expires_at', 'max_size_bytes'})
        self.assertEqual((intent['resource_id'], intent['event_id'], intent['headers'], intent['max_size_bytes']),
                         (None, self.event.pk, {'Content-Type': 'image/jpeg'}, 10 * 1024 * 1024))
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.assertEqual((stored.kind, stored.resource_id, stored.event_id, stored.file_name),
                         ('image', None, self.event.pk, 'Friday-Photo.jpg'))
        self.assertTrue(stored.staging_key.startswith('staging/images/'))
        self.assertTrue(stored.validation_key.startswith('media/images/') and stored.validation_key.endswith('.jpg'))
        event = self.data(self.client.get(f'/dawah/events/{self.event.pk}/'))
        self.assertEqual(event['featured_image_storage_status'], 'pending')
        # A newer intent supersedes the unfinished one.
        newer = self.issue(PNG, name='x.png', content_type='image/png')
        stored.refresh_from_db()
        self.assertEqual((stored.status, stored.error_code), ('failed', 'SUPERSEDED'))
        self.assertEqual(UploadIntent.objects.get(pk=newer['upload_id']).validation_key[-4:], '.png')

    def test_field_validation(self):
        cases = [
            ({'content_type': 'image/gif', 'name': 'a.gif'}, 'content_type'),
            ({'content_type': 'image/svg+xml', 'name': 'a.svg'}, 'content_type'),
            ({'content_type': 'image/webp', 'name': 'a.webp'}, 'content_type'),
            ({'name': 'a.png'}, 'file_name'),  # does not match image/jpeg
            ({'name': 'a.gif'}, 'file_name'),
            ({'size_bytes': 10 * 1024 * 1024 + 1}, 'size_bytes'),
            ({'size_bytes': -5}, 'size_bytes'),
        ]
        for overrides, field in cases:
            with self.subTest(overrides):
                cache.clear()
                self.assertIn(field, self.error(self.issue(status=400, **overrides), 400,
                                                'VALIDATION_ERROR')['fields'])
        self.assertFalse(UploadIntent.objects.exists())

    def test_permissions_and_missing_event(self):
        self.error(self.client.post('/dawah/events/999/featured-image/', {}, format='json'), 404, 'NOT_FOUND')
        payload = {'file_name': 'a.png', 'content_type': 'image/png', 'size_bytes': 10}
        for user, status, code in ((None, 401, 'AUTHENTICATION_REQUIRED'), (self.student, 403, 'PERMISSION_DENIED'),
                                   (self.staff_student, 403, 'PERMISSION_DENIED'),
                                   (self.inactive_admin, 401, 'AUTHENTICATION_REQUIRED')):
            with self.subTest(user=getattr(user, 'email', None)):
                self.as_user(user)
                response = self.client.post(self.intent_url, payload, format='json')
                self.assertIn(response.status_code, (401, 403))
                if user is not self.inactive_admin:
                    self.error(response, status, code)
        self.assertFalse(UploadIntent.objects.exists())

    def test_intent_allowed_on_published_event(self):
        self.event.status = PUBLISHED
        self.event.save()
        self.issue()


class ImageFinalizeTests(ImageTestCase):
    def test_finalize_on_draft_event(self):
        intent = self.upload()
        result = self.finalized(intent['upload_id'])
        self.assertEqual(set(result), {'upload', 'resource', 'event', 'preview_url'})
        self.assertEqual((result['upload']['kind'], result['upload']['resource_id'], result['upload']['event_id'],
                          result['resource']), ('image', None, self.event.pk, None))
        event = result['event']
        self.assertEqual((event['id'], event['featured_image_storage_status'], event['featured_image_url']),
                         (self.event.pk, 'ready', None))
        self.assertTrue(event['featured_image_uploaded_at'].endswith('Z'))
        self.assertEqual(event['featured_image_preview_url'], result['preview_url'])
        self.assertTrue(result['preview_url'].startswith('https://fake-s3.invalid/private/media/images/'))
        call = [kw for name, kw in self.storage.calls if name == 'presign_get'][-1]
        self.assertEqual((call['content_type'], call['disposition']), ('image/jpeg', 'inline'))
        self.event.refresh_from_db()
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        key = self.event.featured_image_object_key
        derivative = self.storage.objects[PRIVATE][key]
        # The attached object is the server-made derivative, not the raw upload.
        self.assertTrue(key.startswith('media/images/') and key.endswith('.jpg'))
        self.assertNotEqual(key, stored.validation_key)
        self.assertEqual((self.event.featured_image_public_key, self.event.featured_image_size_bytes,
                          self.event.featured_image_content_type, self.event.featured_image_etag),
                         ('', len(derivative['data']), 'image/jpeg', derivative['etag']))
        self.assertEqual(self.storage.keys(PUBLIC), set())  # draft images stay private
        self.assertNotIn(stored.staging_key, self.storage.keys(PRIVATE))
        self.assertNotIn(stored.validation_key, self.storage.keys(PRIVATE))  # raw copy cleaned up
        self.assertEqual(StorageCleanup.objects.get(key=stored.validation_key).reason, 'image_sanitized')
        self.assertEqual(StorageCleanup.objects.get(key=key).status, 'cancelled')
        # Replay returns the saved outcome.
        self.assertEqual(self.finalized(intent['upload_id'])['upload'], result['upload'])

    def test_rejections(self):
        cases = [
            (files.png_declaring(30000, 30000), 'image/png', 'a.png', 'IMAGE_TOO_LARGE'),
            (files.png_declaring(6000, 6000), 'image/png', 'a.png', 'IMAGE_TOO_LARGE'),
            (files.jpeg_declaring(9000, 9000), 'image/jpeg', 'a.jpg', 'IMAGE_TOO_LARGE'),
            (PNG, 'image/jpeg', 'a.jpg', 'CONTENT_TYPE_MISMATCH'),
            (files.image_bytes('GIF'), 'image/png', 'a.png', 'INVALID_IMAGE'),
            (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', 'image/png', 'a.png', 'INVALID_IMAGE'),
            (PNG[:len(PNG) - 20], 'image/png', 'a.png', 'INVALID_IMAGE'),
        ]
        for data, content_type, name, code in cases:
            with self.subTest(code=code, name=name):
                cache.clear()
                intent = self.upload(data, name=name, content_type=content_type)
                result = self.finalized(intent['upload_id'])
                self.assertEqual((result['upload']['status'], result['upload']['error_code']), ('rejected', code))
                self.assertEqual(result['event']['featured_image_storage_status'], 'failed')
                self.assertIsNone(result['preview_url'])
                stored = UploadIntent.objects.get(pk=intent['upload_id'])
                self.assertNotIn(stored.validation_key, self.storage.keys(PRIVATE))
                row = StorageCleanup.objects.get(key=stored.validation_key)
                self.assertEqual((row.event_id, row.resource_id, row.status), (self.event.pk, None, 'done'))

    def test_failed_replacement_keeps_the_current_image(self):
        self.ready()
        self.publish()
        url = self.event.featured_image_url
        before = self.storage.public_get(url)[1]
        cache.clear()
        intent = self.upload(files.png_declaring(30000, 30000), name='bomb.png', content_type='image/png')
        self.assertEqual(self.finalized(intent['upload_id'])['upload']['error_code'], 'IMAGE_TOO_LARGE')
        self.event.refresh_from_db()
        self.assertEqual((self.event.featured_image_url, self.event.featured_image_storage_status), (url, 'ready'))
        self.assertEqual(self.storage.public_get(url)[1], before)

    def test_expired_intent(self):
        intent = self.upload()
        UploadIntent.objects.filter(pk=intent['upload_id']).update(expires_at='2000-01-01T00:00:00Z')
        result = self.finalized(intent['upload_id'])
        self.assertEqual((result['upload']['status'], result['upload']['error_code']), ('expired', 'UPLOAD_EXPIRED'))
        self.assertEqual(result['event']['featured_image_storage_status'], 'failed')

    def test_cleanup_command_expires_image_intents(self):
        intent = self.issue()
        UploadIntent.objects.filter(pk=intent['upload_id']).update(expires_at='2000-01-01T00:00:00Z')
        self.assertEqual(uploads.expire_stale_intents(), {'expired': 1, 'abandoned': 0})
        self.event.refresh_from_db()
        self.assertEqual(self.event.featured_image_storage_status, 'failed')
        self.assertTrue(StorageCleanup.objects.filter(event_id=self.event.pk, reason='expired').exists())

    def test_only_the_owner_finalizes(self):
        intent = self.upload()
        self.as_user(self.student)
        self.error(self.finalize(intent['upload_id']), 403, 'PERMISSION_DENIED')
        from .helpers import User
        other = User.objects.create_user(email='o@example.test', password='Other-Pass-9876', first_name='O',
                                         last_name='A', role='admin')
        self.as_user(other)
        self.error(self.finalize(intent['upload_id']), 404, 'NOT_FOUND')


class ImagePublicationTests(ImageTestCase):
    def test_publish_promotes_and_unpublish_retires(self):
        self.ready(PNG, name='Poster.png', content_type='image/png')
        self.error(self.public_event(), 404, 'NOT_FOUND')
        published = self.publish()
        url = published['featured_image_url']
        self.assertTrue(url.startswith('https://media.example.test/images/') and url.endswith('/Poster.png'))
        status, body, headers = self.storage.public_get(url)
        self.assertEqual((status, body, headers['Content-Type'], headers['Content-Disposition'],
                          headers['Cache-Control']),
                         (200, PNG, 'image/png', 'inline; filename="Poster.png"',
                          'public, max-age=31536000, immutable'))
        self.assertEqual(StorageCleanup.objects.get(key=self.event.featured_image_public_key).status, 'cancelled')

        public = self.data(self.public_event())
        self.assertEqual((public['featured_image_url'], public['featured_image_preview_url'],
                          public['featured_image_storage_status']), (url, None, 'ready'))
        self.assert_no_secrets(self.public_event())
        self.as_user(None)
        listed = self.data(self.client.get('/dawah/events/'))['results'][0]
        self.assertEqual(listed['featured_image_url'], url)
        resource = make_visible_resource(self.event)
        nested = self.data(self.client.get(f'/dawah/resources/{resource.pk}/'))['event']
        self.assertEqual(nested['featured_image_url'], url)
        archive = self.data(self.client.get('/dawah/archive/'))['results'][0]['event']
        self.assertEqual(archive['featured_image_url'], url)
        self.as_user(self.student)
        self.assertIsNone(self.data(self.client.get(f'/dawah/events/{self.event.pk}/'))['featured_image_preview_url'])

        self.as_admin()
        unpublished = self.unpublish()
        self.assertIsNone(unpublished['featured_image_url'])
        self.assertEqual(self.storage.public_get(url)[0], 404)
        self.assertIn(url, self.purger.purged)
        self.assertEqual(StorageCleanup.objects.get(public_url=url, reason='event_unpublished').status, 'done')
        admin_view = self.data(self.client.get(f'/dawah/events/{self.event.pk}/'))
        self.assertEqual(admin_view['featured_image_storage_status'], 'ready')
        self.assertIsNotNone(admin_view['featured_image_preview_url'])
        self.assertIsNone(admin_view['featured_image_url'])

        republished = self.publish()
        self.assertNotEqual(republished['featured_image_url'], url)
        self.assertEqual(self.storage.public_get(republished['featured_image_url'])[1], PNG)

    def test_replace_while_published_swaps_pointer_after_promotion(self):
        self.ready()
        self.publish()
        old_url, old_public, old_private = (self.event.featured_image_url, self.event.featured_image_public_key,
                                            self.event.featured_image_object_key)
        cache.clear()
        intent = self.upload(PNG, name='new.png', content_type='image/png')
        # Until finalize succeeds, readers keep the old image.
        self.assertEqual(self.data(self.public_event())['featured_image_url'], old_url)
        result = self.finalized(intent['upload_id'])
        new_url = result['event']['featured_image_url']
        self.assertTrue(new_url.startswith('https://media.example.test/images/') and new_url.endswith('/new.png'))
        self.assertNotEqual(new_url, old_url)
        self.assertEqual(self.data(self.public_event())['featured_image_url'], new_url)
        self.assertEqual(self.storage.public_get(new_url)[1], PNG)
        self.assertEqual(self.storage.public_get(old_url)[0], 404)
        self.assertNotIn(old_private, self.storage.keys(PRIVATE))
        self.assertIn(old_url, self.purger.purged)
        rows = {(r.bucket, r.key): r for r in StorageCleanup.objects.filter(event_id=self.event.pk)}
        self.assertEqual((rows[(PUBLIC, old_public)].reason, rows[(PUBLIC, old_public)].status), ('replaced', 'done'))
        self.assertEqual(rows[(PRIVATE, old_private)].reason, 'replaced')
        self.event.refresh_from_db()
        self.assertEqual(rows[(PUBLIC, self.event.featured_image_public_key)].status, 'cancelled')

    def test_promotion_failure_preserves_old_pointer_and_is_retryable(self):
        self.ready()
        self.publish()
        old_url = self.event.featured_image_url
        cache.clear()
        intent = self.upload(PNG, name='new.png', content_type='image/png')
        original = self.storage.copy_if_match

        def failing_public_copy(*args, **kwargs):
            if kwargs.get('dst_bucket') == PUBLIC:
                raise StorageUnavailable('PROVIDER_UNAVAILABLE')
            return original(*args, **kwargs)

        self.storage.copy_if_match = failing_public_copy
        self.error(self.finalize(intent['upload_id']), 503, 'PROVIDER_UNAVAILABLE')
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.assertEqual((stored.status, stored.lease_token), ('validating', ''))
        self.event.refresh_from_db()
        self.assertEqual(self.event.featured_image_url, old_url)
        self.assertTrue(StorageCleanup.objects.filter(reason='promotion_failed', bucket=PUBLIC).exists())
        self.storage.copy_if_match = original
        result = self.finalized(intent['upload_id'])
        self.assertEqual(result['upload']['status'], 'ready')
        self.assertNotEqual(result['event']['featured_image_url'], old_url)

    def test_event_published_during_validation_requires_retry(self):
        intent = self.upload()
        original = uploads._complete

        def publish_meanwhile(*args, **kwargs):
            # After validation saw a draft event (no promotion), before attaching.
            services.publish_event(self.event.pk)  # no image yet: nothing to promote
            return original(*args, **kwargs)

        with mock.patch.object(uploads, '_complete', side_effect=publish_meanwhile):
            self.error(self.finalize(intent['upload_id']), 409, 'MEDIA_CHANGED')
        self.assertEqual(self.storage.keys(PUBLIC), set())
        self.event.refresh_from_db()
        self.assertEqual((self.event.status, self.event.featured_image_object_key), (PUBLISHED, ''))
        result = self.finalized(intent['upload_id'])
        self.assertEqual(result['upload']['status'], 'ready')
        self.assertIsNotNone(result['event']['featured_image_url'])

    def test_event_unpublished_after_promotion_attaches_privately(self):
        self.event.status = PUBLISHED
        self.event.save()
        intent = self.upload()
        original = self.storage.copy_if_match

        def unpublish_after_public_copy(*args, **kwargs):
            info = original(*args, **kwargs)
            if kwargs.get('dst_bucket') == PUBLIC:
                services.unpublish_event(self.event.pk)
            return info

        self.storage.copy_if_match = unpublish_after_public_copy
        result = self.finalized(intent['upload_id'])
        self.assertEqual(result['upload']['status'], 'ready')
        self.assertIsNone(result['event']['featured_image_url'])
        self.event.refresh_from_db()
        self.assertEqual(self.event.featured_image_public_key, '')
        unused = StorageCleanup.objects.get(bucket=PUBLIC, reason='promotion_unused')
        self.assertEqual(unused.status, 'pending')
        uploads.process_cleanup()
        self.assertNotIn(unused.key, self.storage.keys(PUBLIC))

    def test_publish_fails_atomically_when_image_promotion_fails(self):
        self.ready()
        self.storage.fail['copy_if_match'] = StorageUnavailable('PROVIDER_UNAVAILABLE')
        self.error(self.client.post(f'/dawah/events/{self.event.pk}/publish/'), 503, 'PROVIDER_UNAVAILABLE')
        self.event.refresh_from_db()
        self.assertEqual((self.event.status, self.event.featured_image_public_key), (DRAFT, ''))
        del self.storage.fail['copy_if_match']
        self.publish()
        self.assertIsNotNone(self.event.featured_image_url)

    def test_publish_with_missing_validated_image_is_media_not_ready(self):
        self.ready()
        self.storage.delete(self.event.featured_image_object_key, bucket=PRIVATE)
        self.error(self.client.post(f'/dawah/events/{self.event.pk}/publish/'), 409, 'MEDIA_NOT_READY')
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, DRAFT)

    def test_event_delete_records_image_cleanup(self):
        self.ready()
        cache.clear()
        pending = self.upload(PNG, name='n.png', content_type='image/png')
        private_key = self.event.featured_image_object_key
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f'/dawah/events/{self.event.pk}/').status_code, 204)
        self.assertFalse(Event.objects.filter(pk=self.event.pk).exists())
        self.assertFalse(UploadIntent.objects.filter(pk=pending['upload_id']).exists())
        rows = StorageCleanup.objects.filter(event_id=self.event.pk, reason='event_deleted')
        self.assertIn(private_key, {r.key for r in rows})
        self.assertTrue(all(r.status == 'done' for r in rows))
        self.assertEqual(self.storage.keys(PRIVATE), set())


class FeaturedImageAltTests(ImageTestCase):
    def test_alt_text_patch_put_and_validation(self):
        url = f'/dawah/events/{self.event.pk}/'
        data = self.data(self.client.patch(url, {'featured_image_alt': '  Speaker at the mosque  '}, format='json'))
        self.assertEqual(data['featured_image_alt'], 'Speaker at the mosque')
        self.assertEqual(self.data(self.client.patch(url, {'title': 'Renamed'}, format='json'))['featured_image_alt'],
                         'Speaker at the mosque')
        self.assertEqual(self.data(self.client.patch(url, {'featured_image_alt': ''}, format='json'))
                         ['featured_image_alt'], '')
        self.data(self.client.patch(url, {'featured_image_alt': 'a' * 250}, format='json'))
        for value in ('a' * 251, '<img src=x onerror=alert(1)>', 'line\nbreak', 'tab\there', None):
            with self.subTest(value=value):
                fields = self.error(self.client.patch(url, {'featured_image_alt': value}, format='json'), 400,
                                    'VALIDATION_ERROR')['fields']
                self.assertIn('featured_image_alt', fields)
        created = self.data(self.client.post('/dawah/events/', {
            'title': 'T', 'category': 'jumuah', 'speakers': 's', 'location': 'l',
            'start_time': '2026-10-09T12:00:00Z', 'end_time': '2026-10-09T13:00:00Z',
            'featured_image_alt': 'Poster'}, format='json'), 201)
        self.assertEqual(created['featured_image_alt'], 'Poster')
        for field in ('featured_image_url', 'featured_image_storage_status', 'featured_image_uploaded_at',
                      'featured_image_preview_url'):
            with self.subTest(field=field):
                self.assertIn(field, self.error(self.client.patch(url, {field: 'x'}, format='json'), 400,
                                                'VALIDATION_ERROR')['fields'])

    def test_event_payload_shape(self):
        data = self.data(self.client.get(f'/dawah/events/{self.event.pk}/'))
        for field, value in (('featured_image_url', None), ('featured_image_preview_url', None),
                             ('featured_image_storage_status', 'none'), ('featured_image_uploaded_at', None),
                             ('featured_image_alt', '')):
            self.assertEqual(data[field], value)


class ImagePrivacyTests(ImageTestCase):
    def test_published_bytes_have_no_exif_gps_and_orientation_is_applied(self):
        import io

        from PIL import ExifTags, Image
        source = files.jpeg_with_metadata()
        self.assertTrue(Image.open(io.BytesIO(source)).getexif().get_ifd(ExifTags.IFD.GPSInfo))
        result = self.ready(source, name='phone.jpg')
        # Even the admin preview object is the derivative.
        self.assertNotIn(b'GPS', self.storage.objects['private'][self.event.featured_image_object_key]['data'])
        self.publish()
        status, body, headers = self.storage.public_get(self.event.featured_image_url)
        self.assertEqual((status, headers['Content-Type']), (200, 'image/jpeg'))
        for marker in (b'Exif\x00\x00', b'GPS', b'SecretCam', b'secret-comment', b'secret-xmp'):
            self.assertNotIn(marker, body)
        published = Image.open(io.BytesIO(body))
        self.assertEqual(dict(published.getexif()), {})
        self.assertEqual(published.size, (40, 60))  # orientation 6 applied
        self.assertEqual(self.event.featured_image_size_bytes, len(body))
        self.assertEqual(result['upload']['status'], 'ready')
        # The raw upload (with GPS) is not kept anywhere once cleanup ran.
        for bucket in ('private', 'public'):
            for data in (o['data'] for o in self.storage.objects[bucket].values()):
                self.assertNotIn(b'GPS', data)

    def test_replacement_on_published_event_publishes_only_the_derivative(self):
        self.publish()
        self.ready(files.jpeg_with_metadata(), name='phone.jpg')
        body = self.storage.public_get(self.event.featured_image_url)[1]
        self.assertNotIn(b'GPS', body)
        self.assertEqual(body, self.storage.objects['private'][self.event.featured_image_object_key]['data'])

    def test_derivative_write_failure_is_retryable_and_recorded(self):
        intent = self.upload()
        self.storage.fail['put_object'] = StorageUnavailable('PROVIDER_UNAVAILABLE')
        self.error(self.finalize(intent['upload_id']), 503, 'PROVIDER_UNAVAILABLE')
        row = StorageCleanup.objects.get(reason='derivative_failed')
        self.assertEqual((row.status, row.event_id), ('pending', self.event.pk))
        del self.storage.fail['put_object']
        self.assertEqual(self.finalized(intent['upload_id'])['upload']['status'], 'ready')


class FinalizeThrottleTests(ImageTestCase):
    def test_finalize_is_limited_per_admin(self):
        intent = self.upload()
        for _ in range(30):
            self.assertEqual(self.finalize(intent['upload_id']).status_code, 200)
        response = self.finalize(intent['upload_id'])
        self.error(response, 429, 'THROTTLED')
        self.assertIn('Retry-After', response)
        from .helpers import User
        other = User.objects.create_user(email='o2@example.test', password='Other-Pass-9876', first_name='O',
                                         last_name='A', role='admin')
        self.as_user(other)
        self.error(self.finalize(intent['upload_id']), 404, 'NOT_FOUND')  # separate budget
