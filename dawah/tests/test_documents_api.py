"""Document resources (PDF/DOCX): intent, finalize, publish and visibility (DAWAH-07)."""
import json

from django.core.cache import cache
from django.test import TestCase

from dawah import uploads
from dawah.models import EventResource, StorageCleanup, UploadIntent
from dawah.storage import PRIVATE, PUBLIC

from . import files
from .files import DOCX_TYPE
from .helpers import PUBLISHED, DawahAPITestCase, User, make_event, make_resource, mp3_bytes

PDF = files.pdf_bytes()
DOCX = files.docx_bytes()


def finalize_url(upload_id):
    return f'/dawah/uploads/{upload_id}/finalize/'


class UploadFlowMixin:
    def finalize(self, upload_id):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(finalize_url(upload_id))

    def finalized(self, upload_id):
        return self.data(self.finalize(upload_id))

    def put(self, intent, data, content_type=None):
        status = self.storage.client_put(intent['upload_url'], data,
                                         content_type=content_type or intent['headers']['Content-Type'])
        self.assertEqual(status, 200)

    def assert_no_secrets(self, response_or_text):
        text = response_or_text if isinstance(response_or_text, str) else response_or_text.content.decode()
        for intent in UploadIntent.objects.all():
            self.assertNotIn(intent.staging_key, text)
            self.assertNotIn(intent.validation_key, text)
        self.assertNotIn('X-Amz-Signature', text)
        self.assertNotIn('fake-s3.invalid', text)
        self.assertNotIn('staging/', text)
        self.assertNotIn('media/documents', text)
        self.assertNotIn('media/images', text)


class DocumentTestCase(UploadFlowMixin, DawahAPITestCase, TestCase):
    def setUp(self):
        super().setUp()
        self.event = make_event(status=PUBLISHED)
        self.resource = make_resource(self.event, type='doc', title='Agenda')
        self.as_admin()

    def intent_url(self, resource=None):
        resource = resource or self.resource
        return f'/dawah/events/{resource.event_id}/resources/{resource.pk}/document/'

    def issue(self, data=PDF, name='Agenda (final).pdf', content_type='application/pdf', resource=None,
              status=201, **overrides):
        payload = {'file_name': name, 'content_type': content_type, 'size_bytes': len(data), **overrides}
        response = self.client.post(self.intent_url(resource), payload, format='json')
        return self.data(response, status) if status == 201 else response

    def upload(self, data=PDF, **kwargs):
        intent = self.issue(data, **kwargs)
        self.put(intent, data)
        return intent

    def ready(self, data=PDF, **kwargs):
        intent = self.upload(data, **kwargs)
        result = self.finalized(intent['upload_id'])
        self.assertEqual(result['upload']['status'], 'ready', result)
        self.resource.refresh_from_db()
        return result


class DocumentIntentTests(DocumentTestCase):
    def test_pdf_and_docx_intent_contract(self):
        for data, name, content_type, ext in ((PDF, 'agenda.pdf', 'application/pdf', '.pdf'),
                                              (DOCX, 'Bio.DOCX', DOCX_TYPE, '.docx')):
            with self.subTest(content_type):
                cache.clear()
                intent = self.issue(data, name=name, content_type=content_type)
                self.assertEqual(set(intent), {'upload_id', 'resource_id', 'event_id', 'method', 'upload_url',
                                               'headers', 'expires_at', 'max_size_bytes'})
                self.assertEqual((intent['resource_id'], intent['event_id'], intent['method'], intent['headers'],
                                  intent['max_size_bytes']),
                                 (self.resource.pk, self.event.pk, 'PUT', {'Content-Type': content_type},
                                  20 * 1024 * 1024))
                stored = UploadIntent.objects.get(pk=intent['upload_id'])
                self.assertEqual((stored.kind, stored.content_type, stored.event_id), ('document', content_type, None))
                self.assertTrue(stored.staging_key.startswith('staging/documents/'))
                self.assertTrue(stored.validation_key.startswith('media/documents/'))
                self.assertTrue(stored.validation_key.endswith(ext))
                self.assertTrue(stored.file_name.endswith(ext))
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.storage_status, 'pending')

    def test_field_validation(self):
        cases = [
            ({'content_type': 'application/msword'}, 'content_type'),
            ({'content_type': 'text/html'}, 'content_type'),
            ({'content_type': 'image/png'}, 'content_type'),
            ({'name': 'agenda.doc'}, 'file_name'),
            ({'name': 'agenda.exe'}, 'file_name'),
            ({'name': '.pdf'}, 'file_name'),
            ({'name': 'dir/agenda.pdf'}, 'file_name'),
            ({'name': 'agenda.docx'}, 'file_name'),  # extension does not match application/pdf
            ({'name': 'agenda.pdf', 'content_type': DOCX_TYPE}, 'file_name'),
            ({'size_bytes': 20 * 1024 * 1024 + 1}, 'size_bytes'),
            ({'size_bytes': 0}, 'size_bytes'),
        ]
        for overrides, field in cases:
            with self.subTest(overrides):
                cache.clear()
                response = self.issue(status=400, **overrides)
                self.assertIn(field, self.error(response, 400, 'VALIDATION_ERROR')['fields'])
        cache.clear()
        self.assertIn('bucket', self.error(self.issue(status=400, bucket='x'), 400, 'VALIDATION_ERROR')['fields'])
        self.assertFalse(UploadIntent.objects.exists())

    def test_wrong_resource_type_and_published_resource(self):
        audio = make_resource(self.event, type='aud')
        self.error(self.issue(resource=audio, status=409), 409, 'RESOURCE_TYPE_MISMATCH')
        video = make_resource(self.event, type='vid')
        self.error(self.issue(resource=video, status=409), 409, 'RESOURCE_TYPE_MISMATCH')
        # Audio intents refuse document resources too.
        response = self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/audio/',
                                    {'file_name': 'a.mp3', 'content_type': 'audio/mpeg', 'size_bytes': 10},
                                    format='json')
        self.error(response, 409, 'RESOURCE_TYPE_MISMATCH')
        self.ready()
        self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/publish/'))
        cache.clear()
        self.error(self.issue(status=409), 409, 'UNPUBLISH_REQUIRED')

    def test_permissions_and_parent_ownership(self):
        other = make_event()
        url = f'/dawah/events/{other.pk}/resources/{self.resource.pk}/document/'
        self.error(self.client.post(url, {}, format='json'), 404, 'NOT_FOUND')
        payload = {'file_name': 'a.pdf', 'content_type': 'application/pdf', 'size_bytes': 10}
        for user, status, code in ((None, 401, 'AUTHENTICATION_REQUIRED'), (self.student, 403, 'PERMISSION_DENIED'),
                                   (self.staff_student, 403, 'PERMISSION_DENIED')):
            self.as_user(user)
            self.error(self.client.post(self.intent_url(), payload, format='json'), status, code)
        self.assertFalse(UploadIntent.objects.exists())

    def test_throttle_is_shared_with_audio_and_images(self):
        audio = make_resource(self.event, type='aud')
        audio_payload = {'file_name': 'a.mp3', 'content_type': 'audio/mpeg', 'size_bytes': 10}
        image_payload = {'file_name': 'a.png', 'content_type': 'image/png', 'size_bytes': 10}
        for _ in range(2):
            self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{audio.pk}/audio/', audio_payload,
                                       format='json'), 201)
        self.data(self.client.post(f'/dawah/events/{self.event.pk}/featured-image/', image_payload, format='json'),
                  201)
        self.issue()
        self.issue()
        response = self.issue(status=429)
        self.error(response, 429, 'THROTTLED')
        self.assertIn('Retry-After', response)
        self.error(self.client.post(f'/dawah/events/{self.event.pk}/featured-image/', image_payload, format='json'),
                   429, 'THROTTLED')


class DocumentFinalizeTests(DocumentTestCase):
    def test_pdf_happy_path(self):
        intent = self.upload()
        result = self.finalized(intent['upload_id'])
        self.assertEqual(set(result), {'upload', 'resource', 'event', 'preview_url'})
        self.assertEqual((result['upload']['kind'], result['upload']['status'], result['event']),
                         ('document', 'ready', None))
        resource = result['resource']
        self.assertEqual((resource['type'], resource['storage_status'], resource['status'], resource['file_name'],
                          resource['content_type'], resource['size_bytes'], resource['media_url']),
                         ('doc', 'ready', 'draft', 'Agenda-final.pdf', 'application/pdf', len(PDF), None))
        self.assertTrue(resource['uploaded_at'].endswith('Z'))
        self.assertIsNone(resource['duration'])
        self.assertTrue(result['preview_url'].startswith('https://fake-s3.invalid/private/media/documents/'))
        preview_call = [kw for name, kw in self.storage.calls if name == 'presign_get'][-1]
        self.assertEqual((preview_call['content_type'], preview_call['disposition']),
                         ('application/pdf', 'attachment'))
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.private_object_key, stored.validation_key)
        self.assertEqual(self.storage.objects[PRIVATE][stored.validation_key]['data'], PDF)
        self.assertNotIn(stored.staging_key, self.storage.keys(PRIVATE))
        # Ready drafts stay private.
        self.as_user(None)
        self.error(self.client.get(f'/dawah/resources/{self.resource.pk}/'), 404, 'NOT_FOUND')

    def test_docx_happy_path(self):
        result = self.ready(DOCX, name='Speaker bio.docx', content_type=DOCX_TYPE)
        self.assertEqual((result['resource']['file_name'], result['resource']['content_type']),
                         ('Speaker-bio.docx', DOCX_TYPE))

    def test_hostile_and_mismatched_content_is_rejected(self):
        bomb = files.docx_bytes(extra={'word/media/zeros.bin': b'\x00' * (30 * 1024 * 1024)})
        cases = [
            (b'not a pdf at all, just text %%EOF', 'application/pdf', 'x.pdf', 'INVALID_PDF'),
            (PDF[:len(PDF) // 2], 'application/pdf', 'x.pdf', 'INVALID_PDF'),
            (DOCX, 'application/pdf', 'x.pdf', 'CONTENT_TYPE_MISMATCH'),
            (PDF, DOCX_TYPE, 'x.docx', 'CONTENT_TYPE_MISMATCH'),
            (bomb, DOCX_TYPE, 'x.docx', 'DOCX_LIMITS_EXCEEDED'),
            (files.docx_bytes(extra={'word/vbaProject.bin': b'x' * 64}), DOCX_TYPE, 'x.docx',
             'DOCX_MACROS_NOT_ALLOWED'),
            (files.docx_bytes(document_rels=[(f'{files.OFFICE_REL}/attachedTemplate', 'https://evil.test/t.dotm',
                                              ' TargetMode="External"')]), DOCX_TYPE, 'x.docx',
             'DOCX_EXTERNAL_CONTENT'),
            (files.zip_bytes({'a.txt': b'a'}), DOCX_TYPE, 'x.docx', 'INVALID_DOCX'),
        ]
        for data, content_type, name, code in cases:
            with self.subTest(code=code):
                cache.clear()
                intent = self.upload(data, name=name, content_type=content_type)
                result = self.finalized(intent['upload_id'])
                self.assertEqual((result['upload']['status'], result['upload']['error_code']), ('rejected', code))
                self.assertEqual(result['resource']['storage_status'], 'failed')
                self.assertIsNone(result['preview_url'])
                stored = UploadIntent.objects.get(pk=intent['upload_id'])
                self.assertNotIn(stored.validation_key, self.storage.keys(PRIVATE))  # cleaned after commit
                self.assertTrue(StorageCleanup.objects.filter(key=stored.validation_key, reason='upload_rejected',
                                                              status='done').exists())
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.private_object_key, '')

    def test_storage_observed_type_and_size_mismatch(self):
        intent = self.issue()
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.storage.put_private(stored.staging_key, PDF, content_type='text/html')
        self.assertEqual(self.finalized(intent['upload_id'])['upload']['error_code'], 'CONTENT_TYPE_MISMATCH')
        cache.clear()
        intent = self.issue()
        stored = UploadIntent.objects.get(pk=intent['upload_id'])
        self.storage.put_private(stored.staging_key, PDF + b'\n', content_type='application/pdf')
        self.assertEqual(self.finalized(intent['upload_id'])['upload']['error_code'], 'SIZE_MISMATCH')
        # A PUT through the signed URL with another type or size is refused by storage.
        cache.clear()
        intent = self.issue()
        self.assertEqual(self.storage.client_put(intent['upload_url'], PDF, content_type='text/html'), 403)
        self.assertEqual(self.storage.client_put(intent['upload_url'], PDF + b'x', content_type='application/pdf'),
                         403)

    def test_replay_and_staging_overwrite_after_finalize(self):
        intent = self.upload()
        first = self.finalized(intent['upload_id'])
        self.put(intent, files.pdf_bytes(pages=1, header=b'%PDF-1.5'))  # same size, still-valid PUT URL
        replay = self.finalized(intent['upload_id'])
        self.assertEqual(replay['upload'], first['upload'])
        self.assertEqual(replay['resource']['uploaded_at'], first['resource']['uploaded_at'])
        self.resource.refresh_from_db()
        self.assertEqual(self.storage.objects[PRIVATE][self.resource.private_object_key]['data'], PDF)

    def test_publish_promotes_with_attachment_disposition_and_unpublish_cleans_up(self):
        self.ready(DOCX, name='Bio.docx', content_type=DOCX_TYPE)
        url = f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/'
        published = self.data(self.client.post(url + 'publish/'))
        self.assertTrue(published['media_url'].startswith('https://media.example.test/documents/'))
        self.assertTrue(published['media_url'].endswith('/Bio.docx'))
        status, body, headers = self.storage.public_get(published['media_url'])
        self.assertEqual((status, body), (200, DOCX))
        self.assertEqual(headers['Content-Type'], DOCX_TYPE)
        self.assertEqual(headers['Content-Disposition'], 'attachment; filename="Bio.docx"')
        self.assertEqual(headers['Cache-Control'], 'public, max-age=31536000, immutable')

        self.as_user(None)
        public = self.data(self.client.get(f'/dawah/resources/{self.resource.pk}/'))
        self.assertEqual((public['type'], public['file_name'], public['content_type'], public['size_bytes'],
                          public['media_url'], public['preview_url']),
                         ('doc', 'Bio.docx', DOCX_TYPE, len(DOCX), published['media_url'], None))
        self.assert_no_secrets(self.client.get(f'/dawah/resources/{self.resource.pk}/'))
        archive = self.data(self.client.get('/dawah/archive/', {'type': 'doc'}))
        self.assertEqual([r['id'] for r in archive['results']], [self.resource.pk])
        self.assertEqual(self.data(self.client.get('/dawah/archive/', {'type': 'aud'}))['results'], [])

        self.as_admin()
        with self.captureOnCommitCallbacks(execute=True):
            self.data(self.client.post(url + 'unpublish/'))
        self.assertEqual(self.storage.public_get(published['media_url'])[0], 404)
        self.assertIn(published['media_url'], self.purger.purged)
        self.as_user(None)
        self.error(self.client.get(f'/dawah/resources/{self.resource.pk}/'), 404, 'NOT_FOUND')

    def test_pdf_public_copy_is_attachment_too(self):
        self.ready()
        published = self.data(self.client.post(
            f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/publish/'))
        headers = self.storage.public_get(published['media_url'])[2]
        self.assertEqual((headers['Content-Type'], headers['Content-Disposition']),
                         ('application/pdf', 'attachment; filename="Agenda-final.pdf"'))

    def test_document_without_asset_cannot_be_published(self):
        self.error(self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/publish/'), 409,
                   'MEDIA_NOT_READY')

    def test_replacing_a_draft_document_records_old_copy(self):
        self.ready()
        old_key = self.resource.private_object_key
        cache.clear()
        self.ready(DOCX, name='v2.docx', content_type=DOCX_TYPE)
        self.assertNotEqual(self.resource.private_object_key, old_key)
        self.assertEqual(self.resource.content_type, DOCX_TYPE)
        row = StorageCleanup.objects.get(key=old_key)
        self.assertEqual((row.reason, row.status, row.resource_id), ('replaced', 'done', self.resource.pk))
        self.assertNotIn(old_key, self.storage.keys(PRIVATE))

    def test_published_document_replaced_after_unpublish(self):
        self.ready()
        base = f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/'
        first = self.data(self.client.post(base + 'publish/'))['media_url']
        with self.captureOnCommitCallbacks(execute=True):
            self.data(self.client.post(base + 'unpublish/'))
        cache.clear()
        self.ready(DOCX, name='v2.docx', content_type=DOCX_TYPE)
        second = self.data(self.client.post(base + 'publish/'))['media_url']
        self.assertNotEqual(first, second)
        self.assertEqual(self.storage.public_get(second)[1], DOCX)

    def test_type_change_supersedes_open_document_intent(self):
        intent = self.upload()
        self.data(self.client.patch(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/',
                                    {'type': 'aud'}, format='json'))
        result = self.finalized(intent['upload_id'])
        self.assertEqual((result['upload']['status'], result['upload']['error_code']),
                         ('failed', 'RESOURCE_TYPE_CHANGED'))

    def test_event_republish_repromotes_documents(self):
        self.ready()
        base = f'/dawah/events/{self.event.pk}/'
        first = self.data(self.client.post(base + f'resources/{self.resource.pk}/publish/'))['media_url']
        with self.captureOnCommitCallbacks(execute=True):
            self.data(self.client.post(base + 'unpublish/'))
        self.assertEqual(self.storage.public_get(first)[0], 404)
        self.data(self.client.post(base + 'publish/'))
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.media_url.startswith('https://media.example.test/documents/'))
        self.assertNotEqual(self.resource.media_url, first)
        self.assertEqual(self.storage.public_get(self.resource.media_url)[1], PDF)

    def test_other_admins_cannot_finalize(self):
        intent = self.upload()
        other = User.objects.create_user(email='other@example.test', password='Other-Pass-9876', first_name='O',
                                         last_name='A', role='admin')
        self.as_user(other)
        self.error(self.finalize(intent['upload_id']), 404, 'NOT_FOUND')


class AudioRegressionTests(DocumentTestCase):
    """DAWAH-02 audio keeps its exact behavior through the generalized protocol."""

    def test_audio_flow_unchanged(self):
        audio = make_resource(self.event, type='aud')
        mp3 = mp3_bytes()
        intent = self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{audio.pk}/audio/',
                                            {'file_name': 'k.mp3', 'content_type': 'audio/mpeg',
                                             'size_bytes': len(mp3)}, format='json'), 201)
        self.assertEqual(intent['max_size_bytes'], 100 * 1024 * 1024)
        self.put(intent, mp3)
        result = self.finalized(intent['upload_id'])
        self.assertEqual((result['upload']['kind'], result['upload']['status'], result['resource']['duration'],
                          result['resource']['file_name']), ('audio', 'ready', 5, 'k.mp3'))
        published = self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{audio.pk}/publish/'))
        headers = self.storage.public_get(published['media_url'])[2]
        self.assertTrue(published['media_url'].startswith('https://media.example.test/audio/'))
        self.assertEqual(headers['Content-Disposition'], 'inline; filename="k.mp3"')
        # A PDF cannot be finalized as audio.
        cache.clear()
        other = make_resource(self.event, type='aud')
        intent = self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{other.pk}/audio/',
                                            {'file_name': 'k.mp3', 'content_type': 'audio/mpeg',
                                             'size_bytes': len(PDF)}, format='json'), 201)
        self.put(intent, PDF)
        self.assertEqual(self.finalized(intent['upload_id'])['upload']['error_code'], 'INVALID_MP3')

    def test_cleanup_command_expires_document_intents(self):
        intent = self.issue()
        UploadIntent.objects.filter(pk=intent['upload_id']).update(expires_at='2000-01-01T00:00:00Z')
        counts = uploads.expire_stale_intents()
        self.assertEqual(counts, {'expired': 1, 'abandoned': 0})
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.storage_status, 'failed')

    def test_logs_and_payload_have_no_keys(self):
        import logging

        from .test_uploads import CapturingHandler
        handler = CapturingHandler()
        logger = logging.getLogger('dawah.uploads')
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        result = self.ready()
        self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/publish/'))
        text = handler.text()
        for intent in UploadIntent.objects.all():
            self.assertNotIn(intent.staging_key, text)
            self.assertNotIn(intent.validation_key, text)
        self.assertNotIn('X-Amz', text)
        self.assertNotIn('staging/', json.dumps(result))
        # Only the admin preview (a short-lived signature) names the private object.
        self.assertNotIn(EventResource.objects.get(pk=self.resource.pk).private_object_key,
                         json.dumps({**result, 'preview_url': None, 'resource': {**result['resource'],
                                                                                 'preview_url': None}}))
        self.assertTrue(StorageCleanup.objects.filter(bucket=PUBLIC, status='cancelled').exists())


class PublicInterfaceTests(DocumentTestCase):
    """dawah.public: the safe resource retrieval other domains (Arabic modules) reuse."""

    def test_only_visible_resources_in_public_shape(self):
        from dawah import public
        self.ready()
        self.data(self.client.post(f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/publish/'))
        draft = make_resource(self.event, type='doc')
        hidden_parent = make_event()
        hidden = make_resource(hidden_parent, status=PUBLISHED, type='vid', youtube_video_id='abcdefghijk')
        ids = [self.resource.pk, draft.pk, hidden.pk, 999]
        self.assertEqual(public.visible_resource_ids(ids), {self.resource.pk})
        payloads = public.public_resource_payloads(public.visible_resources(ids))
        self.assertEqual(len(payloads), 1)
        self.as_user(None)
        detail = self.data(self.client.get(f'/dawah/resources/{self.resource.pk}/'))
        self.assertEqual(json.loads(json.dumps(payloads[0])), detail)
        self.assertIsNone(payloads[0]['preview_url'])
        self.assertIsNone(payloads[0]['created_by'])
