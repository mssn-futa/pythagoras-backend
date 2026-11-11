"""Outbox worker: outcomes, retries, leases, recovery, budgets and privacy (COMM-02)."""
import http.server
import io
import json
import os
import signal
import threading
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

# Import the API module first (pre-existing circular import, see test_concurrency).
from dawah import views  # noqa: F401, I001
from dawah import broadcasts, outbox
from dawah.models import Broadcast, Delivery, DeliveryAttempt, DeliveryGeneration
from pythagoras.mail import SendResult
from pythagoras.mail.resend import ResendTransport

from .helpers import DawahAPITestCase, User

SENDER = 'Pythagoras <noreply@example.test>'
MAIL_SETTINGS = override_settings(DEFAULT_FROM_EMAIL=SENDER, EMAIL_REPLY_TO='', EMAIL_IDEMPOTENCY_NAMESPACE='test',
                                  RESEND_TIMEOUT_SECONDS=30,
                                  PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
SUBJECT = 'Weekly lecture notes'
BODY = 'Private body text for the outbox tests'


class SimulatedCrash(BaseException):
    """Escapes the worker like a process kill after `sending` was persisted."""


class FakeTransport:
    """Scripted outcomes; records every handoff (no provider deduplication, so
    a double send is visible)."""

    def __init__(self, *script, on_send=None):
        self.script = list(script)
        self.calls = []
        self.on_send = on_send
        self._lock = threading.Lock()

    def send(self, message):
        with self._lock:
            self.calls.append(message)
            number = len(self.calls)
            item = self.script.pop(0) if self.script else None
        if self.on_send is not None:
            self.on_send(message)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(message)
        return item or SendResult.accepted(f'em_{number}')

    def keys(self):
        return [message.idempotency_key for message in self.calls]


class ManualClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


_seq = [0]


def make_student(department, active=True):
    _seq[0] += 1
    user = User.objects.create_user(email=f'outbox{_seq[0]}@example.test', password=None,
                                    first_name='R', last_name=str(_seq[0]), role='student', department=department)
    if not active:
        user.is_active = False
        user.save(update_fields=['is_active'])
    return user


class OutboxMixin:
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(email='outbox-admin@example.test', password=None, role='admin')

    def queue(self, recipients=1):
        """Queue a broadcast to ``recipients`` new students (own department audience)."""
        _seq[0] += 1
        department = f'Outbox department {_seq[0]}'
        students = [make_student(department) for _ in range(recipients)]
        draft = Broadcast.objects.create(created_by=self.admin, subject=SUBJECT, body=BODY,
                                         audience_type='department', audience_department=department)
        broadcast, _ = broadcasts.queue(draft.pk, draft.version, self.admin)
        return broadcast, students

    def worker(self, transport, **config):
        config.setdefault('send_timeout', 30)
        return outbox.OutboxWorker(outbox.OutboxConfig(**config), transport=transport, limiter=None,
                                   **{k: v for k, v in self.worker_kwargs.items()})

    worker_kwargs = {}

    def deliveries(self, broadcast):
        return list(Delivery.objects.filter(broadcast=broadcast).order_by('id'))

    def one(self, broadcast):
        (delivery,) = self.deliveries(broadcast)
        return delivery

    def make_due(self, broadcast=None):
        rows = Delivery.objects.all() if broadcast is None else Delivery.objects.filter(broadcast=broadcast)
        rows.filter(state='queued').update(next_attempt_at=timezone.now() - timedelta(seconds=1))

    def attempts(self, delivery):
        return list(DeliveryAttempt.objects.filter(delivery=delivery).order_by('generation', 'attempt')
                    .values_list('generation', 'attempt', 'outcome', 'error_code'))

    def state_of(self, broadcast):
        broadcast.refresh_from_db()
        return broadcast.state


@MAIL_SETTINGS
class OutcomeTests(OutboxMixin, TestCase):
    def test_happy_path_sends_one_private_message_per_recipient(self):
        broadcast, students = self.queue(recipients=3)
        transport = FakeTransport()
        summary = self.worker(transport).run_once()
        self.assertEqual(summary.outcomes, {'accepted': 3})
        self.assertEqual(summary.stop_reason, 'drained')
        rows = self.deliveries(broadcast)
        self.assertEqual([row.state for row in rows], ['sent'] * 3)
        self.assertEqual(sorted(row.provider_email_id for row in rows), ['em_1', 'em_2', 'em_3'])
        by_to = {message.to: message for message in transport.calls}
        self.assertEqual(set(by_to), {student.email for student in students})
        for row in rows:
            message = by_to[row.snapshot_email]
            self.assertEqual(message.idempotency_key, f'test/broadcast/{row.pk}/generation/0')
            self.assertEqual((message.from_email, message.subject, message.text, message.html),
                             (SENDER, broadcast.snapshot_subject, broadcast.snapshot_text, broadcast.snapshot_html))
            self.assertIsNotNone(row.sent_at)
            self.assertEqual(row.request_snapshot['to'], [row.snapshot_email])
            self.assertEqual(row.request_hash, outbox.request_hash(row.request_snapshot, row.idempotency_key))
            self.assertEqual(row.idempotency_deadline_at - row.first_dispatch_at, timedelta(hours=23))
            self.assertEqual((row.attempt_count, row.lease_token, row.nonacceptance_known), (1, None, False))
            self.assertEqual(self.attempts(row), [(0, 1, 'accepted', '')])
        self.assertEqual(self.state_of(broadcast), 'completed')
        # Nothing is due any more: a second pass sends nothing.
        self.assertEqual(self.worker(transport).run_once().claimed, 0)
        self.assertEqual(len(transport.calls), 3)

    def test_snapshot_is_used_not_current_profile_or_draft(self):
        broadcast, (student,) = self.queue()
        frozen = student.email
        User.objects.filter(pk=student.pk).update(email='changed@example.test')
        Broadcast.objects.filter(pk=broadcast.pk).update(subject='Edited outside the API')
        transport = FakeTransport()
        self.worker(transport).run_once()
        self.assertEqual((transport.calls[0].to, transport.calls[0].subject), (frozen, SUBJECT))

    def test_inactive_recipient_is_skipped_without_handoff(self):
        broadcast, (active, inactive) = self.queue(recipients=2)
        User.objects.filter(pk=inactive.pk).update(is_active=False)
        transport = FakeTransport()
        self.worker(transport).run_once()
        self.assertEqual([m.to for m in transport.calls], [active.email])
        skipped = Delivery.objects.get(broadcast=broadcast, student=inactive)
        self.assertEqual((skipped.state, skipped.error_code, skipped.idempotency_key), ('skipped', 'RECIPIENT_INACTIVE', ''))
        self.assertEqual(self.attempts(skipped), [(0, 1, 'skipped', 'RECIPIENT_INACTIVE')])
        self.assertEqual(self.state_of(broadcast), 'completed')

    def test_definitive_failure_sets_nonacceptance_and_allows_manual_retry_with_new_key(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SendResult.failed('VALIDATION_ERROR'))
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        self.assertEqual((delivery.state, delivery.error_code, delivery.nonacceptance_known),
                         ('failed', 'VALIDATION_ERROR', True))
        self.assertTrue(delivery.is_retry_eligible)
        self.assertEqual(self.state_of(broadcast), 'completed_with_errors')
        broadcasts.retry(broadcast.pk, [delivery.pk], self.admin)
        self.worker(transport).run_once()
        delivery.refresh_from_db()
        self.assertEqual(transport.keys(), [f'test/broadcast/{delivery.pk}/generation/0',
                                            f'test/broadcast/{delivery.pk}/generation/1'])
        self.assertEqual((delivery.state, delivery.retry_generation, delivery.provider_email_id), ('sent', 1, 'em_2'))
        self.assertEqual(DeliveryGeneration.objects.get(delivery=delivery).idempotency_key, transport.keys()[0])
        self.assertEqual(self.attempts(delivery), [(0, 1, 'failed', 'VALIDATION_ERROR'), (1, 1, 'accepted', '')])
        self.assertEqual(self.state_of(broadcast), 'completed')

    def test_failure_before_handoff_is_known_nonacceptance(self):
        broadcast, _ = self.queue()
        transport = FakeTransport()
        with override_settings(DEFAULT_FROM_EMAIL=''):
            self.worker(transport).run_once()
        delivery = self.one(broadcast)
        self.assertEqual(transport.calls, [])
        self.assertEqual((delivery.state, delivery.error_code, delivery.nonacceptance_known, delivery.request_snapshot),
                         ('failed', 'INVALID_MESSAGE', True, None))

    def test_payload_conflict_is_hard_failure_without_nonacceptance(self):
        for code in ('INVALID_IDEMPOTENT_REQUEST', 'IDEMPOTENCY_PAYLOAD_MISMATCH'):
            broadcast, _ = self.queue()
            self.worker(FakeTransport(SendResult.failed(code))).run_once()
            delivery = self.one(broadcast)
            self.assertEqual((delivery.state, delivery.error_code, delivery.nonacceptance_known),
                             ('failed', 'IDEMPOTENCY_PAYLOAD_CONFLICT', False))
            self.assertFalse(delivery.is_retry_eligible)
            with self.assertRaises(Exception):
                broadcasts.retry(broadcast.pk, [delivery.pk], self.admin)

    def test_timeout_then_same_key_replay_is_accepted(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SendResult.uncertain('TIMEOUT'))
        before = timezone.now()
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        self.assertEqual((delivery.state, delivery.error_code, delivery.nonacceptance_known), ('queued', 'TIMEOUT', False))
        self.assertAlmostEqual((delivery.next_attempt_at - before).total_seconds(), 60, delta=5)
        self.assertEqual(self.state_of(broadcast), 'processing')
        snapshot, key, first = delivery.request_snapshot, delivery.idempotency_key, delivery.first_dispatch_at
        self.make_due()
        self.worker(transport).run_once()
        delivery.refresh_from_db()
        self.assertEqual(transport.calls[0], transport.calls[1])  # identical frozen payload and key
        self.assertEqual((delivery.request_snapshot, delivery.idempotency_key, delivery.first_dispatch_at),
                         (snapshot, key, first))
        self.assertEqual((delivery.state, delivery.provider_email_id, delivery.attempt_count), ('sent', 'em_2', 2))
        self.assertEqual(self.attempts(delivery), [(0, 1, 'uncertain', 'TIMEOUT'), (0, 2, 'accepted', '')])

    def test_uncertain_budget_exhaustion_is_uncertain_with_60_then_300_delays(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(*[SendResult.uncertain('HTTP_500')] * 3)
        delays = []
        for _ in range(3):
            before = timezone.now()
            self.worker(transport).run_once()
            delivery = self.one(broadcast)
            if delivery.state == 'queued':
                delays.append(round((delivery.next_attempt_at - before).total_seconds() / 10) * 10)
                self.make_due()
        self.assertEqual(delays, [60, 300])
        self.assertEqual(len(set(transport.keys())), 1)
        self.assertEqual((delivery.state, delivery.error_code, delivery.nonacceptance_known, delivery.attempt_count),
                         ('uncertain', 'HTTP_500', False, 3))
        self.assertFalse(delivery.is_retry_eligible)
        self.assertEqual(self.state_of(broadcast), 'uncertain')

    def test_rate_limit_uses_retry_after_and_exhausts_to_known_failure(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SendResult.retryable('RATE_LIMIT_EXCEEDED', retry_after=120),
                                  SendResult.retryable('RATE_LIMIT_EXCEEDED'),
                                  SendResult.retryable('RATE_LIMIT_EXCEEDED', retry_after=5))
        before = timezone.now()
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        self.assertEqual(delivery.state, 'queued')
        self.assertAlmostEqual((delivery.next_attempt_at - before).total_seconds(), 120, delta=5)
        self.make_due()
        before = timezone.now()
        self.worker(transport).run_once()
        delivery.refresh_from_db()
        self.assertAlmostEqual((delivery.next_attempt_at - before).total_seconds(), 300, delta=5)
        self.make_due()
        self.worker(transport).run_once()
        delivery.refresh_from_db()
        # Every attempt was a definite non-acceptance: known failure, retry-eligible.
        self.assertEqual((delivery.state, delivery.error_code, delivery.nonacceptance_known),
                         ('failed', 'RATE_LIMIT_EXCEEDED', True))
        self.assertEqual(len(set(transport.keys())), 1)

    def test_concurrent_idempotent_request_retries_same_key(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SendResult.uncertain('TIMEOUT'),
                                  SendResult.retryable('CONCURRENT_IDEMPOTENT_REQUESTS', retry_after=1))
        self.worker(transport).run_once()
        self.make_due()
        self.worker(transport).run_once()
        self.make_due()
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        self.assertEqual(len(set(transport.keys())), 1)
        self.assertEqual((delivery.state, delivery.attempt_count), ('sent', 3))

    def test_retryable_exhaustion_after_ambiguous_attempt_is_uncertain(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SendResult.uncertain('TIMEOUT'), SendResult.retryable('RATE_LIMIT_EXCEEDED'),
                                  SendResult.retryable('RATE_LIMIT_EXCEEDED'))
        for _ in range(3):
            self.worker(transport).run_once()
            self.make_due()
        delivery = self.one(broadcast)
        self.assertEqual((delivery.state, delivery.nonacceptance_known), ('uncertain', False))

    def test_definitive_failure_after_ambiguous_attempt_is_uncertain(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SendResult.uncertain('TIMEOUT'), SendResult.failed('UNAUTHORIZED'))
        self.worker(transport).run_once()
        self.make_due()
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        self.assertEqual((delivery.state, delivery.error_code, delivery.nonacceptance_known),
                         ('uncertain', 'UNAUTHORIZED', False))

    def test_success_without_id_and_transport_exception_are_unresolved(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SendResult.accepted(''), RuntimeError('boom'))
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        self.assertEqual((delivery.state, delivery.error_code, delivery.provider_email_id),
                         ('queued', 'MALFORMED_SUCCESS', ''))
        self.make_due()
        self.worker(transport).run_once()
        delivery.refresh_from_db()
        self.assertEqual((delivery.state, delivery.error_code), ('queued', 'TRANSPORT_EXCEPTION'))
        self.assertEqual(len(set(transport.keys())), 1)

    def test_deadline_expiry_after_ambiguity_is_uncertain_without_send(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SendResult.uncertain('TIMEOUT'))
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        Delivery.objects.filter(pk=delivery.pk).update(
            next_attempt_at=timezone.now(), idempotency_deadline_at=timezone.now() + timedelta(seconds=10))
        self.worker(transport).run_once()  # 10 s left cannot cover a 30 s bounded send
        delivery.refresh_from_db()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual((delivery.state, delivery.error_code, delivery.nonacceptance_known),
                         ('uncertain', 'IDEMPOTENCY_WINDOW_EXPIRED', False))
        self.assertEqual(self.state_of(broadcast), 'uncertain')

    def test_deadline_expiry_after_only_rate_limits_is_known_failure(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SendResult.retryable('RATE_LIMIT_EXCEEDED'))
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        Delivery.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now(),
                                                       idempotency_deadline_at=timezone.now())
        self.worker(transport).run_once()
        delivery.refresh_from_db()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual((delivery.state, delivery.error_code, delivery.nonacceptance_known),
                         ('failed', 'IDEMPOTENCY_WINDOW_EXPIRED', True))

    def test_retry_after_beyond_window_does_not_schedule_replay(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SendResult.retryable('RATE_LIMIT_EXCEEDED', retry_after=24 * 3600))
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        self.assertEqual((delivery.state, delivery.nonacceptance_known), ('failed', True))

    def test_tampered_request_snapshot_is_never_sent(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SendResult.uncertain('TIMEOUT'))
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        snapshot = dict(delivery.request_snapshot, subject='Changed')
        Delivery.objects.filter(pk=delivery.pk).update(request_snapshot=snapshot, next_attempt_at=timezone.now())
        self.worker(transport).run_once()
        delivery.refresh_from_db()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual((delivery.state, delivery.error_code), ('uncertain', 'REQUEST_SNAPSHOT_MISMATCH'))

    def test_inactive_after_ambiguous_attempt_stays_uncertain(self):
        broadcast, (student,) = self.queue()
        transport = FakeTransport(SendResult.uncertain('TIMEOUT'))
        self.worker(transport).run_once()
        User.objects.filter(pk=student.pk).update(is_active=False)
        self.make_due()
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual((delivery.state, delivery.error_code), ('uncertain', 'RECIPIENT_INACTIVE'))

    def test_aggregate_state_precedence(self):
        broadcast, _ = self.queue(recipients=4)
        transport = FakeTransport(SendResult.accepted('em_a'), SendResult.failed('VALIDATION_ERROR'),
                                  SendResult.uncertain('TIMEOUT'))
        self.worker(transport, max_attempts=1).run_once(max_sends=2)
        self.assertEqual(self.state_of(broadcast), 'processing')  # outstanding work beats errors
        self.worker(transport, max_attempts=1).run_once(max_sends=1)
        self.assertEqual(self.state_of(broadcast), 'uncertain')  # uncertain beats outstanding work
        self.worker(transport, max_attempts=1).run_once()
        states = sorted(row.state for row in self.deliveries(broadcast))
        self.assertEqual(states, ['failed', 'sent', 'sent', 'uncertain'])
        self.assertEqual(self.state_of(broadcast), 'uncertain')
        counts = broadcasts.counts_of(broadcast)
        self.assertEqual((counts['sent'], counts['failed'], counts['uncertain'], counts['queued']), (2, 1, 1, 0))

    def test_sent_and_uncertain_are_never_reclaimed(self):
        broadcast, _ = self.queue(recipients=2)
        transport = FakeTransport(SendResult.accepted('em_a'), SendResult.uncertain('TIMEOUT'))
        self.worker(transport, max_attempts=1).run_once()
        Delivery.objects.update(next_attempt_at=timezone.now() - timedelta(days=1))
        self.assertEqual(self.worker(transport).claim_batch(), [])


@MAIL_SETTINGS
class LeaseAndRecoveryTests(OutboxMixin, TestCase):
    def test_crash_after_sending_recovers_with_same_key(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SimulatedCrash())
        with self.assertRaises(SimulatedCrash):
            self.worker(transport).run_once()
        delivery = self.one(broadcast)
        self.assertEqual(delivery.state, 'sending')
        key, digest = delivery.idempotency_key, delivery.request_hash
        # Lease still live: a restarted worker must not touch it.
        self.assertEqual(self.worker(transport).run_once().recovered, 0)
        self.assertEqual(len(transport.calls), 1)
        Delivery.objects.filter(pk=delivery.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        summary = self.worker(transport).run_once()
        delivery.refresh_from_db()
        self.assertEqual(summary.recovered, 1)
        self.assertEqual(transport.calls[0], transport.calls[1])
        self.assertEqual((delivery.idempotency_key, delivery.request_hash), (key, digest))
        self.assertEqual((delivery.state, delivery.attempt_count), ('sent', 2))
        self.assertEqual(self.attempts(delivery), [(0, 1, 'uncertain', 'INTERRUPTED'), (0, 2, 'accepted', '')])

    def test_interrupted_send_on_last_attempt_becomes_uncertain(self):
        broadcast, _ = self.queue()
        transport = FakeTransport(SimulatedCrash())
        with self.assertRaises(SimulatedCrash):
            self.worker(transport, max_attempts=1).run_once()
        Delivery.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.worker(transport, max_attempts=1).run_once()
        delivery = self.one(broadcast)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual((delivery.state, delivery.error_code, delivery.nonacceptance_known),
                         ('uncertain', 'INTERRUPTED', False))
        self.assertEqual(self.state_of(broadcast), 'uncertain')

    def test_interrupted_send_outside_window_becomes_uncertain(self):
        broadcast, _ = self.queue()
        with self.assertRaises(SimulatedCrash):
            self.worker(FakeTransport(SimulatedCrash())).run_once()
        Delivery.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1),
                                idempotency_deadline_at=timezone.now())
        transport = FakeTransport()
        self.worker(transport).run_once()
        delivery = self.one(broadcast)
        self.assertEqual(transport.calls, [])
        self.assertEqual((delivery.state, delivery.error_code), ('uncertain', 'IDEMPOTENCY_WINDOW_EXPIRED'))

    def test_expired_claim_is_requeued_not_sent(self):
        broadcast, _ = self.queue()
        transport = FakeTransport()
        worker = self.worker(transport)
        (claim,) = worker.claim_batch()
        Delivery.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(worker.process(claim), 'expired')
        delivery = self.one(broadcast)
        self.assertEqual((delivery.state, delivery.lease_token, delivery.attempt_count), ('queued', None, 0))
        self.assertEqual(transport.calls, [])

    def test_recovery_requeues_expired_claims(self):
        self.queue(recipients=2)
        worker = self.worker(FakeTransport())
        claims = worker.claim_batch()
        Delivery.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(worker.recover_expired(), 2)
        self.assertEqual(set(Delivery.objects.values_list('state', 'lease_token')), {('queued', None)})
        self.assertEqual([worker.process(claim) for claim in claims], ['lost', 'lost'])

    def test_stale_lease_completion_cannot_overwrite_newer_outcome(self):
        broadcast, _ = self.queue()
        transport = FakeTransport()
        stale = self.worker(transport)
        (claim,) = stale.claim_batch()
        prepared = stale.prepare(claim)
        self.assertEqual(self.one(broadcast).state, 'sending')
        # The stale worker stalls past its lease; a new worker recovers and sends.
        Delivery.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.worker(transport).run_once()
        self.assertEqual(self.one(broadcast).state, 'sent')
        with self.assertLogs('dawah.outbox', 'WARNING') as logs:
            self.assertEqual(stale.complete(prepared, SendResult.failed('VALIDATION_ERROR')), 'stale')
        self.assertIn('outbox_stale_completion', logs.output[0])
        delivery = self.one(broadcast)
        self.assertEqual((delivery.state, delivery.provider_email_id, delivery.error_code), ('sent', 'em_1', ''))
        self.assertEqual(self.attempts(delivery), [(0, 1, 'uncertain', 'INTERRUPTED'), (0, 2, 'accepted', '')])

    def test_stop_request_finishes_current_send_and_releases_claims(self):
        broadcast, _ = self.queue(recipients=3)
        stop = threading.Event()
        transport = FakeTransport(on_send=lambda message: stop.set())
        self.worker_kwargs = {'stop_event': stop}
        summary = self.worker(transport).run_once()
        self.assertEqual((summary.stop_reason, summary.claimed, summary.released), ('signal', 3, 2))
        rows = self.deliveries(broadcast)
        self.assertEqual(sorted(row.state for row in rows), ['queued', 'queued', 'sent'])
        self.assertTrue(all(row.lease_token is None for row in rows))
        self.assertEqual(len(transport.calls), 1)

    def test_wall_budget_stops_dispatch_and_releases_claims(self):
        broadcast, _ = self.queue(recipients=4)
        clock = ManualClock()

        def advance(message):
            clock.now += 35

        self.worker_kwargs = {'clock': clock}
        transport = FakeTransport(on_send=advance)
        # 100 s budget - (30 s send + 10 s margin) = dispatch until t+60: sends at t+0 and t+35.
        summary = self.worker(transport).run_once(budget_seconds=100)
        self.assertEqual((summary.stop_reason, summary.outcomes, summary.released), ('budget', {'accepted': 2}, 2))
        self.assertEqual(sorted(row.state for row in self.deliveries(broadcast)), ['queued', 'queued', 'sent', 'sent'])
        self.assertFalse(Delivery.objects.filter(lease_token__isnull=False).exists())

    def test_max_sends_caps_handoffs(self):
        self.queue(recipients=3)
        transport = FakeTransport()
        summary = self.worker(transport).run_once(max_sends=2)
        self.assertEqual((summary.stop_reason, len(transport.calls)), ('max_sends', 2))
        self.assertEqual(Delivery.objects.filter(state='queued', lease_token__isnull=True).count(), 1)

    def test_rate_limiter_paces_and_respects_budget(self):
        clock = ManualClock()
        limiter = outbox.RateLimiter(2, clock=clock)
        self.assertEqual([limiter.reserve(), limiter.reserve(), limiter.reserve()], [0.0, 0.5, 1.0])
        clock.now += 10
        self.assertEqual(limiter.reserve(), 0.0)
        self.queue(recipients=2)
        slow = outbox.OutboxWorker(outbox.OutboxConfig(send_timeout=30), transport=FakeTransport(),
                                   limiter=outbox.RateLimiter(0.001), clock=ManualClock())
        summary = slow.run_once(budget_seconds=100)
        # First slot is immediate; the next is 1000 s away, beyond the budget.
        self.assertEqual((summary.outcomes, summary.stop_reason, summary.released), ({'accepted': 1}, 'budget', 1))

    def test_continuous_mode_runs_until_stopped(self):
        self.queue(recipients=2)
        stop = threading.Event()
        calls = []

        def on_send(message):
            calls.append(message)
            if len(calls) == 2:
                stop.set()

        worker = outbox.OutboxWorker(outbox.OutboxConfig(send_timeout=30, poll_seconds=0.01),
                                     transport=FakeTransport(on_send=on_send), limiter=None, stop_event=stop)
        summary = worker.run_forever()
        self.assertEqual((summary.mode, summary.stop_reason, summary.outcomes), ('continuous', 'signal', {'accepted': 2}))

    def test_scheduler_tick_hook_runs_first_in_once_mode(self):
        order = []
        with mock.patch.object(outbox, 'scheduler_tick', side_effect=lambda now: order.append('tick') or 0), \
                mock.patch.object(outbox.OutboxWorker, 'recover_expired', side_effect=lambda: order.append('recover') or 0):
            self.worker(FakeTransport()).run_once()
        self.assertEqual(order, ['tick', 'recover'])
        self.assertEqual(outbox.scheduler_tick(timezone.now()), 0)


@MAIL_SETTINGS
class PrivacyAndCommandTests(OutboxMixin, TestCase):
    def test_logs_contain_no_addresses_subjects_or_bodies(self):
        broadcast, students = self.queue(recipients=3)
        User.objects.filter(pk=students[2].pk).update(is_active=False)
        transport = FakeTransport(SendResult.uncertain('TIMEOUT'), SendResult.failed('VALIDATION_ERROR'))
        with self.assertLogs('dawah.outbox', 'INFO') as logs:
            self.worker(transport).run_once()
            self.make_due()
            self.worker(transport).run_once()
        text = '\n'.join(logs.output)
        self.assertIn('outbox_result', text)
        self.assertIn('outbox_run', text)
        for secret in [s.email for s in students] + [SUBJECT, BODY, SENDER, 'example.test']:
            self.assertNotIn(secret, text)

    def run_command(self, *args):
        out = io.StringIO()
        call_command('dawah_outbox', *args, stdout=out, verbosity=0)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def test_status_reports_backlog_without_sending(self):
        self.queue(recipients=2)
        Delivery.objects.update(created_at=timezone.now() - timedelta(minutes=5),
                                next_attempt_at=timezone.now() - timedelta(minutes=2))
        with mock.patch.object(outbox, 'get_transport') as get_transport:
            status = self.run_command('--status')
        get_transport.assert_not_called()
        self.assertEqual((status['counts']['queued'], status['due'], status['expired_leases']), (2, 2, 0))
        self.assertGreaterEqual(status['oldest_outstanding_age_seconds'], 299)
        self.assertGreaterEqual(status['oldest_due_age_seconds'], 119)
        self.assertNotIn('example.test', json.dumps(status))

    def test_once_command_uses_shared_transport_and_prints_summary(self):
        broadcast, _ = self.queue(recipients=2)
        transport = FakeTransport()
        with mock.patch.object(outbox, 'get_transport', return_value=transport) as get_transport, \
                mock.patch.object(outbox, 'account_limiter', return_value=outbox.RateLimiter(1000)):
            summary = self.run_command('--once', '--budget-seconds', '120')
        get_transport.assert_called_with(timeout=30)
        self.assertEqual((summary['mode'], summary['outcomes'], summary['stop_reason']),
                         ('once', {'accepted': 2}, 'drained'))
        self.assertEqual(self.state_of(broadcast), 'completed')

    def test_sigterm_during_once_finishes_current_and_releases(self):
        self.queue(recipients=3)
        transport = FakeTransport(on_send=lambda message: os.kill(os.getpid(), signal.SIGTERM))
        previous = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(outbox, 'get_transport', return_value=transport), \
                mock.patch.object(outbox, 'account_limiter', return_value=outbox.RateLimiter(1000)):
            summary = self.run_command('--once')
        self.assertEqual((summary['stop_reason'], summary['outcomes'], summary['released']),
                         ('signal', {'accepted': 1}, 2))
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        self.assertEqual(Delivery.objects.filter(state='queued', lease_token__isnull=True).count(), 2)

    def test_command_rejects_invalid_options(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('dawah_outbox', '--once', '--budget-seconds', '0')
        with self.assertRaises(CommandError):
            call_command('dawah_outbox', '--once', '--status')


@MAIL_SETTINGS
class DeliveryHistoryAPITests(OutboxMixin, DawahAPITestCase, TestCase):
    def test_history_shows_worker_outcomes_to_admins_only(self):
        broadcast, (student, _) = self.queue(recipients=2)
        self.worker(FakeTransport(SendResult.accepted('re_abc'), SendResult.failed('VALIDATION_ERROR'))).run_once()
        self.client.force_authenticate(self.admin)
        response = self.client.get(f'/dawah/broadcasts/{broadcast.pk}/deliveries/')
        self.assertEqual(response.status_code, 200)
        results = response.json()['data']['results']
        self.assertEqual(sorted((r['state'], r['provider_email_id'], r['error_code'], r['eligible_actions'])
                                for r in results),
                         [('failed', None, 'VALIDATION_ERROR', ['retry']), ('sent', 're_abc', None, [])])
        detail = self.client.get(f'/dawah/broadcasts/{broadcast.pk}/').json()['data']
        self.assertEqual((detail['state'], detail['eligible_actions']), ('completed_with_errors', ['retry']))
        self.client.force_authenticate(student)
        self.assertEqual(self.client.get(f'/dawah/broadcasts/{broadcast.pk}/deliveries/').status_code, 403)


@MAIL_SETTINGS
class ConcurrentWorkerTests(OutboxMixin, TransactionTestCase):
    def run_threads(self, count, target):
        barrier = threading.Barrier(count)
        errors, results = [], [None] * count

        def wrapped(index):
            try:
                barrier.wait(10)
                results[index] = target(index)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=wrapped, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        return results

    def test_concurrent_claims_are_disjoint(self):
        self.queue(recipients=10)
        results = self.run_threads(4, lambda index: self.worker(FakeTransport()).claim_batch(limit=4))
        ids = [claim.delivery_id for claims in results for claim in claims]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 10)

    def test_concurrent_workers_never_double_send(self):
        broadcast, _ = self.queue(recipients=25)
        transport = FakeTransport()
        self.run_threads(4, lambda index: self.worker(transport, batch_size=3).run_once())
        keys = transport.keys()
        self.assertEqual(len(keys), 25)
        self.assertEqual(len(set(keys)), 25)
        self.assertEqual(set(Delivery.objects.values_list('state', flat=True)), {'sent'})
        self.assertEqual(DeliveryAttempt.objects.count(), 25)
        self.assertEqual(Broadcast.objects.get(pk=broadcast.pk).state, 'completed')

    def test_concurrent_recovery_replays_once(self):
        self.queue(recipients=5)
        with self.assertRaises(SimulatedCrash):
            self.worker(FakeTransport(SimulatedCrash()), batch_size=5).run_once()
        # The crash escaped after one handoff; the batch's unused claims were released.
        self.assertEqual(sorted(Delivery.objects.values_list('state', flat=True)), ['queued'] * 4 + ['sending'])
        Delivery.objects.filter(state='sending').update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        transport = FakeTransport()
        self.run_threads(3, lambda index: self.worker(transport).run_once())
        self.assertEqual(len(transport.keys()), len(set(transport.keys())))
        self.assertEqual(set(Delivery.objects.values_list('state', flat=True)), {'sent'})


class _ScriptedResend(http.server.BaseHTTPRequestHandler):
    """Local stand-in for POST /emails: replies from ``server.script`` and records requests."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.requests.append((dict(self.headers), body))
        status, headers, payload = self.server.script.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@MAIL_SETTINGS
class ResendAdapterIntegrationTests(OutboxMixin, TestCase):
    """The worker through the real shared Resend adapter, against a local server."""

    def serve(self, *script):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _ScriptedResend)
        server.script, server.requests = list(script), []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        patcher = mock.patch.dict(os.environ, {'NO_PROXY': '*', 'no_proxy': '*'})
        patcher.start()
        self.addCleanup(patcher.stop)
        transport = ResendTransport(api_key='re_test_local_only', timeout=5,
                                    url=f'http://127.0.0.1:{server.server_address[1]}/emails')
        return server, transport

    def test_rate_limit_then_acceptance_reuses_exact_request(self):
        broadcast, (student,) = self.queue()
        server, transport = self.serve((429, {'Retry-After': '90'}, {'name': 'rate_limit_exceeded'}),
                                       (200, {}, {'id': 're_local_1'}))
        before = timezone.now()
        self.worker(transport, send_timeout=5).run_once()
        delivery = self.one(broadcast)
        self.assertEqual((delivery.state, delivery.error_code), ('queued', 'RATE_LIMIT_EXCEEDED'))
        self.assertAlmostEqual((delivery.next_attempt_at - before).total_seconds(), 90, delta=5)
        self.make_due()
        self.worker(transport, send_timeout=5).run_once()
        delivery.refresh_from_db()
        self.assertEqual((delivery.state, delivery.provider_email_id), ('sent', 're_local_1'))
        (h1, b1), (h2, b2) = server.requests
        self.assertEqual(b1, b2)
        self.assertEqual(h1['Idempotency-Key'], h2['Idempotency-Key'])
        self.assertEqual(h1['Idempotency-Key'], delivery.idempotency_key)
        sent = json.loads(b1)
        self.assertEqual((sent['to'], sent['from'], sent['subject']), ([student.email], SENDER, SUBJECT))
        self.assertNotIn('cc', sent)
        self.assertNotIn('bcc', sent)

    def test_provider_payload_conflict_and_validation_error(self):
        broadcast, _ = self.queue(recipients=2)
        _, transport = self.serve((409, {}, {'name': 'invalid_idempotent_request'}),
                                  (422, {}, {'name': 'validation_error'}))
        self.worker(transport, send_timeout=5).run_once()
        rows = self.deliveries(broadcast)
        self.assertEqual([(r.state, r.error_code, r.nonacceptance_known) for r in rows],
                         [('failed', 'IDEMPOTENCY_PAYLOAD_CONFLICT', False), ('failed', 'VALIDATION_ERROR', True)])
