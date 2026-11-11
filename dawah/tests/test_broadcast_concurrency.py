"""PostgreSQL row-lock behavior of the broadcast queue/edit transactions."""
import threading
import time

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase

# Import the API module first (pre-existing circular import, see test_concurrency).
from dawah import views  # noqa: F401, I001
from dawah import broadcasts
from dawah.broadcast_serializers import BroadcastWriteSerializer
from dawah.models import Broadcast, Delivery
from pythagoras.utils import APIError

User = get_user_model()


def run_in_thread(func, *args, barrier=None):
    outcome = {}

    def target():
        try:
            if barrier is not None:
                barrier.wait(5)
            outcome['value'] = func(*args)
        except APIError as exc:
            outcome['error'] = exc.code
        finally:
            connection.close()

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


class BroadcastLockTests(TransactionTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email='admin@example.test', password=None, role='admin')
        for index in range(5):
            User.objects.create_user(email=f's{index}@example.test', password=None, role='student')
        self.draft = Broadcast.objects.create(created_by=self.admin, subject='Hello', body='Body',
                                              audience_type='all')

    def test_concurrent_queue_creates_one_snapshot(self):
        barrier = threading.Barrier(4)
        runs = [run_in_thread(broadcasts.queue, self.draft.pk, 1, self.admin, barrier=barrier) for _ in range(4)]
        for thread, _ in runs:
            thread.join(20)
        outcomes = [outcome for _, outcome in runs]
        self.assertTrue(all('value' in outcome for outcome in outcomes), outcomes)
        self.assertEqual(sorted(outcome['value'][1] for outcome in outcomes), [False, True, True, True])
        self.assertEqual(Delivery.objects.filter(broadcast=self.draft).count(), 5)
        self.draft.refresh_from_db()
        self.assertEqual((self.draft.state, self.draft.recipient_count), ('queued', 5))

    def test_queue_waits_for_lock_then_replays(self):
        with transaction.atomic():
            broadcasts.queue(self.draft.pk, 1, self.admin)
            thread, outcome = run_in_thread(broadcasts.queue, self.draft.pk, 1, self.admin)
            time.sleep(0.5)
            self.assertTrue(thread.is_alive(), 'second queue should wait for the row lock')
        thread.join(10)
        self.assertIs(outcome['value'][1], True)
        self.assertEqual(Delivery.objects.count(), 5)

    def test_edit_racing_queue_sees_queued_state(self):
        def edit():
            return broadcasts.update_draft(self.draft.pk, 1, lambda locked: BroadcastWriteSerializer(
                locked, data={'subject': 'Changed', 'version': 1}, partial=True))

        with transaction.atomic():
            broadcasts.queue(self.draft.pk, 1, self.admin)
            thread, outcome = run_in_thread(edit)
            time.sleep(0.5)
            self.assertTrue(thread.is_alive())
        thread.join(10)
        self.assertEqual(outcome, {'error': 'BROADCAST_ALREADY_QUEUED'})
        self.draft.refresh_from_db()
        self.assertEqual((self.draft.subject, self.draft.snapshot_subject), ('Hello', 'Hello'))

    def test_queue_racing_edit_sees_new_version(self):
        with transaction.atomic():
            broadcasts.update_draft(self.draft.pk, 1, lambda locked: BroadcastWriteSerializer(
                locked, data={'subject': 'Edited', 'version': 1}, partial=True))
            thread, outcome = run_in_thread(broadcasts.queue, self.draft.pk, 1, self.admin)
            time.sleep(0.5)
            self.assertTrue(thread.is_alive())
        thread.join(10)
        self.assertEqual(outcome, {'error': 'BROADCAST_VERSION_CONFLICT'})
        self.assertEqual(Delivery.objects.count(), 0)

    def test_concurrent_edits_with_same_version(self):
        barrier = threading.Barrier(2)

        def edit(subject):
            return broadcasts.update_draft(self.draft.pk, 1, lambda locked: BroadcastWriteSerializer(
                locked, data={'subject': subject, 'version': 1}, partial=True))

        runs = [run_in_thread(edit, subject, barrier=barrier) for subject in ('A', 'B')]
        for thread, _ in runs:
            thread.join(10)
        outcomes = [outcome for _, outcome in runs]
        self.assertEqual(sorted('value' if 'value' in o else o['error'] for o in outcomes),
                         ['BROADCAST_VERSION_CONFLICT', 'value'])
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.version, 2)
