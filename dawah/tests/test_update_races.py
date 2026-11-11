"""Overlapping writes on PostgreSQL: updates lock the current row (event, then
resource) and write only validated fields, so they never undo a concurrent
transition or resurrect a deleted row."""
import threading
import time

from django.db import connection, transaction
from django.test import TransactionTestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from dawah import views  # noqa: F401, I001  (load DRF before pythagoras.utils)
from dawah import services
from dawah.models import Event, EventResource

from .helpers import DRAFT, PUBLISHED, UNPUBLISHED, DawahAPITestCase, make_event, make_resource


class UpdateRaceTests(DawahAPITestCase, TransactionTestCase):
    def concurrent_request(self, method, url, body=None):
        """Start an admin request in another thread/connection; return it and its outcome."""
        outcome = {}
        token = str(AccessToken.for_user(self.admin))

        def target():
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
                response = getattr(client, method)(url, body, format='json')
                outcome['status'] = response.status_code
                outcome['body'] = response.json() if response.content else None
            finally:
                connection.close()

        thread = threading.Thread(target=target)
        thread.start()
        time.sleep(0.5)
        return thread, outcome

    def finish(self, thread):
        thread.join(15)
        self.assertFalse(thread.is_alive())

    def test_event_patch_waits_for_unpublish_and_keeps_it(self):
        event = make_event(status=PUBLISHED)
        with transaction.atomic():
            services.unpublish_event(event.pk)
            thread, outcome = self.concurrent_request('patch', f'/dawah/events/{event.pk}/', {'title': 'Edited'})
            self.assertTrue(thread.is_alive(), 'PATCH must wait for the event row lock')
        self.finish(thread)
        self.assertEqual(outcome['status'], 200)
        self.assertEqual(outcome['body']['data']['status'], 'unpublished')
        event.refresh_from_db()
        self.assertEqual((event.title, event.status), ('Edited', UNPUBLISHED))

    def test_event_patch_after_concurrent_delete_is_404_not_resurrection(self):
        event = make_event()
        with transaction.atomic():
            services.delete_event(event.pk)
            thread, outcome = self.concurrent_request('patch', f'/dawah/events/{event.pk}/', {'title': 'Edited'})
        self.finish(thread)
        self.assertEqual(outcome['status'], 404)
        self.assertFalse(Event.objects.filter(pk=event.pk).exists())

    def test_resource_patch_waits_for_unpublish_and_keeps_it(self):
        event = make_event(status=PUBLISHED)
        resource = make_resource(event, status=PUBLISHED, ready=True)
        url = f'/dawah/events/{event.pk}/resources/{resource.pk}/'
        with transaction.atomic():
            services.unpublish_resource(event.pk, resource.pk)
            thread, outcome = self.concurrent_request('patch', url, {'title': 'Edited'})
            self.assertTrue(thread.is_alive())
        self.finish(thread)
        self.assertEqual(outcome['status'], 200)
        resource.refresh_from_db()
        self.assertEqual((resource.title, resource.status, resource.storage_status),
                         ('Edited', UNPUBLISHED, 'ready'))

    def test_resource_patch_waits_for_event_lock_then_sees_delete(self):
        event = make_event()
        resource = make_resource(event)
        with transaction.atomic():
            services.delete_resource(event.pk, resource.pk)
            thread, outcome = self.concurrent_request(
                'patch', f'/dawah/events/{event.pk}/resources/{resource.pk}/', {'title': 'Edited'})
        self.finish(thread)
        self.assertEqual(outcome['status'], 404)
        self.assertFalse(EventResource.objects.filter(pk=resource.pk).exists())

    def test_resource_type_patch_after_publish_is_rejected_against_locked_row(self):
        event = make_event(status=PUBLISHED)
        resource = make_resource(event, ready=True)
        with transaction.atomic():
            services.publish_resource(event.pk, resource.pk)
            thread, outcome = self.concurrent_request(
                'patch', f'/dawah/events/{event.pk}/resources/{resource.pk}/', {'type': 'vid'})
        self.finish(thread)
        self.assertEqual(outcome['status'], 400)
        resource.refresh_from_db()
        self.assertEqual((resource.type, resource.status), ('aud', PUBLISHED))

    def test_resource_create_racing_event_delete_is_404(self):
        event = make_event(status=DRAFT)
        with transaction.atomic():
            services.delete_event(event.pk)
            thread, outcome = self.concurrent_request('post', f'/dawah/events/{event.pk}/resources/',
                                                      {'title': 'Late', 'type': 'aud'})
        self.finish(thread)
        self.assertEqual(outcome['status'], 404)
        self.assertEqual(outcome['body']['data']['code'], 'NOT_FOUND')
        self.assertFalse(EventResource.objects.exists())
