"""PostgreSQL behavior of the automation tick: concurrent passes, the unique
occurrence constraint and the rule/event lock order (COMM-06)."""
import threading
import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.db import IntegrityError, connection, transaction
from django.test import TransactionTestCase

# Import the API module first (pre-existing circular import, see test_concurrency).
from dawah import views  # noqa: F401, I001
from dawah import automations, services
from dawah.models import AutomationRule, AutomationRun, Broadcast, Delivery, Event
from dawah.serializers import EventWriteSerializer

from .helpers import PUBLISHED, User, make_event
from .test_automations import MONDAY_8_LAGOS, rule

UTC = dt_timezone.utc


def in_thread(func, *args, barrier=None):
    outcome = {}

    def target():
        try:
            if barrier is not None:
                barrier.wait(5)
            outcome['value'] = func(*args)
        except Exception as exc:  # surfaced by the assertions
            outcome['error'] = exc
        finally:
            connection.close()

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


class AutomationTickConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email='admin@example.test', password=None, role='admin')
        for index in range(3):
            User.objects.create_user(email=f's{index}@example.test', password=None, role='student')

    def test_concurrent_ticks_create_one_run_and_one_broadcast(self):
        weekly = rule(owner=self.admin)
        reminder = rule(kind='event_reminder', owner=self.admin)
        make_event(status=PUBLISHED, start=MONDAY_8_LAGOS - timedelta(days=2))
        make_event(status=PUBLISHED, start=MONDAY_8_LAGOS + timedelta(days=1, minutes=5))
        now = MONDAY_8_LAGOS + timedelta(minutes=6)
        barrier = threading.Barrier(4)
        threads = [in_thread(automations.tick, now, barrier=barrier) for _ in range(4)]
        for thread, _ in threads:
            thread.join(30)
        outcomes = [outcome for _, outcome in threads]
        self.assertTrue(all('value' in outcome for outcome in outcomes), outcomes)
        self.assertEqual(sum(outcome['value'] for outcome in outcomes), 2)
        for rule_obj in (weekly, reminder):
            self.assertEqual(AutomationRun.objects.filter(rule=rule_obj).count(), 1)
        self.assertEqual(Broadcast.objects.count(), 2)
        self.assertEqual(Delivery.objects.count(), 6)

    def test_unique_occurrence_constraint(self):
        rule_obj = rule(owner=self.admin)
        AutomationRun.objects.create(rule=rule_obj, occurrence_key='rule-1:x', scheduled_at=MONDAY_8_LAGOS)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AutomationRun.objects.create(rule=rule_obj, occurrence_key='rule-1:x', scheduled_at=MONDAY_8_LAGOS)

    def test_locked_rule_is_skipped_not_waited_for(self):
        rule_obj = rule(owner=self.admin)
        make_event(status=PUBLISHED, start=MONDAY_8_LAGOS - timedelta(days=2))
        with transaction.atomic():
            AutomationRule.objects.select_for_update().get(pk=rule_obj.pk)
            thread, outcome = in_thread(automations.tick, MONDAY_8_LAGOS)
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, {'value': 0})
        self.assertEqual(automations.tick(MONDAY_8_LAGOS), 1)

    def test_reschedule_holding_event_lock_wins_over_tick(self):
        """Lock order rule -> event: a tick racing a reschedule waits for the
        event lock, then sees the new revision and queues nothing stale."""
        rule_obj = rule(kind='event_reminder', owner=self.admin)
        start = datetime(2026, 11, 13, 12, tzinfo=UTC)
        event = make_event(status=PUBLISHED, start=start)
        due = start - timedelta(days=1)
        new_start = start + timedelta(days=3)
        with transaction.atomic():
            services.update_event(event.pk, lambda locked: EventWriteSerializer(locked, data={
                'start_time': new_start.isoformat(), 'end_time': (new_start + timedelta(hours=1)).isoformat()},
                partial=True))
            thread, outcome = in_thread(automations.tick, due)
            time.sleep(0.5)
            self.assertTrue(thread.is_alive(), 'tick should wait for the event row lock')
        thread.join(10)
        self.assertEqual(outcome, {'value': 0})
        self.assertFalse(AutomationRun.objects.filter(rule=rule_obj).exists())
        self.assertEqual(Event.objects.get(pk=event.pk).schedule_revision, 2)
