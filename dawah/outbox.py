"""Broadcast outbox worker (D4 "State / Lifecycle", D5 "Bounded scheduled processing").

One logical recipient per ``Delivery``. The worker

1. claims a bounded batch of due ``queued`` rows with ``SELECT ... FOR UPDATE
   SKIP LOCKED``, gives each a fresh lease token/expiry, marks it ``claimed``
   and commits (no lock is held afterwards);
2. per row, under the broadcast -> delivery lock order: checks the lease token
   and expiry, rechecks the recipient is active, freezes the provider request
   (snapshot, hash, idempotency key, first dispatch and 23 h safety deadline)
   on the generation's first dispatch, renews the lease, persists ``sending``
   and an open ``DeliveryAttempt`` row, and commits;
3. calls the shared transport with **no database lock held**;
4. completes under the same lock order only if the lease token still matches,
   maps the outcome, closes the attempt row and refreshes the broadcast state.

Recovery: an expired ``claimed`` lease is requeued (nothing was handed off);
an expired ``sending`` lease is an interrupted handoff and is replayed only
with the same request/key inside the attempt budget and safety window,
otherwise it becomes ``uncertain``. Nothing here mints a new key: a new key
only comes from a manual retry generation (``broadcasts.retry``), which needs
positive evidence of nonacceptance (docs/communication-outbox.md).

Logs carry delivery/broadcast ids, generation, attempt, outcome, safe codes and
provider email IDs only - never addresses, subjects, bodies or credentials.
"""
import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Min, Q
from django.utils import timezone

from pythagoras.mail import InvalidMessage, OutboundEmail, SendResult, SendStatus, get_transport, idempotency_key

from . import broadcasts
from .models import OUTSTANDING_DELIVERY_STATES, Broadcast, Delivery, DeliveryAttempt, DeliveryState, Event

logger = logging.getLogger('dawah.outbox')
# The command attaches a stderr handler; library use relies on the project's logging config.
logger.addHandler(logging.NullHandler())

# Provider codes meaning "a different payload was already sent under this key".
PAYLOAD_CONFLICT_CODES = frozenset({'INVALID_IDEMPOTENT_REQUEST', 'IDEMPOTENCY_PAYLOAD_MISMATCH'})
PAYLOAD_CONFLICT = 'IDEMPOTENCY_PAYLOAD_CONFLICT'
WINDOW_EXPIRED = 'IDEMPOTENCY_WINDOW_EXPIRED'
INTERRUPTED = 'INTERRUPTED'
RECIPIENT_INACTIVE = 'RECIPIENT_INACTIVE'
INVALID_MESSAGE = 'INVALID_MESSAGE'
SNAPSHOT_MISMATCH = 'REQUEST_SNAPSHOT_MISMATCH'
TRANSPORT_EXCEPTION = 'TRANSPORT_EXCEPTION'
# 409: a request with this key is still in flight; it may yet be accepted.
CONCURRENT_REQUEST = 'CONCURRENT_IDEMPOTENT_REQUESTS'
# Pause after a 429/quota response without Retry-After.
RATE_LIMIT_DEFAULT_SECONDS = 60

# Attempt outcomes that prove the provider did not accept that attempt:
# ``retryable`` is recorded only for explicit 429/quota rejections (a
# concurrent-key 409 is recorded ``uncertain``); ``skipped`` never handed off.
_NONACCEPTED_OUTCOMES = (DeliveryAttempt.Outcome.RETRYABLE, DeliveryAttempt.Outcome.SKIPPED)


def is_rate_limit(result):
    """429 rate limit or quota: an explicit provider non-acceptance."""
    return result.status is SendStatus.RETRYABLE and (result.code or '') != CONCURRENT_REQUEST


@dataclass(frozen=True)
class OutboxConfig:
    """D4 engineering defaults."""
    batch_size: int = 20
    poll_seconds: float = 5.0
    lease_seconds: int = 120
    max_attempts: int = 3
    retry_delays: tuple = (60, 300)
    safety_window: timedelta = timedelta(hours=23)
    send_timeout: float | None = None  # defaults to settings.RESEND_TIMEOUT_SECONDS
    shutdown_margin: float = 10.0

    @property
    def timeout(self):
        return self.send_timeout if self.send_timeout is not None else settings.RESEND_TIMEOUT_SECONDS

    def validate(self):
        """A bounded send plus margin must fit in the lease, or a live worker's
        claim could expire mid-send and be replayed concurrently."""
        if self.timeout + self.shutdown_margin >= self.lease_seconds:
            raise ValueError('send timeout plus shutdown margin must be shorter than the lease.')
        if self.max_attempts < 1 or self.batch_size < 1:
            raise ValueError('max_attempts and batch_size must be positive.')


@dataclass(frozen=True)
class Claim:
    delivery_id: int
    token: uuid.UUID


@dataclass
class RunSummary:
    mode: str
    claimed: int = 0
    released: int = 0
    recovered: int = 0
    materialized: int = 0
    handoffs: int = 0
    outcomes: dict = field(default_factory=dict)
    stop_reason: str = ''
    elapsed_seconds: float = 0.0

    def count(self, outcome):
        outcome = str(outcome)
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    def as_dict(self):
        return {'mode': self.mode, 'claimed': self.claimed, 'released': self.released,
                'recovered': self.recovered, 'materialized': self.materialized, 'handoffs': self.handoffs,
                'outcomes': dict(sorted(self.outcomes.items())), 'stop_reason': self.stop_reason,
                'elapsed_seconds': round(self.elapsed_seconds, 3)}


# ---------------------------------------------------------------- rate budget

class RateLimiter:
    """Process-wide pacing: at most ``rate`` handoffs per second, no bursts.

    ``reserve()`` books the next slot and returns the seconds to wait for it.
    The deployment runs one outbox process (Cloudflare singleton job), so this
    is the outbox's share of the Resend account rate; welcome mail uses the
    remaining headroom and 429 responses back off with Retry-After."""

    def __init__(self, rate, clock=time.monotonic):
        self.interval = 1.0 / rate
        self._clock = clock
        self._lock = threading.Lock()
        self._next = 0.0

    def reserve(self):
        with self._lock:
            now = self._clock()
            slot = max(now, self._next)
            self._next = slot + self.interval
            return slot - now

    def pending(self):
        """Seconds until the next slot, without booking it."""
        with self._lock:
            return max(0.0, self._next - self._clock())

    def defer(self, seconds):
        """Pause every handoff in this process (429/quota with Retry-After)."""
        with self._lock:
            self._next = max(self._next, self._clock() + max(0.0, seconds))


_account_limiter = None
_account_limiter_lock = threading.Lock()


def account_limiter():
    global _account_limiter
    with _account_limiter_lock:
        if _account_limiter is None:
            _account_limiter = RateLimiter(settings.OUTBOX_REQUESTS_PER_SECOND)
        return _account_limiter


# ---------------------------------------------------------------- scheduler hook

def scheduler_tick(now):
    """Materialize due automation occurrences into queued broadcasts (COMM-06,
    ``dawah.automations.tick``): unique rule/occurrence runs under row locks,
    so repeated or concurrent passes create nothing twice. Returns the number
    of broadcasts created."""
    from . import automations  # automations imports this module lazily as well

    return automations.tick(now)


# ---------------------------------------------------------------- request identity

def delivery_idempotency_key(delivery):
    return idempotency_key('broadcast', delivery.pk, 'generation', delivery.retry_generation)


def build_request_snapshot(broadcast, delivery):
    """The exact provider request for this recipient, frozen on first dispatch."""
    return {
        'from': settings.DEFAULT_FROM_EMAIL,
        'to': [delivery.snapshot_email],
        'subject': broadcast.snapshot_subject,
        'html': broadcast.snapshot_html,
        'text': broadcast.snapshot_text,
        'reply_to': settings.EMAIL_REPLY_TO or None,
    }


def request_hash(snapshot, key):
    canonical = json.dumps({'idempotency_key': key, 'request': snapshot}, sort_keys=True,
                           separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def message_from_snapshot(snapshot, key):
    """Raises InvalidMessage if the snapshot violates the single-recipient contract."""
    to = snapshot.get('to') or []
    if len(to) != 1:
        raise InvalidMessage('to must contain exactly one recipient.')
    return OutboundEmail(from_email=snapshot['from'], to=to[0], subject=snapshot['subject'],
                         html=snapshot['html'], text=snapshot['text'], idempotency_key=key,
                         reply_to=snapshot.get('reply_to') or None)


def _safe_code(code, default):
    code = (code or default).strip() or default
    return code[:64]


# ---------------------------------------------------------------- observability

def backlog(now=None):
    """Counts and ages for operators. Ids/counts/seconds only, no recipient data."""
    now = now or timezone.now()
    counts = {state: 0 for state in DeliveryState.values}
    for row in Delivery.objects.order_by().values('state').annotate(total=Count('id')):
        counts[row['state']] = row['total']
    outstanding = Delivery.objects.filter(state__in=OUTSTANDING_DELIVERY_STATES)
    due = Delivery.objects.filter(state=DeliveryState.QUEUED).filter(
        Q(next_attempt_at__lte=now) | Q(next_attempt_at__isnull=True))
    oldest_outstanding = outstanding.aggregate(value=Min('created_at'))['value']
    oldest_due = due.aggregate(value=Min('next_attempt_at'))['value']

    def age(moment):
        return None if moment is None else max(0, int((now - moment).total_seconds()))

    return {
        'counts': counts,
        'due': due.count(),
        'expired_leases': Delivery.objects.filter(state__in=[DeliveryState.CLAIMED, DeliveryState.SENDING],
                                                  lease_expires_at__lt=now).count(),
        'oldest_outstanding_age_seconds': age(oldest_outstanding),
        'oldest_due_age_seconds': age(oldest_due),
        'broadcasts_processing': Broadcast.objects.filter(state__in=['queued', 'processing']).count(),
        'broadcasts_uncertain': Broadcast.objects.filter(state='uncertain').count(),
    }


# ---------------------------------------------------------------- worker

@dataclass(frozen=True)
class _Prepared:
    claim: Claim
    broadcast_id: int
    generation: int
    attempt: int
    message: OutboundEmail


def _lock_pair(broadcast_id, delivery_id):
    """Lock order: broadcast row, then the delivery row."""
    broadcast = Broadcast.objects.select_for_update().get(pk=broadcast_id)
    delivery = Delivery.objects.select_for_update().get(pk=delivery_id)
    return broadcast, delivery


def _ambiguous_history(delivery, exclude_attempt=None):
    """True if any attempt of the current generation may have been accepted
    (uncertain, interrupted/open, lease lost, conflicting)."""
    attempts = DeliveryAttempt.objects.filter(delivery=delivery, generation=delivery.retry_generation)
    if exclude_attempt is not None:
        attempts = attempts.exclude(attempt=exclude_attempt)
    return attempts.exclude(outcome__in=_NONACCEPTED_OUTCOMES).exists()


def _budget_used(delivery):
    """Handoffs of the current generation that count against the attempt
    budget: every attempt except explicit 429/quota rejections (those are
    bounded by the safety window instead)."""
    return (DeliveryAttempt.objects.filter(delivery=delivery, generation=delivery.retry_generation)
            .exclude(outcome__in=_NONACCEPTED_OUTCOMES).count())


_DEFAULT = object()


class OutboxWorker:
    def __init__(self, config=None, *, transport=None, limiter=_DEFAULT, stop_event=None, clock=time.monotonic,
                 worker_id=None):
        """``limiter=None`` disables pacing (tests); the default is the process-wide account limiter."""
        self.config = config or OutboxConfig()
        self.config.validate()
        self._rate_limited = False
        self._transport = transport
        self.limiter = account_limiter() if limiter is _DEFAULT else limiter
        self.stop_event = stop_event or threading.Event()
        self.clock = clock
        self.worker_id = worker_id or uuid.uuid4().hex[:8]

    # ------------------------------------------------------------ helpers

    def transport(self):
        return self._transport if self._transport is not None else get_transport(timeout=self.config.timeout)

    def _log(self, level, event, delivery, **fields):
        parts = [f'{event} worker={self.worker_id} delivery={delivery.pk} broadcast={delivery.broadcast_id} '
                 f'generation={delivery.retry_generation} attempt={delivery.attempt_count} '
                 f'state={delivery.state}']
        parts.extend(f'{key}={value}' for key, value in fields.items())
        logger.log(level, ' '.join(parts))

    def _attempt_row(self, delivery, attempt, now, outcome='', error_code='', finished=True):
        return DeliveryAttempt.objects.create(
            delivery=delivery, generation=delivery.retry_generation, attempt=attempt, started_at=now,
            finished_at=now if finished else None, outcome=outcome, error_code=error_code)

    def _clear_lease(self, delivery):
        delivery.lease_token = None
        delivery.lease_expires_at = None

    def _window_at(self, delivery, now, delay):
        """``now + delay`` if a bounded send started then still ends inside the
        safety window, else None. ``delay`` is clamped first (huge Retry-After)."""
        deadline = delivery.idempotency_deadline_at or (now + self.config.safety_window)
        room = (deadline - now).total_seconds() - self.config.timeout
        if room < 0:
            return None
        delay = max(0.0, float(delay))
        if delay > room:
            return None
        return now + timedelta(seconds=delay)

    def _retry_at(self, delivery, now, retry_after=None):
        """Next budget-counted same-key attempt time, or None when the budget
        or safety window is spent."""
        used = _budget_used(delivery)
        if used >= self.config.max_attempts:
            return None
        delays = self.config.retry_delays
        delay = delays[min(max(used, 1), len(delays)) - 1]
        if retry_after:
            delay = max(delay, retry_after)
        return self._window_at(delivery, now, delay)

    def _finish(self, broadcast, delivery, state, *, error_code='', nonacceptance_known=False):
        delivery.state = state
        delivery.error_code = error_code
        delivery.nonacceptance_known = nonacceptance_known
        delivery.next_attempt_at = None
        self._clear_lease(delivery)
        delivery.save()
        broadcasts.refresh_state(broadcast)

    def _requeue(self, broadcast, delivery, at, error_code):
        delivery.state = DeliveryState.QUEUED
        delivery.next_attempt_at = at
        delivery.error_code = error_code
        self._clear_lease(delivery)
        delivery.save()
        broadcasts.refresh_state(broadcast)

    # ------------------------------------------------------------ claim / release / recovery

    def claim_batch(self, limit=None):
        limit = limit or self.config.batch_size
        now = timezone.now()
        expires = now + timedelta(seconds=self.config.lease_seconds)
        with transaction.atomic():
            rows = list(Delivery.objects.select_for_update(skip_locked=True)
                        .filter(state=DeliveryState.QUEUED)
                        .filter(Q(next_attempt_at__lte=now) | Q(next_attempt_at__isnull=True))
                        .order_by('next_attempt_at', 'id')[:limit])
            for row in rows:
                row.state = DeliveryState.CLAIMED
                row.lease_token = uuid.uuid4()
                row.lease_expires_at = expires
                row.updated_at = now
            Delivery.objects.bulk_update(rows, ['state', 'lease_token', 'lease_expires_at', 'updated_at'])
        return [Claim(row.pk, row.lease_token) for row in rows]

    def renew(self, claims):
        """Extend leases of still-held, not yet dispatched claims."""
        if not claims:
            return 0
        now = timezone.now()
        query = Q()
        for claim in claims:
            query |= Q(pk=claim.delivery_id, lease_token=claim.token)
        return Delivery.objects.filter(query, state=DeliveryState.CLAIMED, lease_expires_at__gt=now).update(
            lease_expires_at=now + timedelta(seconds=self.config.lease_seconds), updated_at=now)

    def release(self, claims):
        """Return unused claims to the queue (token-checked; nothing was sent)."""
        if not claims:
            return 0
        now = timezone.now()
        query = Q()
        for claim in claims:
            query |= Q(pk=claim.delivery_id, lease_token=claim.token)
        released = Delivery.objects.filter(query, state=DeliveryState.CLAIMED).update(
            state=DeliveryState.QUEUED, lease_token=None, lease_expires_at=None, updated_at=now)
        if released:
            logger.info('outbox_claims_released worker=%s count=%s', self.worker_id, released)
        return released

    def recover_expired(self):
        """Requeue expired claims; resolve interrupted handoffs (expired ``sending``)."""
        now = timezone.now()
        requeued = Delivery.objects.filter(state=DeliveryState.CLAIMED, lease_expires_at__lt=now).update(
            state=DeliveryState.QUEUED, lease_token=None, lease_expires_at=None, updated_at=now)
        if requeued:
            logger.warning('outbox_lease_recovered worker=%s state=claimed count=%s', self.worker_id, requeued)
        stuck = (Delivery.objects.filter(state=DeliveryState.SENDING, lease_expires_at__lt=now)
                 .order_by('broadcast_id', 'id').values_list('broadcast_id', 'id'))
        by_broadcast = {}
        for broadcast_id, delivery_id in stuck:
            by_broadcast.setdefault(broadcast_id, []).append(delivery_id)
        recovered = 0
        for broadcast_id, ids in by_broadcast.items():
            with transaction.atomic():
                broadcast = Broadcast.objects.select_for_update().get(pk=broadcast_id)
                rows = list(Delivery.objects.select_for_update()
                            .filter(pk__in=ids, state=DeliveryState.SENDING, lease_expires_at__lt=now)
                            .order_by('id'))
                for delivery in rows:
                    self._recover_sending(broadcast, delivery, now)
                    recovered += 1
        return requeued + recovered

    def _recover_sending(self, broadcast, delivery, now):
        DeliveryAttempt.objects.filter(delivery=delivery, generation=delivery.retry_generation,
                                       attempt=delivery.attempt_count, outcome='').update(
            outcome=DeliveryAttempt.Outcome.UNCERTAIN, error_code=INTERRUPTED, finished_at=now)
        window_ok = (delivery.idempotency_deadline_at is not None
                     and now + timedelta(seconds=self.config.timeout) <= delivery.idempotency_deadline_at)
        if _budget_used(delivery) < self.config.max_attempts and window_ok and delivery.request_snapshot:
            # Same request/key replay; the provider deduplicates an accepted original.
            self._requeue(broadcast, delivery, now, INTERRUPTED)
        else:
            self._finish(broadcast, delivery, DeliveryState.UNCERTAIN,
                         error_code=INTERRUPTED if window_ok else WINDOW_EXPIRED)
        self._log(logging.WARNING, 'outbox_lease_recovered', delivery, code=delivery.error_code)

    # ------------------------------------------------------------ one delivery

    def prepare(self, claim):
        """Validate the claim and persist ``sending``. Returns ``_Prepared`` or
        an outcome string when nothing is to be handed off."""
        now = timezone.now()
        broadcast_id = Delivery.objects.filter(pk=claim.delivery_id).values_list('broadcast_id', flat=True).first()
        if broadcast_id is None:
            return 'lost'
        from . import automations

        reminder = automations.reminder_run_for_broadcast(broadcast_id)
        with transaction.atomic():
            # Automated reminders: lock order event -> broadcast -> delivery, so a
            # reschedule/unpublish either commits first (and is seen below) or waits.
            event = (Event.objects.select_for_update().filter(pk=reminder.event_id).first()
                     if reminder is not None else None)
            broadcast, delivery = _lock_pair(broadcast_id, claim.delivery_id)
            if delivery.state != DeliveryState.CLAIMED or delivery.lease_token != claim.token:
                return 'lost'
            if delivery.lease_expires_at is None or delivery.lease_expires_at <= now:
                # Never hand off on an expired claim; nothing was sent, so requeue.
                delivery.state = DeliveryState.QUEUED
                self._clear_lease(delivery)
                delivery.save()
                self._log(logging.WARNING, 'outbox_claim_expired', delivery)
                return 'expired'
            next_attempt = delivery.attempt_count + 1
            stale = automations.stale_reminder_code(reminder, event, now) if reminder is not None else None
            if stale:
                # Never hand off an obsolete reminder, not even a same-key replay;
                # keep possible earlier acceptance visible as uncertain.
                code = automations.DELIVERY_CODES[stale]
                state = DeliveryState.UNCERTAIN if _ambiguous_history(delivery) else DeliveryState.CANCELLED
                self._attempt_row(delivery, next_attempt, now, DeliveryAttempt.Outcome.SKIPPED, code)
                self._finish(broadcast, delivery, state, error_code=code)
                automations.mark_run_cancelled(reminder.pk, stale)
                self._log(logging.INFO, 'outbox_result', delivery, outcome=str(state), code=code)
                return str(state)
            active = get_user_model().objects.filter(pk=delivery.student_id, is_active=True).exists()
            if not active:
                # If an earlier same-key attempt may have been accepted, keep that
                # evidence as uncertain rather than claiming the recipient was skipped.
                state = DeliveryState.UNCERTAIN if _ambiguous_history(delivery) else DeliveryState.SKIPPED
                self._attempt_row(delivery, next_attempt, now, DeliveryAttempt.Outcome.SKIPPED, RECIPIENT_INACTIVE)
                self._finish(broadcast, delivery, state, error_code=RECIPIENT_INACTIVE)
                self._log(logging.INFO, 'outbox_result', delivery, outcome='skipped', code=RECIPIENT_INACTIVE)
                return str(state)

            if delivery.request_snapshot is None:
                key = delivery_idempotency_key(delivery)
                snapshot = build_request_snapshot(broadcast, delivery)
                try:
                    message = message_from_snapshot(snapshot, key)
                except (InvalidMessage, KeyError):
                    # No request was formed or handed off: known nonacceptance.
                    self._attempt_row(delivery, next_attempt, now, DeliveryAttempt.Outcome.FAILED, INVALID_MESSAGE)
                    self._finish(broadcast, delivery, DeliveryState.FAILED, error_code=INVALID_MESSAGE,
                                 nonacceptance_known=True)
                    self._log(logging.WARNING, 'outbox_result', delivery, outcome='failed', code=INVALID_MESSAGE)
                    return 'failed'
                delivery.idempotency_key = key
                delivery.request_snapshot = snapshot
                delivery.request_hash = request_hash(snapshot, key)
                delivery.first_dispatch_at = now
                delivery.idempotency_deadline_at = now + self.config.safety_window
            else:
                deadline = delivery.idempotency_deadline_at
                if deadline is None or now + timedelta(seconds=self.config.timeout) > deadline:
                    # Outside the safety window nothing is replayed, with this key or a new one.
                    ambiguous = _ambiguous_history(delivery)
                    self._attempt_row(delivery, next_attempt, now, DeliveryAttempt.Outcome.UNCERTAIN
                                      if ambiguous else DeliveryAttempt.Outcome.FAILED, WINDOW_EXPIRED)
                    self._finish(broadcast, delivery,
                                 DeliveryState.UNCERTAIN if ambiguous else DeliveryState.FAILED,
                                 error_code=WINDOW_EXPIRED, nonacceptance_known=not ambiguous)
                    self._log(logging.WARNING, 'outbox_result', delivery, outcome=delivery.state,
                              code=WINDOW_EXPIRED)
                    return delivery.state
                try:
                    if request_hash(delivery.request_snapshot, delivery.idempotency_key) != delivery.request_hash:
                        raise InvalidMessage('request snapshot hash mismatch')
                    message = message_from_snapshot(delivery.request_snapshot, delivery.idempotency_key)
                except (InvalidMessage, KeyError, TypeError, AttributeError):
                    # A request under this key was formed before: never send a different one.
                    self._attempt_row(delivery, next_attempt, now, DeliveryAttempt.Outcome.UNCERTAIN,
                                      SNAPSHOT_MISMATCH)
                    self._finish(broadcast, delivery, DeliveryState.UNCERTAIN, error_code=SNAPSHOT_MISMATCH)
                    self._log(logging.ERROR, 'outbox_result', delivery, outcome='uncertain', code=SNAPSHOT_MISMATCH)
                    return 'uncertain'

            delivery.attempt_count = next_attempt
            delivery.state = DeliveryState.SENDING
            delivery.lease_expires_at = now + timedelta(seconds=self.config.lease_seconds)
            delivery.save()
            self._attempt_row(delivery, next_attempt, now, finished=False)
            broadcasts.refresh_state(broadcast)
            self._log(logging.INFO, 'outbox_sending', delivery)
        return _Prepared(claim, broadcast_id, delivery.retry_generation, next_attempt, message)

    def send(self, prepared):
        try:
            result = self.transport().send(prepared.message)
        except Exception:  # transports should not raise; anything here is unresolved
            result = SendResult.uncertain(TRANSPORT_EXCEPTION)
        if not isinstance(result, SendResult):
            result = SendResult.uncertain(TRANSPORT_EXCEPTION)
        return result

    @staticmethod
    def _result_outcome(result):
        Outcome = DeliveryAttempt.Outcome
        if result.status is SendStatus.ACCEPTED:
            return Outcome.ACCEPTED if (result.provider_message_id or '').strip() else Outcome.UNCERTAIN
        if result.status is SendStatus.FAILED:
            return Outcome.FAILED
        if is_rate_limit(result):
            return Outcome.RETRYABLE
        return Outcome.UNCERTAIN

    def complete(self, prepared, result):
        """Persist the outcome only if this worker still owns the lease."""
        now = timezone.now()
        with transaction.atomic():
            broadcast, delivery = _lock_pair(prepared.broadcast_id, prepared.claim.delivery_id)
            attempt = DeliveryAttempt.objects.filter(delivery=delivery, generation=prepared.generation,
                                                     attempt=prepared.attempt).first()
            if (delivery.state != DeliveryState.SENDING or delivery.lease_token != prepared.claim.token
                    or delivery.retry_generation != prepared.generation
                    or delivery.attempt_count != prepared.attempt):
                return self._complete_stale(prepared, result, broadcast, delivery, attempt, now)
            outcome = self._apply(broadcast, delivery, result, now)
            if attempt is not None:
                attempt.outcome = outcome
                attempt.error_code = '' if outcome == DeliveryAttempt.Outcome.ACCEPTED else delivery.error_code
                attempt.provider_email_id = delivery.provider_email_id
                attempt.finished_at = now
                attempt.save(update_fields=['outcome', 'error_code', 'provider_email_id', 'finished_at'])
            level = logging.INFO if outcome == DeliveryAttempt.Outcome.ACCEPTED else logging.WARNING
            self._log(level, 'outbox_result', delivery, outcome=outcome, code=delivery.error_code or None,
                      provider_id=delivery.provider_email_id or None,
                      next_attempt_at=delivery.next_attempt_at.isoformat() if delivery.next_attempt_at else None)
        return outcome

    def _complete_stale(self, prepared, result, broadcast, delivery, attempt, now):
        """The lease was lost (recovered by another pass) while this send ran.
        Never overwrite the newer state; keep the late result as evidence on
        the attempt row. A late acceptance with an ID resolves a same-generation
        ``uncertain`` delivery to ``sent`` (the provider accepted this exact
        request/key)."""
        late = self._result_outcome(result)
        provider_id = (result.provider_message_id or '').strip()[:128] if late == 'accepted' else ''
        if attempt is not None:
            if not attempt.outcome:
                attempt.outcome = DeliveryAttempt.Outcome.LEASE_LOST
                attempt.finished_at = now
            attempt.late_outcome = late
            attempt.late_error_code = '' if late == 'accepted' else _safe_code(result.code, result.status.value.upper())
            attempt.late_recorded_at = now
            if provider_id:
                attempt.provider_email_id = provider_id
            attempt.save(update_fields=['outcome', 'finished_at', 'late_outcome', 'late_error_code',
                                        'late_recorded_at', 'provider_email_id'])
        logger.warning('outbox_stale_completion worker=%s delivery=%s generation=%s attempt=%s '
                       'status=%s code=%s provider_id=%s', self.worker_id, prepared.claim.delivery_id,
                       prepared.generation, prepared.attempt, result.status.value, result.code,
                       provider_id or None)
        if (provider_id and delivery.state == DeliveryState.UNCERTAIN
                and delivery.retry_generation == prepared.generation):
            previous = delivery.error_code
            delivery.provider_email_id = provider_id
            delivery.sent_at = now
            self._finish(broadcast, delivery, DeliveryState.SENT)
            self._log(logging.WARNING, 'outbox_late_acceptance', delivery, provider_id=provider_id,
                      previous_code=previous or None)
            return 'late_accepted'
        return 'stale'

    def _apply(self, broadcast, delivery, result, now):
        """Map a current-lease transport result; returns the attempt outcome."""
        Outcome = DeliveryAttempt.Outcome
        status = result.status
        provider_id = (result.provider_message_id or '').strip()
        if status is SendStatus.ACCEPTED and provider_id:
            delivery.provider_email_id = provider_id[:128]
            delivery.sent_at = now
            self._finish(broadcast, delivery, DeliveryState.SENT)
            return Outcome.ACCEPTED
        if status is SendStatus.ACCEPTED:
            status, code = SendStatus.UNCERTAIN, 'MALFORMED_SUCCESS'  # never fabricate sent
        else:
            code = _safe_code(result.code, status.value.upper())
        earlier_ambiguous = _ambiguous_history(delivery, exclude_attempt=delivery.attempt_count)

        if status is SendStatus.FAILED:
            if code in PAYLOAD_CONFLICT_CODES:
                # A different payload exists under this key: investigate, never rotate the key.
                self._finish(broadcast, delivery, DeliveryState.FAILED, error_code=PAYLOAD_CONFLICT)
            elif earlier_ambiguous:
                # An earlier same-key attempt may have been accepted.
                self._finish(broadcast, delivery, DeliveryState.UNCERTAIN, error_code=code)
            else:
                self._finish(broadcast, delivery, DeliveryState.FAILED, error_code=code, nonacceptance_known=True)
            return Outcome.FAILED

        if is_rate_limit(result):
            # Explicit 429/quota non-acceptance: pause the whole process, end the
            # batch, and retry the same request later without spending the
            # attempt budget; only the safety window bounds these.
            pause = float(result.retry_after) if result.retry_after else RATE_LIMIT_DEFAULT_SECONDS
            pause = min(max(pause, 1.0), self.config.safety_window.total_seconds())
            if self.limiter:
                self.limiter.defer(pause)
            self._rate_limited = True
            at = self._window_at(delivery, now, pause)
            if at is not None:
                self._requeue(broadcast, delivery, at, code)
            elif earlier_ambiguous:
                self._finish(broadcast, delivery, DeliveryState.UNCERTAIN, error_code=code)
            else:
                # Every handoff of this generation was explicitly not accepted.
                self._finish(broadcast, delivery, DeliveryState.FAILED, error_code=code, nonacceptance_known=True)
            return Outcome.RETRYABLE

        # Uncertain: timeout, connection, 5xx, malformed success, concurrent-key
        # 409 (an earlier request with this key may still be accepted), unknown.
        at = self._retry_at(delivery, now, result.retry_after)
        if at is not None:
            self._requeue(broadcast, delivery, at, code)  # same request/key later
        else:
            self._finish(broadcast, delivery, DeliveryState.UNCERTAIN, error_code=code)
        return Outcome.UNCERTAIN

    def process(self, claim, summary=None):
        """Prepare, hand off (no lock held) and complete one claim. Only real
        provider handoffs count toward ``summary.handoffs``."""
        prepared = self.prepare(claim)
        if not isinstance(prepared, _Prepared):
            return prepared
        if summary is not None:
            summary.handoffs += 1
        result = self.send(prepared)
        return self.complete(prepared, result)

    # ------------------------------------------------------------ loops

    def _wait(self, seconds, dispatch_deadline):
        """Wait for a pacing slot or rate-limit pause; returns a stop reason or ''."""
        if seconds <= 0:
            return ''
        if dispatch_deadline is not None and self.clock() + seconds >= dispatch_deadline:
            return 'budget'
        return 'signal' if self.stop_event.wait(seconds) else ''

    def _drain(self, summary, dispatch_deadline, max_sends):
        """Claim and process batches until nothing is due, the budget is spent
        or a stop is requested. Returns the stop reason ('' when drained)."""
        while True:
            reason = self._stop_reason(summary, dispatch_deadline, max_sends)
            if not reason and self.limiter:
                # A 429/quota pause is waited out before claiming, never while holding claims.
                reason = self._wait(self.limiter.pending(), dispatch_deadline)
            if reason:
                return reason
            limit = self.config.batch_size
            if max_sends is not None:
                limit = max(1, min(limit, max_sends - summary.handoffs))
            claims = self.claim_batch(limit)
            summary.claimed += len(claims)
            if not claims:
                return ''
            pending = list(claims)
            try:
                reason = self._process_batch(summary, pending, dispatch_deadline, max_sends)
            finally:
                # Signal, budget, cap, rate limit or an unexpected error: unused
                # claims go back now rather than waiting for lease expiry.
                if pending:
                    try:
                        summary.released += self.release(pending)
                    except Exception:  # pragma: no cover - recovery requeues them later
                        logger.exception('outbox_release_failed worker=%s count=%s', self.worker_id, len(pending))
            if reason:
                return reason

    def _stop_reason(self, summary, dispatch_deadline, max_sends):
        if self.stop_event.is_set():
            return 'signal'
        if dispatch_deadline is not None and self.clock() >= dispatch_deadline:
            return 'budget'
        if max_sends is not None and summary.handoffs >= max_sends:
            return 'max_sends'
        return ''

    def _process_batch(self, summary, pending, dispatch_deadline, max_sends):
        """Process claims in order, popping each from ``pending`` as it is used.
        Returns a stop reason, or '' when the batch is done or was ended by a
        429/quota response (the caller releases what is left)."""
        while pending:
            reason = self._stop_reason(summary, dispatch_deadline, max_sends)
            if not reason and self.limiter:
                reason = self._wait(self.limiter.reserve(), dispatch_deadline)
                self.renew(pending)  # pacing waits never eat into held leases
            if reason:
                return reason
            claim = pending.pop(0)
            self._rate_limited = False
            summary.count(self.process(claim, summary))
            if self._rate_limited:
                self._rate_limited = False
                return ''
            self.renew(pending)
        return ''

    def run_once(self, budget_seconds=240.0, max_sends=None):
        """Bounded pass for Cron-started containers (D5): scheduler tick, lease
        recovery, drain while a bounded send still fits the wall budget, release
        unused claims, return."""
        start = self.clock()
        summary = RunSummary(mode='once')
        reserve = self.config.timeout + self.config.shutdown_margin
        dispatch_deadline = start + max(0.0, budget_seconds - reserve)
        try:
            summary.materialized = scheduler_tick(timezone.now()) or 0
            summary.recovered = self.recover_expired()
            summary.stop_reason = self._drain(summary, dispatch_deadline, max_sends) or 'drained'
        finally:
            summary.elapsed_seconds = self.clock() - start
            logger.info('outbox_run worker=%s %s', self.worker_id, json.dumps(summary.as_dict(), sort_keys=True))
        return summary

    def run_forever(self, max_sends=None):
        """Continuous local/operator mode: poll until a stop is requested."""
        start = self.clock()
        summary = RunSummary(mode='continuous')
        try:
            while not self.stop_event.is_set():
                summary.materialized += scheduler_tick(timezone.now()) or 0
                summary.recovered += self.recover_expired()
                reason = self._drain(summary, None, max_sends)
                if reason in ('signal', 'max_sends'):
                    summary.stop_reason = reason
                    break
                self.stop_event.wait(self.config.poll_seconds)
            summary.stop_reason = summary.stop_reason or 'signal'
        finally:
            summary.elapsed_seconds = self.clock() - start
            logger.info('outbox_run worker=%s %s', self.worker_id, json.dumps(summary.as_dict(), sort_keys=True))
        return summary
