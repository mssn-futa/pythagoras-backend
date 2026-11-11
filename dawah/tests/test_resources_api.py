from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import ProtectedError
from django.test import TestCase

from dawah import services
from dawah.models import Event, EventResource, StorageCleanup
from quiz.models import Quiz

from .helpers import DRAFT, PUBLISHED, UNPUBLISHED, DawahAPITestCase, make_event, make_resource


def resources_url(event_id):
    return f'/dawah/events/{event_id}/resources/'


def resource_url(event_id, resource_id):
    return f'/dawah/events/{event_id}/resources/{resource_id}/'


class ResourceCrudTests(DawahAPITestCase, TestCase):
    def setUp(self):
        super().setUp()
        self.event = make_event()
        self.other = make_event(title='Other')
        self.as_admin()

    def test_create_draft_metadata_under_url_parent(self):
        data = self.data(self.client.post(resources_url(self.event.pk),
                                          {'title': 'Patience', 'type': 'aud', 'duration': 2100}, format='json'), 201)
        resource = EventResource.objects.get()
        self.assertEqual(data['id'], resource.pk)
        self.assertEqual(data['event_id'], self.event.pk)
        self.assertEqual(data['event']['id'], self.event.pk)
        self.assertEqual(data['status'], 'draft')
        self.assertEqual(data['storage_status'], 'none')
        self.assertIsNone(data['media_url'])
        self.assertIsNone(data['playback'])
        self.assertEqual(data['created_by'], self.admin.pk)
        self.assertIs(data['legacy_file_present'], False)
        self.assertEqual(resource.event, self.event)

    def test_video_and_document_storage_status(self):
        video = self.data(self.client.post(resources_url(self.event.pk), {'title': 'Lecture', 'type': 'vid'},
                                           format='json'), 201)
        self.assertEqual(video['storage_status'], 'not_applicable')
        doc = self.data(self.client.post(resources_url(self.event.pk), {'title': 'Agenda', 'type': 'doc'},
                                         format='json'), 201)
        self.assertEqual(doc['storage_status'], 'none')

    def test_create_validation(self):
        url = resources_url(self.event.pk)
        for payload, field in [({'type': 'aud'}, 'title'), ({'title': 'x'}, 'type'),
                               ({'title': 'x', 'type': 'mp3'}, 'type'),
                               ({'title': 'x', 'type': 'aud', 'duration': 0}, 'duration'),
                               ({'title': 'x', 'type': 'aud', 'duration': -5}, 'duration'),
                               ({'title': 'x', 'type': 'aud', 'event': self.other.pk}, 'event'),
                               ({'title': 'x', 'type': 'aud', 'status': 'published'}, 'status'),
                               ({'title': 'x', 'type': 'aud', 'storage_status': 'ready'}, 'storage_status')]:
            with self.subTest(payload=payload):
                fields = self.error(self.client.post(url, payload, format='json'), 400, 'VALIDATION_ERROR')['fields']
                self.assertIn(field, fields)
        self.assertFalse(EventResource.objects.exists())

    def test_file_bytes_are_not_accepted(self):
        upload = SimpleUploadedFile('khutbah.mp3', b'ID3' + b'\x00' * 32, content_type='audio/mpeg')
        response = self.client.post(resources_url(self.event.pk), {'title': 'x', 'type': 'aud', 'file': upload},
                                    format='multipart')
        fields = self.error(response, 400, 'VALIDATION_ERROR')['fields']
        self.assertEqual(fields, {'file': ['This field cannot be set or changed here.']})
        self.assertFalse(EventResource.objects.exists())

    def test_create_on_missing_event_is_404(self):
        self.error(self.client.post(resources_url(999), {'title': 'x', 'type': 'aud'}, format='json'),
                   404, 'NOT_FOUND')

    def test_patch_cannot_move_resource(self):
        resource = make_resource(self.event)
        for key in ('event', 'event_id'):
            with self.subTest(key=key):
                self.error(self.client.patch(resource_url(self.event.pk, resource.pk), {key: self.other.pk},
                                             format='json'), 400, 'VALIDATION_ERROR')
        resource.refresh_from_db()
        self.assertEqual(resource.event, self.event)
        # Wrong parent in the URL never reaches the resource.
        self.error(self.client.patch(resource_url(self.other.pk, resource.pk), {'title': 'x'}, format='json'),
                   404, 'NOT_FOUND')
        self.error(self.client.delete(resource_url(self.other.pk, resource.pk)), 404, 'NOT_FOUND')
        self.error(self.client.get(resource_url(self.other.pk, resource.pk)), 404, 'NOT_FOUND')
        self.assertTrue(EventResource.objects.filter(pk=resource.pk).exists())

    def test_patch_metadata_and_type_rules(self):
        resource = make_resource(self.event)
        url = resource_url(self.event.pk, resource.pk)
        data = self.data(self.client.patch(url, {'title': 'Renamed', 'duration': 60}, format='json'))
        self.assertEqual((data['title'], data['duration']), ('Renamed', 60))
        data = self.data(self.client.patch(url, {'type': 'vid'}, format='json'))
        self.assertEqual((data['type'], data['storage_status']), ('vid', 'not_applicable'))
        data = self.data(self.client.patch(url, {'type': 'doc'}, format='json'))
        self.assertEqual((data['type'], data['storage_status']), ('doc', 'none'))
        ready = make_resource(self.event, ready=True)
        self.error(self.client.patch(resource_url(self.event.pk, ready.pk), {'type': 'vid'}, format='json'),
                   400, 'VALIDATION_ERROR')
        published = make_resource(make_event(status=PUBLISHED), status=PUBLISHED, ready=True)
        self.error(self.client.patch(resource_url(published.event_id, published.pk), {'type': 'doc'},
                                     format='json'), 400, 'VALIDATION_ERROR')
        self.data(self.client.patch(resource_url(published.event_id, published.pk), {'title': 'Fixed typo'},
                                    format='json'))

    def test_put_is_not_supported(self):
        resource = make_resource(self.event)
        self.error(self.client.put(resource_url(self.event.pk, resource.pk), {'title': 'x', 'type': 'aud'},
                                   format='json'), 405, 'METHOD_NOT_ALLOWED')

    def test_delete_rules(self):
        draft = make_resource(self.event, file='event_resources/legacy.mp3')
        unpublished = make_resource(self.event, status=UNPUBLISHED, ready=True)
        published = make_resource(make_event(status=PUBLISHED), status=PUBLISHED, ready=True)
        response = self.client.delete(resource_url(self.event.pk, draft.pk))
        self.assertEqual((response.status_code, response.content), (204, b''))
        self.assertEqual(self.client.delete(resource_url(self.event.pk, unpublished.pk)).status_code, 204)
        self.error(self.client.delete(resource_url(published.event_id, published.pk)), 409, 'UNPUBLISH_REQUIRED')
        self.assertEqual(list(EventResource.objects.values_list('pk', flat=True)), [published.pk])

    def test_legacy_no_slash_resource_alias(self):
        resource = make_resource(self.event)
        url = f'/dawah/events/{self.event.pk}/resources/{resource.pk}'
        self.assertEqual(self.data(self.client.get(url))['id'], resource.pk)
        self.assertEqual(self.data(self.client.patch(url, {'title': 'Alias'}, format='json'))['title'], 'Alias')
        self.as_user(None)
        self.error(self.client.get(url), 404, 'NOT_FOUND')


class ResourcePermissionTests(DawahAPITestCase, TestCase):
    def test_write_permission_matrix(self):
        event = make_event(status=PUBLISHED)
        resource = make_resource(event, status=PUBLISHED, ready=True)
        draft = make_resource(event)
        requests = [
            ('post', resources_url(event.pk), {'title': 'x', 'type': 'aud'}),
            ('patch', resource_url(event.pk, resource.pk), {'title': 'x'}),
            ('delete', resource_url(event.pk, draft.pk), None),
            ('post', f'{resource_url(event.pk, draft.pk)}publish/', None),
            ('post', f'{resource_url(event.pk, resource.pk)}unpublish/', None),
        ]
        actors = [(None, 401, 'AUTHENTICATION_REQUIRED'), (self.student, 403, 'PERMISSION_DENIED'),
                  (self.staff_student, 403, 'PERMISSION_DENIED'), (self.inactive_admin, 403, 'ACCOUNT_INACTIVE')]
        for user, status, code in actors:
            self.as_user(user)
            for method, url, body in requests:
                with self.subTest(user=getattr(user, 'email', 'anonymous'), method=method, url=url):
                    self.error(getattr(self.client, method)(url, body, format='json'), status, code)
        self.assertEqual(EventResource.objects.count(), 2)
        resource.refresh_from_db()
        self.assertEqual((resource.title, resource.status), ('Patience', PUBLISHED))

    def test_read_visibility_matrix(self):
        event = make_event(status=PUBLISHED)
        visible = make_resource(event, status=PUBLISHED, ready=True)
        hidden = [
            make_resource(event),  # draft
            make_resource(event, status=DRAFT, ready=True),  # ready draft
            make_resource(event, status=UNPUBLISHED, ready=True),
            make_resource(event, status=PUBLISHED),  # no ready asset
            make_resource(event, status=PUBLISHED, type='vid'),  # no validated reference yet
        ]
        draft_parent = make_event()
        child_of_draft = make_resource(draft_parent, status=PUBLISHED, ready=True)
        for user in (None, self.student):
            self.as_user(user)
            with self.subTest(user=getattr(user, 'email', 'anonymous')):
                self.assertEqual(self.ids(self.client.get(resources_url(event.pk))), [visible.pk])
                self.assertEqual(self.ids(self.client.get(resources_url(event.pk), {'status': 'all'})), [visible.pk])
                self.data(self.client.get(resource_url(event.pk, visible.pk)))
                self.data(self.client.get(f'/dawah/resources/{visible.pk}/'))
                for resource in hidden:
                    self.error(self.client.get(resource_url(event.pk, resource.pk)), 404, 'NOT_FOUND')
                    self.error(self.client.get(f'/dawah/resources/{resource.pk}/'), 404, 'NOT_FOUND')
                self.error(self.client.get(resources_url(draft_parent.pk)), 404, 'NOT_FOUND')
                self.error(self.client.get(resource_url(draft_parent.pk, child_of_draft.pk)), 404, 'NOT_FOUND')
                self.error(self.client.get(f'/dawah/resources/{child_of_draft.pk}/'), 404, 'NOT_FOUND')
        self.as_admin()
        self.assertEqual(len(self.ids(self.client.get(resources_url(event.pk)))), 6)
        self.assertEqual(self.ids(self.client.get(resources_url(event.pk), {'status': 'unpublished'})),
                         [hidden[2].pk])
        self.assertEqual(self.ids(self.client.get(resources_url(event.pk), {'type': 'vid'})), [hidden[4].pk])
        self.assertEqual(self.data(self.client.get(f'/dawah/resources/{child_of_draft.pk}/'))['id'],
                         child_of_draft.pk)
        self.assertEqual(self.ids(self.client.get(resources_url(draft_parent.pk))), [child_of_draft.pk])

    def test_public_detail_payload(self):
        event = make_event(status=PUBLISHED)
        resource = make_resource(event, status=PUBLISHED, ready=True, duration=2100)
        data = self.data(self.client.get(f'/dawah/resources/{resource.pk}/'))
        self.assertEqual(set(data), {'id', 'event_id', 'title', 'type', 'status', 'storage_status', 'duration',
                                     'media_url', 'playback', 'content_type', 'size_bytes', 'file_name',
                                     'uploaded_at', 'preview_url', 'published_at', 'created_at', 'updated_at',
                                     'created_by', 'legacy_file_present', 'event'})
        self.assertEqual(data['file_name'], 'khutbah.mp3')
        self.assertIsNone(data['preview_url'])
        self.assertEqual(data['media_url'], resource.public_media_url)
        self.assertEqual(data['event'], {'id': event.pk, 'title': event.title, 'speakers': event.speakers,
                                         'category': event.category, 'start_time': '2026-10-09T12:00:00Z',
                                         'summary': event.summary, 'featured_image_url': None,
                                         'featured_image_alt': ''})
        self.assertNotIn('file', data)


class ResourcePublicationTests(DawahAPITestCase, TestCase):
    def setUp(self):
        super().setUp()
        self.as_admin()

    def publish(self, resource):
        return self.client.post(f'{resource_url(resource.event_id, resource.pk)}publish/')

    def unpublish(self, resource):
        return self.client.post(f'{resource_url(resource.event_id, resource.pk)}unpublish/')

    def test_audio_without_ready_asset_is_refused(self):
        event = make_event(status=PUBLISHED)
        for resource in (make_resource(event, type='aud'), make_resource(event, type='doc'),
                         make_resource(event, type='aud', file='event_resources/legacy.mp3')):
            with self.subTest(type=resource.type):
                self.error(self.publish(resource), 409, 'MEDIA_NOT_READY')
                resource.refresh_from_db()
                self.assertEqual(resource.status, DRAFT)

    def test_video_without_validated_reference_is_refused(self):
        resource = make_resource(make_event(status=PUBLISHED), type='vid')
        self.error(self.publish(resource), 409, 'MEDIA_NOT_READY')

    def test_parent_must_be_published(self):
        for status in (DRAFT, UNPUBLISHED):
            resource = make_resource(make_event(status=status), ready=True)
            with self.subTest(parent=status):
                self.error(self.publish(resource), 409, 'EVENT_NOT_PUBLISHED')
                resource.refresh_from_db()
                self.assertEqual(resource.status, DRAFT)

    def test_ready_asset_lifecycle(self):
        resource = make_resource(make_event(status=PUBLISHED), ready=True)
        data = self.data(self.publish(resource))
        self.assertEqual(data['status'], 'published')
        self.assertTrue(data['published_at'].endswith('Z'))
        self.error(self.publish(resource), 409, 'INVALID_STATE_TRANSITION')
        self.as_user(None)
        self.data(self.client.get(f'/dawah/resources/{resource.pk}/'))
        self.as_admin()
        self.assertEqual(self.data(self.unpublish(resource))['status'], 'unpublished')
        self.error(self.unpublish(resource), 409, 'INVALID_STATE_TRANSITION')
        self.as_user(None)
        self.error(self.client.get(f'/dawah/resources/{resource.pk}/'), 404, 'NOT_FOUND')
        self.as_admin()
        self.assertEqual(self.data(self.publish(resource))['status'], 'published')

    def test_unpublish_draft_and_wrong_parent(self):
        resource = make_resource(make_event(status=PUBLISHED))
        self.error(self.unpublish(resource), 409, 'INVALID_STATE_TRANSITION')
        other = make_event(status=PUBLISHED)
        self.error(self.client.post(f'{resource_url(other.pk, resource.pk)}publish/'), 404, 'NOT_FOUND')


class ResourceReferenceTests(DawahAPITestCase, TestCase):
    """Resources referenced by other domains (ASSESS-07 quiz.ModuleResource, PROTECT) are not deleted."""

    def setUp(self):
        super().setUp()
        self.event = make_event()
        self.resource = make_resource(self.event, ready=True)
        self.referrer = Quiz.objects.create(title='Module quiz', type='event', description='d', time_limit=10)
        self.as_admin()

    def protected(self):
        return ProtectedError('protected', {self.referrer})

    def test_protected_resource_delete_is_resource_referenced(self):
        url = f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/'
        with mock.patch.object(EventResource, 'delete', side_effect=self.protected()):
            data = self.error(self.client.delete(url), 409, 'RESOURCE_REFERENCED')
        self.assertEqual(data['fields'], {'references': {'quiz.quiz': [self.referrer.pk]}})
        self.assertTrue(EventResource.objects.filter(pk=self.resource.pk).exists())
        self.assertFalse(StorageCleanup.objects.exists())  # nothing recorded for a refused delete
        self.assertIn(self.resource.private_object_key, self.storage.keys('private'))
        self.assertEqual(self.client.delete(url).status_code, 204)

    def test_generic_precheck_reports_reverse_relations(self):
        url = f'/dawah/events/{self.event.pk}/resources/{self.resource.pk}/'
        with mock.patch('dawah.services.resource_references', return_value={'quiz.moduleresource': [4, 7]}):
            data = self.error(self.client.delete(url), 409, 'RESOURCE_REFERENCED')
            self.assertEqual(data['fields'], {'references': {'quiz.moduleresource': [4, 7]}})
            # Event deletion cascades into resources, so it reports their references too.
            data = self.error(self.client.delete(f'/dawah/events/{self.event.pk}/'), 409, 'RESOURCE_REFERENCED')
            self.assertEqual(data['fields'], {'references': {'quiz.moduleresource': [4, 7]}})
        self.assertEqual(services.resource_references(self.resource), {})  # intents are not references

    def test_event_delete_reports_protecting_rows_of_its_resources(self):

        with mock.patch.object(Event, 'delete', side_effect=self.protected()):
            data = self.error(self.client.delete(f'/dawah/events/{self.event.pk}/'), 409, 'RESOURCE_REFERENCED')
        self.assertEqual(data['fields'], {'references': {'quiz.quiz': [self.referrer.pk]}})
        self.assertTrue(EventResource.objects.filter(pk=self.resource.pk).exists())


class EventResourceReferenceScanTests(DawahAPITestCase, TestCase):
    """event_references includes incoming references to the event's resources (generic scan)."""

    def test_event_references_merge_resource_references(self):
        event = make_event()
        first, second = make_resource(event), make_resource(event)
        quiz = Quiz.objects.create(title='Q', type='event', description='d', related_event=event, time_limit=10)
        fake = {first.pk: {'quiz.moduleresource': [9, 3]}, second.pk: {'quiz.moduleresource': [3, 5]}}
        with mock.patch('dawah.services.resource_references', side_effect=lambda r: fake.get(r.pk, {})):
            self.assertEqual(services.event_references(event),
                             {'quiz.quiz': [quiz.pk], 'quiz.moduleresource': [3, 5, 9]})
            self.as_admin()
            data = self.error(self.client.delete(f'/dawah/events/{event.pk}/'), 409, 'RESOURCE_REFERENCED')
        self.assertEqual(data['fields']['references']['quiz.moduleresource'], [3, 5, 9])
        self.assertNotIn('quiz', self.client.delete(f'/dawah/events/{event.pk}/').json()['message'].lower())
        self.assertEqual(EventResource.objects.filter(event=event).count(), 2)
