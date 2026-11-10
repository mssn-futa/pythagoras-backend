import threading

from django.db import connection
from django.test import TransactionTestCase

# Import the API module first (pre-existing shared-package import order).
from dawah import views  # noqa: F401, I001
from dawah import uploads
from dawah.models import EventResource, UploadIntent
from dawah.storage import PRIVATE
from pythagoras.utils import APIError

from .helpers import PUBLISHED, FakeStorageMixin, User, make_event, make_resource, mp3_bytes


class BlockingReads:
    """Wraps open_stream so a finalize pauses mid-validation (no DB locks held)."""

    def __init__(self, storage):
        self.storage = storage
        self.original = storage.open_stream
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, key, **kwargs):
        self.entered.set()
        assert self.release.wait(20), 'test did not release the blocked read'
        return self.original(key, **kwargs)


class ConcurrentFinalizeTests(FakeStorageMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(email='admin@example.test', password='Admin-Pass-9876',
                                              first_name='Ada', last_name='Admin', role='admin')
        self.event = make_event(status=PUBLISHED)
        self.resource = make_resource(self.event)

    def staged_intent(self, data):
        intent, url = uploads.create_audio_intent(
            self.event.pk, self.resource.pk,
            {'file_name': 'k.mp3', 'content_type': 'audio/mpeg', 'size_bytes': len(data)}, self.admin)
        self.assertEqual(self.storage.client_put(url, data, content_type='audio/mpeg'), 200)
        return intent

    def run_in_thread(self, func, *args):
        outcome = {}

        def target():
            try:
                result = func(*args)
                outcome['status'] = result.intent.status
                outcome['error_code'] = result.intent.error_code
            except APIError as exc:
                outcome['error'] = exc.code
            finally:
                connection.close()

        thread = threading.Thread(target=target)
        thread.start()
        return thread, outcome

    def test_concurrent_finalize_of_one_intent_attaches_once(self):
        intent = self.staged_intent(mp3_bytes())
        blocker = BlockingReads(self.storage)
        self.storage.open_stream = blocker
        first, first_outcome = self.run_in_thread(uploads.finalize_upload, intent.pk, self.admin)
        self.assertTrue(blocker.entered.wait(10))
        # Second finalize while the first holds the lease (outside any row lock).
        second, second_outcome = self.run_in_thread(uploads.finalize_upload, intent.pk, self.admin)
        second.join(10)
        self.assertEqual(second_outcome, {'error': 'UPLOAD_IN_PROGRESS'})
        blocker.release.set()
        first.join(20)
        self.assertEqual(first_outcome, {'status': 'ready', 'error_code': ''})
        copies = [c for c in self.storage.calls if c[0] == 'copy_if_match']
        self.assertEqual(len(copies), 1)
        self.resource.refresh_from_db()
        intent.refresh_from_db()
        self.assertEqual(self.resource.private_object_key, intent.validation_key)
        # A later replay returns the saved outcome.
        self.assertEqual(uploads.finalize_upload(intent.pk, self.admin).intent.status, 'ready')

    def test_racing_intents_attach_only_the_newest_version(self):
        old_bytes, new_bytes = mp3_bytes(frames=100), mp3_bytes(frames=300)
        old = self.staged_intent(old_bytes)
        blocker = BlockingReads(self.storage)
        self.storage.open_stream = blocker
        old_thread, old_outcome = self.run_in_thread(uploads.finalize_upload, old.pk, self.admin)
        self.assertTrue(blocker.entered.wait(10))
        # A replacement upload is requested and finalized while the old one validates.
        self.storage.open_stream = blocker.original
        new = self.staged_intent(new_bytes)
        self.assertEqual(uploads.finalize_upload(new.pk, self.admin).intent.status, 'ready')
        blocker.release.set()
        old_thread.join(20)
        self.assertEqual(old_outcome, {'status': 'failed', 'error_code': 'SUPERSEDED'})
        self.resource.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(self.resource.private_object_key, new.validation_key)
        self.assertEqual(self.storage.objects[PRIVATE][self.resource.private_object_key]['data'], new_bytes)
        self.assertEqual(self.resource.size_bytes, len(new_bytes))
        self.assertEqual(UploadIntent.objects.filter(resource=self.resource, status='ready').count(), 1)

    def test_resource_deleted_during_validation_records_orphan(self):
        intent = self.staged_intent(mp3_bytes())
        blocker = BlockingReads(self.storage)
        self.storage.open_stream = blocker
        thread, outcome = self.run_in_thread(uploads.finalize_upload, intent.pk, self.admin)
        self.assertTrue(blocker.entered.wait(10))
        from dawah import services
        services.delete_resource(self.event.pk, self.resource.pk)
        blocker.release.set()
        thread.join(20)
        self.assertEqual(outcome, {'error': 'NOT_FOUND'})
        self.assertFalse(EventResource.objects.filter(pk=self.resource.pk).exists())
        from dawah.models import StorageCleanup
        self.assertTrue(StorageCleanup.objects.filter(key=intent.validation_key).exists())
        uploads.process_cleanup()
        self.assertNotIn(intent.validation_key, self.storage.keys(PRIVATE))
