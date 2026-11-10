from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from dawah import storage
from dawah.models import Event, EventResource, PublicationStatus
from dawah.storage import PUBLIC
from dawah.storage import purge
from dawah.storage.memory import InMemoryObjectStorage


class RecordingPurger(purge.CachePurger):
    def __init__(self):
        self.purged = []
        self.error = None

    def purge(self, urls):
        if self.error is not None:
            raise self.error
        self.purged.extend(urls)

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


def mp3_bytes(frames=200):
    """Small valid MPEG-1 Layer III stream (128 kbps, 44.1 kHz), ~5 seconds."""
    frame = b'\xff\xfb\x90\x00' + b'\x00' * 413
    return b'ID3\x03\x00\x00\x00\x00\x00\x00' + frame * frames


_asset_counter = [0]


def make_resource(event, status=DRAFT, type='aud', ready=False, **fields):
    """``ready=True`` attaches a validated private asset stored in the active
    fake adapter (as finalize would); a published ready resource also gets a
    promoted public copy."""
    storage_status = EventResource.initial_storage_status(type)
    fields.setdefault('title', 'Patience')
    if ready:
        storage_status = EventResource.StorageStatus.READY
        _asset_counter[0] += 1
        key = f'media/audio/test-{_asset_counter[0]}.mp3'
        info = storage.get_storage().put_private(key, mp3_bytes())
        fields.update(private_object_key=key, etag=info['etag'], content_type='audio/mpeg',
                      size_bytes=len(info['data']), file_name='khutbah.mp3',
                      uploaded_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
        if status == PUBLISHED:
            public_key = f'audio/test-{_asset_counter[0]}/khutbah.mp3'
            fake = storage.get_storage()
            fake._store(PUBLIC, public_key, info['data'], 'audio/mpeg')
            fields.update(public_object_key=public_key, public_media_url=fake.public_url(public_key))
    return EventResource.objects.create(event=event, status=status, type=type, storage_status=storage_status,
                                        **fields)


def make_visible_resource(event, type='aud', **fields):
    """A ready, published, promoted asset (as the DAWAH-02 flow leaves it)."""
    return make_resource(event, status=PUBLISHED, type=type, ready=True, **fields)


class FakeStorageMixin:
    """Installs a fresh in-memory storage adapter for each test."""

    def setUp(self):
        super().setUp()
        self.storage = InMemoryObjectStorage()
        storage.set_storage(self.storage)
        self.addCleanup(storage.set_storage, None)
        self.purger = RecordingPurger()
        purge.set_purger(self.purger)
        self.addCleanup(purge.set_purger, None)


class DawahAPITestCase(FakeStorageMixin):
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
