"""Real-lock races for featured images and documents (PostgreSQL, threads)."""
import threading

from django.db import connection
from django.test import TransactionTestCase

from dawah import views  # noqa: F401, I001  (pre-existing shared-package import order)
from dawah import services, uploads
from dawah.models import Event, UploadIntent
from dawah.storage import PRIVATE, PUBLIC
from pythagoras.utils import APIError

from . import files
from .helpers import PUBLISHED, FakeStorageMixin, User, make_event, make_resource
from .test_upload_concurrency import BlockingReads


def run_in_thread(func, *args):
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


class ImageRaceTests(FakeStorageMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(email='admin@example.test', password='Admin-Pass-9876',
                                              first_name='Ada', last_name='Admin', role='admin')
        self.event = make_event(status=PUBLISHED)

    def staged_image(self, data):
        intent, url = uploads.create_image_intent(
            self.event.pk, {'file_name': 'p.png', 'content_type': 'image/png', 'size_bytes': len(data)}, self.admin)
        self.assertEqual(self.storage.client_put(url, data, content_type='image/png'), 200)
        return intent

    def test_concurrent_finalize_attaches_and_promotes_once(self):
        intent = self.staged_image(files.image_bytes('PNG'))
        blocker = BlockingReads(self.storage)
        self.storage.open_stream = blocker
        first, first_outcome = run_in_thread(uploads.finalize_upload, intent.pk, self.admin)
        self.assertTrue(blocker.entered.wait(10))
        second, second_outcome = run_in_thread(uploads.finalize_upload, intent.pk, self.admin)
        second.join(10)
        self.assertEqual(second_outcome, {'error': 'UPLOAD_IN_PROGRESS'})
        blocker.release.set()
        first.join(20)
        self.assertEqual(first_outcome, {'status': 'ready', 'error_code': ''})
        public_copies = [c for c in self.storage.calls if c[0] == 'copy_if_match' and c[1]['dst_bucket'] == PUBLIC]
        self.assertEqual(len(public_copies), 1)
        self.event.refresh_from_db()
        intent.refresh_from_db()
        self.assertEqual(self.event.featured_image_object_key, intent.validation_key)
        self.assertEqual(self.storage.keys(PUBLIC), {self.event.featured_image_public_key})

    def test_racing_replacements_attach_only_the_newest(self):
        old_bytes, new_bytes = files.image_bytes('PNG', size=(10, 10)), files.image_bytes('PNG', size=(30, 20))
        old = self.staged_image(old_bytes)
        blocker = BlockingReads(self.storage)
        self.storage.open_stream = blocker
        thread, outcome = run_in_thread(uploads.finalize_upload, old.pk, self.admin)
        self.assertTrue(blocker.entered.wait(10))
        self.storage.open_stream = blocker.original
        new = self.staged_image(new_bytes)
        self.assertEqual(uploads.finalize_upload(new.pk, self.admin).intent.status, 'ready')
        blocker.release.set()
        thread.join(20)
        self.assertEqual(outcome, {'status': 'failed', 'error_code': 'SUPERSEDED'})
        self.event.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(self.event.featured_image_object_key, new.validation_key)
        self.assertEqual(self.storage.public_get(self.event.featured_image_url)[1], new_bytes)
        self.assertEqual(UploadIntent.objects.filter(event=self.event, status='ready').count(), 1)
        uploads.process_cleanup()
        self.assertEqual(self.storage.keys(PUBLIC), {self.event.featured_image_public_key})
        self.assertNotIn(old.validation_key, self.storage.keys(PRIVATE))

    def test_unpublish_during_validation_keeps_image_private(self):
        intent = self.staged_image(files.image_bytes('PNG'))
        blocker = BlockingReads(self.storage)
        self.storage.open_stream = blocker
        thread, outcome = run_in_thread(uploads.finalize_upload, intent.pk, self.admin)
        self.assertTrue(blocker.entered.wait(10))
        services.unpublish_event(self.event.pk)  # no row locks are held during validation
        blocker.release.set()
        thread.join(20)
        self.assertEqual(outcome['status'], 'ready')
        event = Event.objects.get(pk=self.event.pk)
        self.assertEqual((event.featured_image_public_key, event.featured_image_url), ('', None))
        self.assertEqual(self.storage.keys(PUBLIC), set())
        self.assertEqual(services.publish_event(self.event.pk).featured_image_url[:34],
                         'https://media.example.test/images/')

    def test_event_delete_during_validation_records_orphan(self):
        intent = self.staged_image(files.image_bytes('PNG'))
        blocker = BlockingReads(self.storage)
        self.storage.open_stream = blocker
        thread, outcome = run_in_thread(uploads.finalize_upload, intent.pk, self.admin)
        self.assertTrue(blocker.entered.wait(10))
        services.unpublish_event(self.event.pk)
        services.delete_event(self.event.pk)
        blocker.release.set()
        thread.join(20)
        self.assertEqual(outcome, {'error': 'NOT_FOUND'})
        uploads.process_cleanup()
        self.assertEqual(self.storage.keys(PRIVATE), set())
        self.assertEqual(self.storage.keys(PUBLIC), set())


class DocumentRaceTests(FakeStorageMixin, TransactionTestCase):
    def test_type_change_during_validation_fails_the_document_attach(self):
        admin = User.objects.create_user(email='admin@example.test', password='Admin-Pass-9876', first_name='A',
                                         last_name='A', role='admin')
        event = make_event(status=PUBLISHED)
        resource = make_resource(event, type='doc')
        pdf = files.pdf_bytes()
        intent, url = uploads.create_document_intent(
            event.pk, resource.pk, {'file_name': 'a.pdf', 'content_type': 'application/pdf', 'size_bytes': len(pdf)},
            admin)
        self.assertEqual(self.storage.client_put(url, pdf, content_type='application/pdf'), 200)
        blocker = BlockingReads(self.storage)
        self.storage.open_stream = blocker
        thread, outcome = run_in_thread(uploads.finalize_upload, intent.pk, admin)
        self.assertTrue(blocker.entered.wait(10))
        # Type changes meanwhile (bypassing the API's supersede): the validated PDF
        # must still not attach to a non-document resource.
        resource.type = 'aud'
        resource.save(update_fields=['type'])
        blocker.release.set()
        thread.join(20)
        self.assertEqual(outcome, {'status': 'failed', 'error_code': 'RESOURCE_TYPE_MISMATCH'})
        resource.refresh_from_db()
        self.assertEqual(resource.private_object_key, '')
