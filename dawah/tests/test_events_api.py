from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from dawah.models import Event, EventResource
from quiz.models import Quiz

from .helpers import DRAFT, PUBLISHED, UNPUBLISHED, DawahAPITestCase, at, event_payload, make_event, make_resource

EVENTS = '/dawah/events/'


def detail(event_id):
    return f'/dawah/events/{event_id}/'


class EventCrudTests(DawahAPITestCase, TestCase):
    def test_admin_creates_draft_event_with_ids_and_utc_timestamps(self):
        self.as_admin()
        data = self.data(self.client.post(EVENTS, event_payload(), format='json'), 201)
        event = Event.objects.get()
        self.assertEqual(data['id'], event.pk)
        self.assertEqual(data['status'], 'draft')
        self.assertIsNone(data['published_at'])
        self.assertEqual(data['created_by'], self.admin.pk)
        self.assertEqual(data['start_time'], '2026-10-09T12:00:00Z')
        self.assertEqual(data['end_time'], '2026-10-09T13:00:00Z')
        self.assertTrue(data['created_at'].endswith('Z'))
        self.assertIsNone(data['featured_image_url'])
        self.assertEqual(event.created_by, self.admin)
        self.assertEqual(event.status, DRAFT)

    def test_offset_input_is_normalized_to_utc(self):
        self.as_admin()
        data = self.data(self.client.post(EVENTS, event_payload(start_time='2026-10-09T13:00:00+01:00',
                                                                end_time='2026-10-09T14:00:00+01:00'),
                                          format='json'), 201)
        self.assertEqual(data['start_time'], '2026-10-09T12:00:00Z')

    def test_validation_errors_use_envelope(self):
        self.as_admin()
        cases = [
            (event_payload(end_time='2026-10-09T12:00:00Z'), 'end_time'),  # equal is invalid
            (event_payload(end_time='2026-10-09T11:00:00Z'), 'end_time'),
            (event_payload(title='   '), 'title'),
            (event_payload(start_time='2026-10-09T12:00:00'), 'start_time'),  # naive
            (event_payload(start_time='09-10-2026'), 'start_time'),
            ({k: v for k, v in event_payload().items() if k != 'speakers'}, 'speakers'),
        ]
        for payload, field in cases:
            with self.subTest(field=field, payload=payload):
                fields = self.error(self.client.post(EVENTS, payload, format='json'), 400, 'VALIDATION_ERROR')['fields']
                self.assertIn(field, fields)
        self.assertFalse(Event.objects.exists())

    def test_protected_fields_rejected(self):
        self.as_admin()
        for key, value in [('status', 'published'), ('id', 99), ('published_at', '2026-01-01T00:00:00Z'),
                           ('created_by', self.student.pk), ('resources', [{'title': 'x', 'type': 'aud'}]),
                           ('featured_image', 'x.png')]:
            with self.subTest(key=key):
                fields = self.error(self.client.post(EVENTS, event_payload(**{key: value}), format='json'),
                                    400, 'VALIDATION_ERROR')['fields']
                self.assertEqual(fields, {key: ['This field cannot be set or changed here.']})
        self.assertFalse(Event.objects.exists())

    def test_tags_optional(self):
        self.as_admin()
        payload = event_payload()
        del payload['tags']
        self.assertEqual(self.data(self.client.post(EVENTS, payload, format='json'), 201)['tags'], '')

    def test_admin_updates_with_put_and_patch(self):
        event = make_event()
        self.as_admin()
        data = self.data(self.client.patch(detail(event.pk), {'title': 'Renamed'}, format='json'))
        self.assertEqual(data['title'], 'Renamed')
        self.assertEqual(data['resources'], [])
        data = self.data(self.client.put(detail(event.pk), event_payload(title='Replaced'), format='json'))
        self.assertEqual(data['title'], 'Replaced')
        fields = self.error(self.client.put(detail(event.pk), {'title': 'Only'}, format='json'),
                            400, 'VALIDATION_ERROR')['fields']
        self.assertIn('start_time', fields)

    def test_patch_timing_validated_against_stored_values(self):
        event = make_event(start=at(2026, 10, 9, 12))
        self.as_admin()
        fields = self.error(self.client.patch(detail(event.pk), {'end_time': '2026-10-09T11:59:00Z'}, format='json'),
                            400, 'VALIDATION_ERROR')['fields']
        self.assertEqual(fields, {'end_time': ['End time must be after start time.']})
        self.error(self.client.patch(detail(event.pk), {'start_time': '2026-10-09T13:00:00Z'}, format='json'),
                   400, 'VALIDATION_ERROR')
        self.error(self.client.patch(detail(event.pk), {'status': 'published'}, format='json'),
                   400, 'VALIDATION_ERROR')
        event.refresh_from_db()
        self.assertEqual(event.status, DRAFT)

    def test_update_missing_event_is_404(self):
        self.as_admin()
        self.error(self.client.patch(detail(999), {'title': 'x'}, format='json'), 404, 'NOT_FOUND')

    def test_legacy_no_slash_detail_alias(self):
        event = make_event(status=PUBLISHED)
        response = self.client.get(f'/dawah/events/{event.pk}')
        self.assertEqual(self.data(response)['id'], event.pk)
        self.as_admin()
        self.assertEqual(self.data(self.client.patch(f'/dawah/events/{event.pk}', {'title': 'Alias'},
                                                     format='json'))['title'], 'Alias')
        self.as_user(None)
        self.error(self.client.get(f'/dawah/events/{make_event().pk}'), 404, 'NOT_FOUND')


class EventPermissionTests(DawahAPITestCase, TestCase):
    def test_write_permission_matrix(self):
        event = make_event()
        published = make_event(status=PUBLISHED)
        requests = [
            ('post', EVENTS, event_payload()),
            ('put', detail(event.pk), event_payload()),
            ('patch', detail(event.pk), {'title': 'x'}),
            ('delete', detail(event.pk), None),
            ('post', f'{detail(event.pk)}publish/', None),
            ('post', f'{detail(published.pk)}unpublish/', None),
            ('patch', detail(999), {'title': 'x'}),  # permission is checked before lookup
        ]
        actors = [(None, 401, 'AUTHENTICATION_REQUIRED'), (self.student, 403, 'PERMISSION_DENIED'),
                  (self.staff_student, 403, 'PERMISSION_DENIED'), (self.inactive_admin, 403, 'ACCOUNT_INACTIVE')]
        for user, status, code in actors:
            self.as_user(user)
            for method, url, body in requests:
                with self.subTest(user=getattr(user, 'email', 'anonymous'), method=method, url=url):
                    self.error(getattr(self.client, method)(url, body, format='json'), status, code)
        self.assertEqual(Event.objects.count(), 2)
        event.refresh_from_db()
        published.refresh_from_db()
        self.assertEqual((event.title, event.status, published.status), ('Friday khutbah', DRAFT, PUBLISHED))

    def test_read_visibility_matrix(self):
        draft = make_event(title='Draft')
        unpublished = make_event(status=UNPUBLISHED, title='Hidden')
        published = make_event(status=PUBLISHED, title='Public')
        for user in (None, self.student, self.staff_student):
            self.as_user(user)
            with self.subTest(user=getattr(user, 'email', 'anonymous')):
                self.assertEqual(self.ids(self.client.get(EVENTS)), [published.pk])
                self.assertEqual(self.ids(self.client.get(EVENTS, {'status': 'all'})), [published.pk])
                self.assertEqual(self.ids(self.client.get(EVENTS, {'status': 'draft'})), [published.pk])
                self.data(self.client.get(detail(published.pk)))
                self.error(self.client.get(detail(draft.pk)), 404, 'NOT_FOUND')
                self.error(self.client.get(detail(unpublished.pk)), 404, 'NOT_FOUND')
                # Non-admin status values are ignored, never an error.
                self.assertEqual(self.ids(self.client.get(EVENTS, {'status': 'bogus'})), [published.pk])
                self.assertEqual(self.ids(self.client.get(f'/dawah/events/{published.pk}/resources/',
                                                          {'status': 'bogus'})), [])
        self.as_user(self.inactive_admin)
        self.error(self.client.get(EVENTS), 403, 'ACCOUNT_INACTIVE')
        self.as_admin()
        self.assertEqual(self.ids(self.client.get(EVENTS)), [published.pk])
        self.assertEqual(sorted(self.ids(self.client.get(EVENTS, {'status': 'all'}))),
                         sorted([draft.pk, unpublished.pk, published.pk]))
        self.assertEqual(self.ids(self.client.get(EVENTS, {'status': 'draft'})), [draft.pk])
        self.assertEqual(self.ids(self.client.get(EVENTS, {'status': 'unpublished'})), [unpublished.pk])
        self.assertEqual(self.data(self.client.get(detail(draft.pk)))['status'], 'draft')
        fields = self.error(self.client.get(EVENTS, {'status': 'bogus'}), 400, 'VALIDATION_ERROR')['fields']
        self.assertIn('status', fields)

    def test_admin_only_fields_null_for_public_readers(self):
        event = make_event(status=PUBLISHED, created_by=self.admin)
        make_resource(event, status=PUBLISHED, ready=True, file='event_resources/legacy.mp3')
        public = self.data(self.client.get(detail(event.pk)))
        self.assertIsNone(public['created_by'])
        self.assertIsNone(public['resources'][0]['legacy_file_present'])
        self.as_admin()
        admin = self.data(self.client.get(detail(event.pk)))
        self.assertEqual(admin['created_by'], self.admin.pk)
        self.assertIs(admin['resources'][0]['legacy_file_present'], True)

    def test_detail_resources_respect_visibility(self):
        event = make_event(status=PUBLISHED)
        visible = make_resource(event, status=PUBLISHED, ready=True)
        make_resource(event)  # draft
        make_resource(event, status=PUBLISHED)  # published but no ready asset (legacy state)
        make_resource(event, status=UNPUBLISHED, ready=True)
        self.assertEqual([r['id'] for r in self.data(self.client.get(detail(event.pk)))['resources']], [visible.pk])
        self.as_admin()
        self.assertEqual(len(self.data(self.client.get(detail(event.pk)))['resources']), 4)


class EventPublicationTests(DawahAPITestCase, TestCase):
    def setUp(self):
        super().setUp()
        self.as_admin()

    def publish(self, event):
        return self.client.post(f'{detail(event.pk)}publish/')

    def unpublish(self, event):
        return self.client.post(f'{detail(event.pk)}unpublish/')

    def test_lifecycle_draft_published_unpublished_published(self):
        event = make_event()
        data = self.data(self.publish(event))
        self.assertEqual(data['status'], 'published')
        first_published_at = data['published_at']
        self.assertTrue(first_published_at.endswith('Z'))
        self.as_user(None)
        self.assertEqual(self.ids(self.client.get(EVENTS)), [event.pk])
        self.as_admin()
        self.assertEqual(self.data(self.unpublish(event))['status'], 'unpublished')
        self.as_user(None)
        self.assertEqual(self.ids(self.client.get(EVENTS)), [])
        self.error(self.client.get(detail(event.pk)), 404, 'NOT_FOUND')
        self.as_admin()
        data = self.data(self.publish(event))
        self.assertEqual(data['status'], 'published')
        self.assertGreaterEqual(data['published_at'], first_published_at)

    def test_invalid_transitions(self):
        draft = make_event()
        published = make_event(status=PUBLISHED)
        unpublished = make_event(status=UNPUBLISHED)
        self.error(self.publish(published), 409, 'INVALID_STATE_TRANSITION')
        self.error(self.unpublish(draft), 409, 'INVALID_STATE_TRANSITION')
        self.error(self.unpublish(unpublished), 409, 'INVALID_STATE_TRANSITION')
        self.error(self.client.post('/dawah/events/999/publish/'), 404, 'NOT_FOUND')

    def test_publish_requires_valid_stored_metadata(self):
        # Legacy rows were never validated (the old timing check never ran).
        event = make_event(duration=timedelta(0), location='')
        data = self.error(self.publish(event), 409, 'EVENT_NOT_PUBLISHABLE')
        self.assertEqual(set(data['fields']), {'end_time', 'location'})
        event.refresh_from_db()
        self.assertEqual(event.status, DRAFT)

    def test_unpublish_hides_resources_without_rewriting_their_state(self):
        event = make_event(status=PUBLISHED)
        resource = make_resource(event, status=PUBLISHED, ready=True)
        self.data(self.unpublish(event))
        resource.refresh_from_db()
        self.assertEqual(resource.status, PUBLISHED)
        self.as_user(None)
        self.error(self.client.get(f'/dawah/resources/{resource.pk}/'), 404, 'NOT_FOUND')
        self.error(self.client.get(f'{detail(event.pk)}resources/'), 404, 'NOT_FOUND')
        self.assertEqual(self.data(self.client.get('/dawah/archive/'))['count'], 0)
        self.as_admin()
        self.data(self.publish(event))
        self.as_user(None)
        self.data(self.client.get(f'/dawah/resources/{resource.pk}/'))


class EventDeletionTests(DawahAPITestCase, TestCase):
    def setUp(self):
        super().setUp()
        self.as_admin()

    def test_delete_draft_event_with_nonpublished_resources(self):
        event = make_event()
        make_resource(event, file='event_resources/legacy.mp3')
        response = self.client.delete(detail(event.pk))
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b'')
        self.assertFalse(Event.objects.exists())
        self.assertFalse(EventResource.objects.exists())
        self.error(self.client.delete(detail(event.pk)), 404, 'NOT_FOUND')

    def test_published_event_requires_unpublication(self):
        event = make_event(status=PUBLISHED)
        self.error(self.client.delete(detail(event.pk)), 409, 'UNPUBLISH_REQUIRED')
        self.assertTrue(Event.objects.filter(pk=event.pk).exists())

    def test_published_resource_blocks_event_deletion(self):
        event = make_event(status=UNPUBLISHED)
        make_resource(event, status=PUBLISHED, ready=True)
        self.error(self.client.delete(detail(event.pk)), 409, 'UNPUBLISH_REQUIRED')
        self.assertEqual(EventResource.objects.count(), 1)

    def test_quiz_reference_returns_resource_referenced(self):
        event = make_event(status=UNPUBLISHED)
        quiz = Quiz.objects.create(title='Khutbah quiz', type='event', description='Recap', related_event=event,
                                   time_limit=10)
        data = self.error(self.client.delete(detail(event.pk)), 409, 'RESOURCE_REFERENCED')
        self.assertEqual(data['fields'], {'references': {'quiz.quiz': [quiz.pk]}})
        self.assertTrue(Event.objects.filter(pk=event.pk).exists())
        self.assertTrue(Quiz.objects.filter(pk=quiz.pk).exists())
        quiz.related_event = None
        quiz.save()
        self.assertEqual(self.client.delete(detail(event.pk)).status_code, 204)
        self.assertTrue(Quiz.objects.filter(pk=quiz.pk).exists())

    def test_database_protection_is_reported_as_referenced(self):
        """Once ASSESS-02 makes the FK PROTECT, a race past the check still conflicts."""
        from django.db.models import ProtectedError

        event = make_event()
        with mock.patch('dawah.services.event_references', return_value={}), \
                mock.patch.object(Event, 'delete', side_effect=ProtectedError('protected', set())):
            self.error(self.client.delete(detail(event.pk)), 409, 'RESOURCE_REFERENCED')


class EventListContractTests(DawahAPITestCase, TestCase):
    def test_default_order_and_pagination(self):
        start = at(2026, 1, 1)
        events = [make_event(status=PUBLISHED, start=start + timedelta(days=i % 3)) for i in range(25)]
        expected = [e.pk for e in sorted(events, key=lambda e: (e.start_time, e.pk), reverse=True)]
        page1 = self.data(self.client.get(EVENTS))
        self.assertEqual(page1['count'], 25)
        self.assertEqual(len(page1['results']), 20)
        self.assertIsNotNone(page1['next'])
        self.assertIsNone(page1['previous'])
        page2 = self.data(self.client.get(EVENTS, {'page': 2}))
        self.assertEqual([e['id'] for e in page1['results'] + page2['results']], expected)
        self.assertEqual(len(self.data(self.client.get(EVENTS, {'page_size': 5}))['results']), 5)
        self.assertEqual(len(self.data(self.client.get(EVENTS, {'page_size': 500}))['results']), 25)
        self.error(self.client.get(EVENTS, {'page': 9}), 404, 'NOT_FOUND')

    def test_max_page_size_is_100(self):
        Event.objects.bulk_create([
            Event(title=f'E{i}', category='c', speakers='s', location='l', status=PUBLISHED,
                  start_time=at(2026, 1, 1), end_time=at(2026, 1, 1, 13)) for i in range(105)])
        self.assertEqual(len(self.data(self.client.get(EVENTS, {'page_size': 1000}))['results']), 100)

    def test_upcoming(self):
        now = timezone.now()
        past = make_event(status=PUBLISHED, start=now - timedelta(days=2))
        ongoing = make_event(status=PUBLISHED, start=now - timedelta(minutes=30))
        later = make_event(status=PUBLISHED, start=now + timedelta(days=2))
        soon_b = make_event(status=PUBLISHED, start=now + timedelta(days=1))
        soon_a = make_event(status=PUBLISHED, start=soon_b.start_time)
        make_event(start=now + timedelta(days=1))  # draft
        ended_exactly = make_event(status=PUBLISHED, start=now - timedelta(hours=2), duration=timedelta(hours=1))
        ids = self.ids(self.client.get(EVENTS, {'upcoming': 'true'}))
        self.assertEqual(ids, [ongoing.pk, soon_b.pk, soon_a.pk, later.pk])
        self.assertNotIn(past.pk, ids)
        self.assertNotIn(ended_exactly.pk, ids)
        self.assertEqual(len(self.ids(self.client.get(EVENTS, {'upcoming': 'false'}))), 6)
        self.error(self.client.get(EVENTS, {'upcoming': 'soon'}), 400, 'VALIDATION_ERROR')

    def test_search_speaker_category_and_dates(self):
        a = make_event(status=PUBLISHED, title='Patience in hardship', speakers='Imam Bello', tags='',
                       category='jumuah', start=at(2026, 3, 6))
        b = make_event(status=PUBLISHED, title='Night lecture', tags='family, patience',
                       speakers='Dr. Musa', category='dawah_night', start=at(2026, 3, 7))
        c = make_event(status=PUBLISHED, title='Usroh', summary='Brotherhood', speakers='imam bello', tags='',
                       category='usroh', start=at(2026, 4, 1))
        self.assertEqual(self.ids(self.client.get(EVENTS, {'search': 'PATIENCE'})), [b.pk, a.pk])
        self.assertEqual(self.ids(self.client.get(EVENTS, {'search': 'brother'})), [c.pk])
        self.assertEqual(self.ids(self.client.get(EVENTS, {'speaker': 'IMAM'})), [c.pk, a.pk])
        self.assertEqual(self.ids(self.client.get(EVENTS, {'category': 'JUMUAH'})), [a.pk])
        self.assertEqual(self.ids(self.client.get(EVENTS, {'date_from': '2026-03-07', 'date_to': '2026-03-31'})),
                         [b.pk])


class DjangoAdminTests(TestCase):
    def test_django_admin_cannot_delete_or_publish(self):
        from django.contrib import admin as django_admin

        for model in (Event, EventResource):
            model_admin = django_admin.site._registry[model]
            self.assertFalse(model_admin.has_delete_permission(None))
            self.assertIn('status', model_admin.readonly_fields)
        inline = django_admin.site._registry[Event].inlines[0]
        self.assertIn('type', inline.readonly_fields)
        self.assertFalse(inline(Event, django_admin.site).has_add_permission(None))
        self.assertFalse(django_admin.site._registry[EventResource].has_add_permission(None))

    def test_django_admin_event_form_validates_timing(self):
        from dawah.admin import EventAdminForm

        event = make_event()
        data = {'title': 'T', 'category': 'c', 'tags': '', 'speakers': 's', 'location': 'l',
                'start_time': '2026-10-09 12:00:00', 'end_time': '2026-10-09 12:00:00'}
        form = EventAdminForm(data, instance=event)
        self.assertFalse(form.is_valid())
        self.assertIn('end_time', form.errors)
        data['end_time'] = '2026-10-09 13:00:00'
        self.assertTrue(EventAdminForm(data, instance=event).is_valid())
