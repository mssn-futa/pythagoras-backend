import threading
import time

from django.db import connection, transaction
from django.test import TransactionTestCase

# Import the API module first: importing pythagoras.utils before DRF's views
# module is loaded is a pre-existing circular import in the shared package.
from dawah import views  # noqa: F401, I001
from dawah import services
from dawah.models import EventResource
from pythagoras.utils import APIError

from .helpers import PUBLISHED, UNPUBLISHED, make_event, make_resource


class TransitionLockTests(TransactionTestCase):
    """Transitions lock the event row, so a racing request sees the committed state."""

    def run_blocked(self, func, *args):
        outcome = {}

        def target():
            try:
                outcome['value'] = func(*args)
            except APIError as exc:
                outcome['error'] = exc.code
            finally:
                connection.close()

        thread = threading.Thread(target=target)
        return thread, outcome

    def test_concurrent_publish_conflicts_instead_of_double_publishing(self):
        event = make_event()
        with transaction.atomic():
            services.publish_event(event.pk)  # holds the row lock until commit
            thread, outcome = self.run_blocked(services.publish_event, event.pk)
            thread.start()
            time.sleep(0.5)
            self.assertTrue(thread.is_alive(), 'second publish should wait for the row lock')
        thread.join(10)
        self.assertEqual(outcome, {'error': 'INVALID_STATE_TRANSITION'})

    def test_resource_publish_waits_for_parent_unpublish(self):
        event = make_event(status=PUBLISHED)
        resource = make_resource(event, ready=True)
        with transaction.atomic():
            services.unpublish_event(event.pk)
            thread, outcome = self.run_blocked(services.publish_resource, event.pk, resource.pk)
            thread.start()
            time.sleep(0.5)
            self.assertTrue(thread.is_alive())
        thread.join(10)
        self.assertEqual(outcome, {'error': 'EVENT_NOT_PUBLISHED'})
        resource.refresh_from_db()
        event.refresh_from_db()
        self.assertEqual((event.status, resource.status), (UNPUBLISHED, 'draft'))
        self.assertEqual(EventResource.objects.count(), 1)
