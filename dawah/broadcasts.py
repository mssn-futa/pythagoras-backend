"""Broadcast drafts, audiences, queue snapshots and retry eligibility (D4).

Every write locks the broadcast row (``select_for_update``) and then, when it
touches them, its deliveries in ascending id order. Queue only persists work:
it never sends mail or calls a provider; the COMM-02 worker drains
``Delivery`` rows in state ``queued``.
"""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q, Value
from django.db.models.functions import Lower, Trim
from django.shortcuts import get_object_or_404
from django.utils import timezone

from pythagoras.utils import APIError

from . import broadcast_content
from .models import (
    OUTSTANDING_DELIVERY_STATES, AudienceType, BodyFormat, Broadcast, BroadcastState, Delivery,
    DeliveryGeneration, DeliveryState,
)

COUNT_KEYS = ('queued', 'sent', 'failed', 'uncertain', 'skipped', 'cancelled')
NO_RECIPIENTS = 'No active students match this audience.'


# ---------------------------------------------------------------- errors

def version_conflict(current):
    return APIError('BROADCAST_VERSION_CONFLICT',
                    'This draft changed since you loaded it. Reload it before continuing.',
                    status_code=409, fields={'version': [f'The current version is {current}.']})


def already_queued():
    return APIError('BROADCAST_ALREADY_QUEUED',
                    'This broadcast has already been queued; its content and audience cannot change.',
                    status_code=409)


# ---------------------------------------------------------------- audience

def audience_recipients(audience_type, department='', level=''):
    """Active student-role accounts matching the audience (D1 normalization:
    department trimmed and case-insensitive, level exact)."""
    users = get_user_model().objects.filter(role='student', is_active=True).exclude(email='')
    if audience_type == AudienceType.DEPARTMENT:
        users = users.annotate(_department=Lower(Trim('department'))).filter(
            _department=Lower(Value(department.strip())))
    elif audience_type == AudienceType.LEVEL:
        users = users.filter(level=level)
    elif audience_type != AudienceType.ALL:
        raise ValueError(f'Unknown audience type {audience_type!r}')
    return users


def broadcast_recipients(broadcast):
    return audience_recipients(broadcast.audience_type, broadcast.audience_department, broadcast.audience_level)


# ---------------------------------------------------------------- counts/state

def _count(states):
    return Count('deliveries', filter=Q(deliveries__state__in=states))


def with_counts(queryset):
    """Annotate delivery counts (``queued`` covers every unresolved delivery:
    queued/claimed/sending) and the number of retry-eligible failures."""
    return queryset.annotate(
        count_queued=_count(OUTSTANDING_DELIVERY_STATES),
        count_sent=_count([DeliveryState.SENT]),
        count_failed=_count([DeliveryState.FAILED]),
        count_uncertain=_count([DeliveryState.UNCERTAIN]),
        count_skipped=_count([DeliveryState.SKIPPED]),
        count_cancelled=_count([DeliveryState.CANCELLED]),
        # Same rule as Delivery.is_retry_eligible / RETRY_ELIGIBLE.
        count_retryable=Count('deliveries', filter=Q(deliveries__state=DeliveryState.FAILED,
                                                     deliveries__nonacceptance_known=True,
                                                     deliveries__provider_email_id='',
                                                     deliveries__sent_at__isnull=True)),
    )


def counts_of(broadcast):
    """Counts from annotations (``with_counts``) or, if absent, one query."""
    if not hasattr(broadcast, 'count_queued'):
        broadcast = with_counts(Broadcast.objects.filter(pk=broadcast.pk)).get()
    return {key: getattr(broadcast, f'count_{key}') for key in COUNT_KEYS}


def retryable_count(broadcast):
    if not hasattr(broadcast, 'count_retryable'):
        broadcast = with_counts(Broadcast.objects.filter(pk=broadcast.pk)).get()
    return broadcast.count_retryable


def derive_state(counts, any_dispatched):
    """Aggregate broadcast state from its deliveries (D4): uncertain wins, then
    outstanding work, then terminal errors/completed."""
    if counts['uncertain']:
        return BroadcastState.UNCERTAIN
    if counts['queued']:
        return BroadcastState.PROCESSING if any_dispatched else BroadcastState.QUEUED
    if counts['failed']:
        return BroadcastState.COMPLETED_WITH_ERRORS
    return BroadcastState.COMPLETED


def refresh_state(broadcast):
    """Recompute and store a queued broadcast's aggregate state. The caller
    holds the broadcast row lock (COMM-02 calls this after outcome writes)."""
    counts = counts_of(Broadcast(pk=broadcast.pk))
    dispatched = Delivery.objects.filter(broadcast=broadcast).filter(
        Q(attempt_count__gt=0) | Q(retry_generation__gt=0)
        | ~Q(state__in=OUTSTANDING_DELIVERY_STATES)).exists()
    state = derive_state(counts, dispatched)
    if state != broadcast.state:
        broadcast.state = state
        broadcast.save(update_fields=['state', 'updated_at'])
    return broadcast


def eligible_actions(broadcast):
    if broadcast.is_draft:
        return ['edit', 'delete', 'preview', 'queue']
    return ['retry'] if retryable_count(broadcast) else []


# ---------------------------------------------------------------- drafts

def _lock(broadcast_id):
    return get_object_or_404(Broadcast.objects.select_for_update(), pk=broadcast_id)


def _locked_draft(broadcast_id, version):
    broadcast = _lock(broadcast_id)
    if not broadcast.is_draft:
        raise already_queued()
    if version != broadcast.version:
        raise version_conflict(broadcast.version)
    return broadcast


def update_draft(broadcast_id, version, make_serializer):
    """Optimistic update: the draft row is locked, its version must match and
    each successful save increments it."""
    with transaction.atomic():
        broadcast = _locked_draft(broadcast_id, version)
        serializer = make_serializer(broadcast)
        serializer.is_valid(raise_exception=True)
        broadcast = serializer.save(version=broadcast.version + 1)
    return broadcast


def delete_draft(broadcast_id):
    with transaction.atomic():
        broadcast = _lock(broadcast_id)
        if not broadcast.is_draft:
            raise already_queued()
        broadcast.delete()


def _checked_render(broadcast):
    """Stored drafts were validated on save; recheck (and re-sanitize rich
    HTML) before preview/queue so a draft edited outside the API cannot freeze
    invalid or unsafe content. Returns the rendering preview shows and queue
    freezes."""
    errors = {}
    subject = (broadcast.subject or '').strip()
    if not subject:
        errors['subject'] = ['Enter a subject.']
    elif len(subject) > broadcast_content.SUBJECT_MAX_LENGTH or broadcast_content.subject_error(subject):
        errors['subject'] = [broadcast_content.subject_error(subject) or 'Subject is too long.']
    if broadcast.body_format == BodyFormat.RICH:
        try:
            rendered = broadcast_content.render(broadcast)
        except broadcast_content.ContentError as error:
            errors['body_html'] = [str(error)]
    elif broadcast.body_format == BodyFormat.PLAIN:
        body = broadcast_content.normalize_body(broadcast.body or '')
        if not body.strip():
            errors['body'] = ['Enter the message text.']
        elif broadcast_content.body_error(body):
            errors['body'] = [broadcast_content.body_error(body)]
        else:
            rendered = broadcast_content.render(broadcast)
    else:
        errors['body_format'] = ['Choose "plain" or "rich".']
    if not errors and len(rendered.html) > broadcast_content.EMAIL_HTML_MAX_LENGTH:
        errors['body_html' if broadcast.body_format == BodyFormat.RICH else 'body'] = [
            'The rendered email is too large. Shorten the message.']
    if errors:
        raise APIError('VALIDATION_ERROR', 'Correct the highlighted fields.', status_code=400, fields=errors)
    return rendered


def preview(broadcast_id, version):
    """Rendered content and current audience count. Read-only: no lock, no
    snapshot, no delivery rows, no provider call."""
    broadcast = get_object_or_404(Broadcast, pk=broadcast_id)
    if not broadcast.is_draft:
        raise already_queued()
    if version != broadcast.version:
        raise version_conflict(broadcast.version)
    rendered = _checked_render(broadcast)
    return broadcast, rendered, broadcast_recipients(broadcast).count()


# ---------------------------------------------------------------- queue

def queue(broadcast_id, version, user):
    """Freeze content and the recipient snapshot in one transaction.

    Returns ``(broadcast, replayed)``. A replay of the queued version returns
    the existing broadcast; a different version of a queued broadcast is
    ``BROADCAST_ALREADY_QUEUED``. Zero recipients is a validation error raised
    before anything is written, so the draft is unchanged."""
    with transaction.atomic():
        broadcast = _lock(broadcast_id)
        if not broadcast.is_draft:
            if version == broadcast.version:
                return broadcast, True
            raise already_queued()
        if version != broadcast.version:
            raise version_conflict(broadcast.version)
        rendered = _checked_render(broadcast)
        recipients = list(broadcast_recipients(broadcast).order_by('id').values_list('id', 'email'))
        if not recipients:
            raise APIError('VALIDATION_ERROR', NO_RECIPIENTS, status_code=400, fields={'audience': [NO_RECIPIENTS]})
        now = timezone.now()
        Delivery.objects.bulk_create([
            Delivery(broadcast=broadcast, student_id=student_id, snapshot_email=email, next_attempt_at=now)
            for student_id, email in recipients
        ])
        broadcast.state = BroadcastState.QUEUED
        broadcast.queued_at = now
        broadcast.queued_by = user
        broadcast.recipient_count = len(recipients)
        broadcast.snapshot_subject = rendered.subject
        broadcast.snapshot_text = rendered.text
        broadcast.snapshot_html = rendered.html
        broadcast.snapshot_template_version = rendered.template_version
        broadcast.save()
    return broadcast, False


# ---------------------------------------------------------------- retry

def retry(broadcast_id, delivery_ids, user):
    """Manual retry of explicitly selected, known-failed deliveries.

    All or nothing: IDs outside this broadcast are a 400, any ineligible
    delivery (sent/uncertain/skipped/in-flight, or failed without positive
    evidence of nonacceptance) a 409, and nothing changes. Each eligible row's
    closing generation is first copied to an append-only ``DeliveryGeneration``
    row; then it starts a new generation (fresh attempt budget and provider
    request identity, safe because nonacceptance is known) and goes back to
    ``queued`` for the COMM-02 worker."""
    with transaction.atomic():
        broadcast = _lock(broadcast_id)
        if broadcast.is_draft:
            raise APIError('BROADCAST_NOT_QUEUED', 'Only queued broadcasts have deliveries to retry.',
                           status_code=409)
        rows = list(Delivery.objects.select_for_update().filter(broadcast=broadcast, pk__in=delivery_ids)
                    .order_by('id'))
        unknown = sorted(set(delivery_ids) - {row.pk for row in rows})
        if unknown:
            raise APIError('VALIDATION_ERROR', 'Correct the highlighted fields.', status_code=400,
                           fields={'delivery_ids': [f'Unknown deliveries for this broadcast: '
                                                    f'{", ".join(map(str, unknown))}.']})
        ineligible = [row.pk for row in rows if not row.is_retry_eligible]
        if ineligible:
            raise APIError('DELIVERY_NOT_RETRYABLE',
                           'Only failed deliveries that the provider did not accept can be retried.',
                           status_code=409,
                           fields={'delivery_ids': [f'Not retryable: {", ".join(map(str, ineligible))}.']})
        now = timezone.now()
        DeliveryGeneration.objects.bulk_create([DeliveryGeneration(
            delivery=row, generation=row.retry_generation, state=row.state, attempt_count=row.attempt_count,
            error_code=row.error_code, nonacceptance_known=row.nonacceptance_known,
            provider_email_id=row.provider_email_id, idempotency_key=row.idempotency_key,
            request_snapshot=row.request_snapshot, request_hash=row.request_hash,
            first_dispatch_at=row.first_dispatch_at, idempotency_deadline_at=row.idempotency_deadline_at,
            requested_at=row.retry_requested_at, requested_by_id=row.retry_requested_by_id,
            closed_at=now, closed_by=user) for row in rows])
        for row in rows:
            row.nonacceptance_known = False
            row.state = DeliveryState.QUEUED
            row.retry_generation += 1
            row.attempt_count = 0
            row.next_attempt_at = now
            row.error_code = ''
            row.lease_token = row.lease_expires_at = None
            row.idempotency_key = row.request_hash = ''
            row.request_snapshot = row.first_dispatch_at = row.idempotency_deadline_at = None
            row.retry_requested_at, row.retry_requested_by = now, user
            row.updated_at = now  # bulk_update does not apply auto_now
        Delivery.objects.bulk_update(rows, [
            'state', 'nonacceptance_known', 'retry_generation', 'attempt_count', 'next_attempt_at', 'error_code', 'lease_token',
            'lease_expires_at', 'idempotency_key', 'request_hash', 'request_snapshot', 'first_dispatch_at',
            'idempotency_deadline_at', 'retry_requested_at', 'retry_requested_by', 'updated_at'])
        refresh_state(broadcast)
    return broadcast
