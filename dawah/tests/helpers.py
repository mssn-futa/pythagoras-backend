from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from dawah.models import Event, EventResource, PublicationStatus

User = get_user_model()
UTC = timezone.utc
PUBLISHED = PublicationStatus.PUBLISHED
DRAFT = PublicationStatus.DRAFT
UNPUBLISHED = PublicationStatus.UNPUBLISHED


def at(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def event_payload(**overrides):
    payload = {
        'title': 'Friday khutbah', 'category': 'jumuah', 'tags': 'patience', 'speakers': 'Guest speaker',
        'location': 'FUTA mosque', 'start_time': '2026-10-09T12:00:00Z', 'end_time': '2026-10-09T13:00:00Z',
        'summary': 'Key takeaways', 'description': 'Friday session',
    }
    payload.update(overrides)
    return payload


def make_event(status=DRAFT, start=None, duration=timedelta(hours=1), **fields):
    start = start or at(2026, 10, 9)
    defaults = {'title': 'Friday khutbah', 'category': 'jumuah', 'tags': 'patience',
                'speakers': 'Guest speaker', 'location': 'FUTA mosque', 'summary': 'Key takeaways'}
    defaults.update(fields)
    return Event.objects.create(status=status, start_time=start, end_time=start + duration, **defaults)


def make_resource(event, status=DRAFT, type='aud', ready=False, **fields):
    storage = EventResource.initial_storage_status(type)
    if ready:
        storage = EventResource.StorageStatus.READY
    fields.setdefault('title', 'Patience')
    return EventResource.objects.create(event=event, status=status, type=type, storage_status=storage, **fields)


def make_visible_resource(event, type='aud', **fields):
    """Simulates a DAWAH-02 ready, published asset (no upload path exists yet)."""
    return make_resource(event, status=PUBLISHED, type=type, ready=True, **fields)


class DawahAPITestCase:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create_user(email='admin@example.test', password='Admin-Pass-9876',
                                              first_name='Ada', last_name='Admin', role='admin')
        self.student = User.objects.create_user(email='student@example.test', password='Student-Pass-9876',
                                                first_name='Sam', last_name='Student', role='student')
        self.inactive_admin = User.objects.create_user(email='gone@example.test', password='Gone-Pass-9876',
                                                       first_name='Gone', last_name='Admin', role='admin')
        self.inactive_admin.is_active = False
        self.inactive_admin.save()
        self.staff_student = User.objects.create_user(email='staff@example.test', password='Staff-Pass-9876',
                                                      first_name='Staff', last_name='Student', role='student',
                                                      is_staff=True)

    def as_user(self, user):
        if user is None:
            self.client.credentials()
        else:
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')

    def as_admin(self):
        self.as_user(self.admin)

    def data(self, response, status=200):
        self.assertEqual(response.status_code, status, response.content)
        body = response.json()
        self.assertIs(body['success'], True)
        self.assertIsInstance(body['message'], str)
        return body['data']

    def error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        body = response.json()
        self.assertEqual(set(body), {'success', 'data', 'message'})
        self.assertIs(body['success'], False)
        self.assertEqual(body['data']['code'], code)
        return body['data']

    def ids(self, response):
        return [item['id'] for item in self.data(response)['results']]
