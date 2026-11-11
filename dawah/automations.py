"""Weekly digests and event reminders (D4 "Automated summaries and event reminders", COMM-06).

Rules (``AutomationRule``) start disabled. Each outbox pass calls
``tick(now)`` (via ``outbox.scheduler_tick``), which, per enabled rule and
inside one transaction holding that rule's row lock, records every due
occurrence as one ``AutomationRun`` (unique per rule/``occurrence_key``) and,
when there is content and an audience, materializes it into one ordinary
broadcast through ``broadcasts.queue`` (same sanitizer, template, recipient
snapshot, deliveries and outbox worker as manual broadcasts). A repeated or
concurrent tick finds the existing run and creates nothing.

Occurrences
    Weekly: ``rule-{id}:{scheduled_at UTC}`` at ``weekday``/``local_time`` in
    ``timezone``; the window is the seven days ending at the scheduled
    occurrence. Only the latest due occurrence inside the catch-up window
    (default one day) is sent; older unprocessed occurrences inside the
    bounded horizon (28 days) are recorded ``missed``.
    Reminder: ``rule-{id}:event-{event_id}:rev-{schedule_revision}`` at
    ``start_time - offset_minutes``; sent only inside the grace (default 15
    minutes) and before the event starts, otherwise recorded ``missed``.
    Occurrences scheduled before ``active_from`` (last enable or schedule
    change) are never materialized or recorded.

Cancellation
    Rescheduling (start/end change bumps ``Event.schedule_revision``) or
    unpublishing an event cancels unsent deliveries of its pending reminder
    runs (``cancel_event_reminders``, called by ``services`` under the event
    lock); the outbox rechecks publication/revision/start under the event lock
    before every handoff (``stale_reminder_code``). Deliveries that may already
    have been accepted under a same-key attempt become ``uncertain``, never
    ``cancelled``; sent/sending history is never rewritten.

Lock order: rule, event, broadcast, deliveries (ascending id). No lock is held
during provider I/O (the tick never sends; the worker does).
"""
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models.functions import Lower, Trim
from django.shortcuts import get_object_or_404
from django.utils import timezone

from pythagoras.utils import APIError

from . import broadcast_content, broadcasts
from .models import (
    AUTOMATION_CATEGORIES, AutomationKind, AutomationRule, AutomationRun, AutomationRunState, BodyFormat,
    Broadcast, BroadcastState, Delivery, DeliveryState, Event, EventResource, PublicationStatus,
)

logger = logging.getLogger('dawah.automations')
logger.addHandler(logging.NullHandler())

UTC = dt_timezone.utc
DEFAULT_TIMEZONE = 'Africa/Lagos'
SUPPORTED_TIMEZONES = (DEFAULT_TIMEZONE,)
WEEKLY_WINDOW = timedelta(days=7)
WEEKLY_HORIZON = timedelta(days=28)
REMINDER_HORIZON = timedelta(days=7)
REMINDER_EVENT_LIMIT = 200  # events considered per reminder rule and tick


def weekly_catch_up():
    return timedelta(minutes=getattr(settings, 'AUTOMATION_WEEKLY_CATCH_UP_MINUTES', 24 * 60))


def reminder_grace():
    return timedelta(minutes=getattr(settings, 'AUTOMATION_REMINDER_GRACE_MINUTES', 15))


# Run reason codes (COMM-07 renders unknown ones verbatim).
NO_ELIGIBLE_SUMMARIES = 'no_eligible_summaries'
NO_RECIPIENTS = 'no_recipients'
MISSED_CATCH_UP_WINDOW = 'missed_catch_up_window'
REMINDER_GRACE_ELAPSED = 'reminder_grace_elapsed'
SOURCE_UNPUBLISHED = 'source_unpublished'
SOURCE_RESCHEDULED = 'source_rescheduled'
RULE_DISABLED = 'rule_disabled'
OWNER_INACTIVE = 'owner_inactive'
CONTENT_INVALID = 'content_invalid'
PROVIDER_FAILURE = 'provider_failure'
DELIVERY_UNCERTAIN = 'delivery_uncertain'
# Preview only: no upcoming event matches a reminder rule.
NO_UPCOMING_EVENT = 'no_upcoming_event'

# Safe delivery error codes for cancelled reminder deliveries.
DELIVERY_CODES = {SOURCE_UNPUBLISHED: 'SOURCE_UNPUBLISHED', SOURCE_RESCHEDULED: 'SOURCE_RESCHEDULED',
                  REMINDER_GRACE_ELAPSED: 'EVENT_STARTED'}

# Run states as the API shows them (stored states plus those derived from the broadcast).
RUN_STATES = ['pending', 'queued', 'processing', 'completed', 'completed_with_errors', 'failed', 'uncertain',
              'skipped', 'missed', 'cancelled', 'blocked']
REASON_CODES = [NO_ELIGIBLE_SUMMARIES, NO_RECIPIENTS, MISSED_CATCH_UP_WINDOW, REMINDER_GRACE_ELAPSED,
                SOURCE_UNPUBLISHED, SOURCE_RESCHEDULED, RULE_DISABLED, OWNER_INACTIVE, CONTENT_INVALID,
                PROVIDER_FAILURE, DELIVERY_UNCERTAIN]

# ---------------------------------------------------------------- placeholders

PLACEHOLDERS = {
    AutomationKind.WEEKLY_DIGEST: {
        'week_start': 'First moment of the seven-day window, in the rule time zone.',
        'week_end': 'The scheduled occurrence the window ends at, in the rule time zone.',
        'summary_count': 'Number of events with authored summaries in the window.',
        'summaries': 'The collated event summaries and their visible resource links.',
    },
    AutomationKind.EVENT_REMINDER: {
        'event_title': 'Event title.',
        'event_start': 'Event start, in the rule time zone.',
        'event_location': 'Event location.',
        'event_summary': 'Authored event summary (may be empty).',
        'event_url': 'Link to the event page (empty when FRONTEND_BASE_URL is not configured).',
    },
}

DEFAULT_TEMPLATES = {
    AutomationKind.WEEKLY_DIGEST: {
        'subject': 'Weekly Da’wah summary',
        'body': 'Assalamu alaykum,\n\nHere is what happened from {{week_start}} to {{week_end}}:\n\n{{summaries}}',
    },
    AutomationKind.EVENT_REMINDER: {
        'subject': 'Reminder: {{event_title}}',
        'body': '{{event_title}} starts {{event_start}} at {{event_location}}.\n\n{{event_summary}}\n\n{{event_url}}',
    },
}

_TOKEN = re.compile(r'\{\{(.*?)\}\}', re.S)
_NAME = re.compile(r'\s*([a-z_][a-z0-9_]*)\s*')
_CODE = re.compile(r'\{%|%\}|\{#|#\}')


def template_errors(kind, *texts):
    """Messages for anything that is not a documented placeholder of ``kind``."""
    allowed = PLACEHOLDERS.get(kind, {})
    problems = []
    for text in texts:
        text = text or ''
        for match in _TOKEN.finditer(text):
            name = _NAME.fullmatch(match.group(1))
            if name is None:
                problems.append(f'{match.group(0)} is not allowed: templates support documented placeholders only.')
            elif name.group(1) not in allowed:
                problems.append(f'{{{{{name.group(1)}}}}} is not a documented placeholder for this rule type.')
        if _CODE.search(text):
            problems.append('Template code ({% %} or {# #}) is not allowed: use documented placeholders only.')
    return list(dict.fromkeys(problems))


def _substitute(text, values):
    def replace(match):
        name = _NAME.fullmatch(match.group(1))
        return values.get(name.group(1), '') if name else ''
    return _TOKEN.sub(replace, text or '')


# ---------------------------------------------------------------- owner / categories

def owner_blocked_reason(rule):
    owner = rule.owner
    if owner is None or not owner.is_active or getattr(owner, 'role', None) != 'admin':
        return OWNER_INACTIVE
    return None


def rule_categories(rule):
    cats = [str(c).strip().lower() for c in (rule.categories or [])]
    if not cats and rule.kind == AutomationKind.WEEKLY_DIGEST:
        return list(AUTOMATION_CATEGORIES)
    return cats


def _matching_events(rule):
    return (Event.objects.filter(status=PublicationStatus.PUBLISHED)
            .annotate(_category=Lower(Trim('category'))).filter(_category__in=rule_categories(rule)))


# ---------------------------------------------------------------- schedule

def _zone(rule):
    return ZoneInfo(rule.timezone or DEFAULT_TIMEZONE)


def _weekly_at(rule, local_date):
    return datetime.combine(local_date, rule.local_time, tzinfo=_zone(rule)).astimezone(UTC)


def latest_weekly_occurrence(rule, now):
    """The most recent scheduled occurrence at or before ``now`` (UTC)."""
    local_now = now.astimezone(_zone(rule))
    date = local_now.date() - timedelta(days=(local_now.weekday() - rule.weekday) % 7)
    occurrence = _weekly_at(rule, date)
    if occurrence > now:
        occurrence = _weekly_at(rule, date - timedelta(days=7))
    return occurrence


def next_weekly_occurrence(rule, now):
    latest = latest_weekly_occurrence(rule, now)
    return _weekly_at(rule, latest.astimezone(_zone(rule)).date() + timedelta(days=7))


def _iso(moment):
    return moment.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def weekly_key(rule, scheduled_at):
    return f'rule-{rule.pk}:{_iso(scheduled_at)}'


def reminder_key(rule, event):
    return f'rule-{rule.pk}:event-{event.pk}:rev-{event.schedule_revision}'


# ---------------------------------------------------------------- content

def format_local(moment, zone):
    local = moment.astimezone(zone)
    return f'{local:%A} {local.day} {local:%B %Y, %H:%M} ({zone.key})'


def event_url(event):
    base = (getattr(settings, 'FRONTEND_BASE_URL', '') or '').rstrip('/')
    return f'{base}/dawah/events/{event.pk}' if base else ''


def _resource_links(events):
    links = {}
    resources = (EventResource.objects.visible().filter(event__in=[e.pk for e in events])
                 .order_by('event_id', 'id'))
    for resource in resources:
        url = resource.media_url
        if resource.type == EventResource.Type.VIDEO:
            playback = resource.playback
            url = playback['watch_url'] if playback else None
        if url:
            links.setdefault(resource.event_id, []).append((resource.title, url))
    return links


def _paragraphs_html(text):
    blocks = [block.strip() for block in re.split(r'\n\s*\n', text or '') if block.strip()]
    return ''.join('<p>' + '<br>'.join(html.escape(line) for line in block.split('\n')) + '</p>' for block in blocks)


def _event_summary(event):
    return {'id': event.pk, 'title': event.title, 'category': event.category,
            'start_time': _iso(event.start_time)}


@dataclass
class Source:
    """What an occurrence is built from (also its provenance snapshot)."""
    scheduled_at: datetime
    window_start: datetime | None = None
    window_end: datetime | None = None
    events: list = field(default_factory=list)
    event: Event | None = None

    def as_dict(self):
        data = {'window_start': self.window_start and _iso(self.window_start),
                'window_end': self.window_end and _iso(self.window_end),
                'events': [_event_summary(e) for e in self.events]}
        if self.event is not None:
            data['event'] = _event_summary(self.event)
            data['event_revision'] = self.event.schedule_revision
        return data


def weekly_source(rule, scheduled_at):
    start = scheduled_at - WEEKLY_WINDOW
    events = list(_matching_events(rule).filter(start_time__gte=start, start_time__lt=scheduled_at)
                  .exclude(summary__isnull=True).exclude(summary__regex=r'^\s*$').order_by('start_time', 'id'))
    return Source(scheduled_at=scheduled_at, window_start=start, window_end=scheduled_at, events=events)


def placeholder_values(rule, source, rich):
    """Placeholder -> replacement (HTML-escaped markup for rich templates)."""
    zone = _zone(rule)
    esc = html.escape if rich else (lambda value: value)
    if rule.kind == AutomationKind.WEEKLY_DIGEST:
        links = _resource_links(source.events)
        text_parts, html_parts = [], []
        for event in source.events:
            when = format_local(event.start_time, zone)
            lines = [f'{event.title} ({when})', (event.summary or '').strip()]
            lines += [f'- {title}: {url}' for title, url in links.get(event.pk, [])]
            text_parts.append('\n'.join(lines))
            item = f'<h3>{html.escape(event.title)}</h3><p><em>{html.escape(when)}</em></p>'
            item += _paragraphs_html(event.summary)
            if links.get(event.pk):
                item += '<ul>' + ''.join(f'<li><p><a href="{html.escape(url)}">{html.escape(title)}</a></p></li>'
                                         for title, url in links[event.pk]) + '</ul>'
            html_parts.append(item)
        return {
            'week_start': esc(format_local(source.window_start, zone)),
            'week_end': esc(format_local(source.window_end, zone)),
            'summary_count': str(len(source.events)),
            'summaries': ''.join(html_parts) if rich else '\n\n'.join(text_parts),
        }
    event = source.event
    url = event_url(event)
    return {
        'event_title': esc(event.title),
        'event_start': esc(format_local(event.start_time, zone)),
        'event_location': esc(event.location or ''),
        'event_summary': _paragraphs_html(event.summary) if rich else (event.summary or '').strip(),
        'event_url': (f'<a href="{html.escape(url)}">{html.escape(url)}</a>' if rich and url else url),
    }


def _one_line(value):
    value = ''.join(ch if ch.isprintable() else ' ' for ch in value)
    value = ' '.join(value.split())
    limit = broadcast_content.SUBJECT_MAX_LENGTH
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def compose(rule, source):
    """Broadcast content fields for this occurrence: placeholders substituted
    into the stored template, then the shared sanitizer (rich). Raises
    ``ContentError`` when nothing readable is left."""
    rich = rule.body_format == BodyFormat.RICH
    subject = _one_line(_substitute(rule.subject, placeholder_values(rule, source, rich=False)))
    values = placeholder_values(rule, source, rich)
    if rich:
        sanitized = broadcast_content.sanitize_rich_html(_substitute(rule.body_html, values))
        return {'subject': subject, 'body_format': BodyFormat.RICH, 'body_html': sanitized.html,
                'body': sanitized.text}
    body = broadcast_content.normalize_body(_substitute(rule.body, values))
    return {'subject': subject, 'body_format': BodyFormat.PLAIN, 'body_html': None, 'body': body}


def render_preview(rule, source):
    fields = compose(rule, source)
    return broadcast_content.render_content(fields['subject'], fields['body_format'], fields['body'],
                                            fields['body_html'])


# ---------------------------------------------------------------- runs

def _record(rule, key, scheduled_at, **fields):
    """Create the run for this occurrence, or return the existing one (the
    unique constraint is the final guard against concurrent ticks)."""
    try:
        with transaction.atomic():
            return AutomationRun.objects.create(rule=rule, occurrence_key=key, scheduled_at=scheduled_at,
                                                **fields), True
    except IntegrityError:
        return AutomationRun.objects.get(rule=rule, occurrence_key=key), False


def _materialize(rule, run, source):
    """Turn a pending run into a queued broadcast, or a recorded skip/failure.
    Returns 1 when a broadcast was created."""
    if not source.events and rule.kind == AutomationKind.WEEKLY_DIGEST:
        return _close(run, AutomationRunState.SKIPPED, NO_ELIGIBLE_SUMMARIES)
    if not broadcasts.audience_recipients(rule.audience_type, rule.audience_department,
                                          rule.audience_level).exists():
        return _close(run, AutomationRunState.SKIPPED, NO_RECIPIENTS)
    try:
        content = compose(rule, source)
    except broadcast_content.ContentError:
        return _close(run, AutomationRunState.FAILED, CONTENT_INVALID)
    broadcast = Broadcast.objects.create(
        created_by=rule.owner, audience_type=rule.audience_type, audience_department=rule.audience_department,
        audience_level=rule.audience_level, **content)
    try:
        with transaction.atomic():
            broadcast, _ = broadcasts.queue(broadcast.pk, broadcast.version, rule.owner)
    except APIError as error:
        broadcast.delete()
        no_audience = bool(error.fields and 'audience' in error.fields)
        return _close(run, AutomationRunState.SKIPPED if no_audience else AutomationRunState.FAILED,
                      NO_RECIPIENTS if no_audience else CONTENT_INVALID)
    run.broadcast = broadcast
    run.state = AutomationRunState.QUEUED
    run.save(update_fields=['broadcast', 'state', 'updated_at'])
    logger.info('automation_run_queued rule=%s run=%s broadcast=%s recipients=%s', rule.pk, run.pk, broadcast.pk,
                broadcast.recipient_count)
    return 1


def _close(run, state, reason):
    run.state, run.reason_code = state, reason
    run.save(update_fields=['state', 'reason_code', 'updated_at'])
    logger.info('automation_run_closed rule=%s run=%s state=%s reason=%s', run.rule_id, run.pk, state, reason)
    return 0


def _tick_weekly(rule, now):
    latest = latest_weekly_occurrence(rule, now)
    floor = max(rule.active_from or now, now - WEEKLY_HORIZON)
    occurrences = []
    moment = latest
    while moment >= floor:
        occurrences.append(moment)
        moment = _weekly_at(rule, moment.astimezone(_zone(rule)).date() - timedelta(days=7))
    if not occurrences:
        return 0
    keys = {weekly_key(rule, moment): moment for moment in reversed(occurrences)}
    existing = set(AutomationRun.objects.filter(rule=rule, occurrence_key__in=keys).values_list(
        'occurrence_key', flat=True))
    created = 0
    for key, moment in keys.items():
        if key in existing:
            continue
        window = {'window_start': moment - WEEKLY_WINDOW, 'window_end': moment}
        if moment != latest or now - moment > weekly_catch_up():
            _record(rule, key, moment, state=AutomationRunState.MISSED, reason_code=MISSED_CATCH_UP_WINDOW,
                    **window)
            continue
        if owner_blocked_reason(rule):
            _record(rule, key, moment, state=AutomationRunState.BLOCKED, reason_code=OWNER_INACTIVE, **window)
            continue
        source = weekly_source(rule, moment)
        run, new = _record(rule, key, moment, source=source.as_dict(), **window)
        if new:
            created += _materialize(rule, run, source)
    return created


def _tick_reminders(rule, now):
    offset = timedelta(minutes=rule.offset_minutes)
    floor = max(rule.active_from or now, now - REMINDER_HORIZON)
    candidates = list(_matching_events(rule).filter(start_time__lte=now + offset, start_time__gte=floor + offset)
                      .order_by('start_time', 'id').values_list('pk', 'schedule_revision')[:REMINDER_EVENT_LIMIT])
    keys = {f'rule-{rule.pk}:event-{pk}:rev-{revision}': pk for pk, revision in candidates}
    existing = set(AutomationRun.objects.filter(rule=rule, occurrence_key__in=keys).values_list(
        'occurrence_key', flat=True))
    created = 0
    for key, event_id in keys.items():
        if key in existing:
            continue
        # Lock order rule -> event: an overlapping reschedule/unpublish either
        # commits first (we see it and skip) or waits and then cancels our run.
        event = Event.objects.select_for_update().filter(pk=event_id).first()
        if (event is None or event.status != PublicationStatus.PUBLISHED or reminder_key(rule, event) != key
                or event.category.strip().lower() not in rule_categories(rule)):
            continue
        scheduled_at = event.start_time - offset
        if scheduled_at > now:
            continue
        fields = {'event_id': event.pk, 'event_revision': event.schedule_revision}
        source = Source(scheduled_at=scheduled_at, events=[event], event=event)
        if now >= event.start_time or now - scheduled_at > reminder_grace():
            _record(rule, key, scheduled_at, state=AutomationRunState.MISSED, reason_code=REMINDER_GRACE_ELAPSED,
                    source=source.as_dict(), **fields)
            continue
        if owner_blocked_reason(rule):
            _record(rule, key, scheduled_at, state=AutomationRunState.BLOCKED, reason_code=OWNER_INACTIVE,
                    source=source.as_dict(), **fields)
            continue
        run, new = _record(rule, key, scheduled_at, source=source.as_dict(), **fields)
        if new:
            created += _materialize(rule, run, source)
    return created


def tick(now=None):
    """Materialize due occurrences of every enabled rule. Returns the number
    of broadcasts created. Each rule is evaluated in its own transaction under
    its row lock (``SKIP LOCKED``: a concurrent tick leaves it alone); one
    failing rule is logged and does not stop the others or the outbox pass."""
    now = now or timezone.now()
    created = 0
    rule_ids = list(AutomationRule.objects.filter(enabled=True).order_by('id').values_list('pk', flat=True))
    for rule_id in rule_ids:
        try:
            with transaction.atomic():
                rule = (AutomationRule.objects.select_for_update(skip_locked=True)
                        .filter(pk=rule_id, enabled=True).first())
                if rule is None:
                    continue
                if rule.kind == AutomationKind.WEEKLY_DIGEST:
                    created += _tick_weekly(rule, now)
                else:
                    created += _tick_reminders(rule, now)
        except Exception:
            logger.exception('automation_tick_failed rule=%s', rule_id)
    return created


# ---------------------------------------------------------------- cancellation

def _cancel_unsent(broadcast_id, delivery_code):
    """Cancel queued/claimed-before-handoff deliveries of one broadcast.
    Rows with an earlier ambiguous same-key attempt become ``uncertain``
    instead (they may already have been accepted). Returns rows changed."""
    from .outbox import _ambiguous_history  # outbox imports this module lazily too

    broadcast = Broadcast.objects.select_for_update().get(pk=broadcast_id)
    rows = list(Delivery.objects.select_for_update().filter(
        broadcast=broadcast, state__in=[DeliveryState.QUEUED, DeliveryState.CLAIMED]).order_by('id'))
    for row in rows:
        row.state = DeliveryState.UNCERTAIN if _ambiguous_history(row) else DeliveryState.CANCELLED
        row.error_code = delivery_code
        row.next_attempt_at = None
        row.lease_token = row.lease_expires_at = None
        row.save(update_fields=['state', 'error_code', 'next_attempt_at', 'lease_token', 'lease_expires_at',
                                'updated_at'])
    if rows:
        broadcasts.refresh_state(broadcast)
    return len(rows)


def _cancel_run(run, reason):
    changed = _cancel_unsent(run.broadcast_id, DELIVERY_CODES.get(reason, reason.upper())) if run.broadcast_id else 0
    if run.broadcast_id and not changed:
        return False  # everything was already handed off or resolved: history stays as it is
    run.state, run.reason_code = AutomationRunState.CANCELLED, reason
    run.save(update_fields=['state', 'reason_code', 'updated_at'])
    logger.info('automation_run_cancelled rule=%s run=%s reason=%s cancelled=%s', run.rule_id, run.pk, reason, changed)
    return True


def cancel_event_reminders(event, reason):
    """Cancel obsolete reminder work for ``event``. The caller holds the event
    row lock (``services``). ``source_rescheduled`` cancels runs of older
    revisions; ``source_unpublished`` cancels all of the event's runs."""
    # of=self: never lock the joined rule rows (lock order is rule -> event).
    runs = AutomationRun.objects.select_for_update(of=('self',)).filter(
        event_id=event.pk, rule__kind=AutomationKind.EVENT_REMINDER,
        state__in=[AutomationRunState.PENDING, AutomationRunState.QUEUED])
    if reason == SOURCE_RESCHEDULED:
        runs = runs.exclude(event_revision=event.schedule_revision)
    return sum(_cancel_run(run, reason) for run in runs.order_by('id'))


def reminder_run_for_broadcast(broadcast_id):
    """The reminder run behind a broadcast (None for manual/weekly mail)."""
    return (AutomationRun.objects.filter(broadcast_id=broadcast_id, event_id__isnull=False)
            .only('pk', 'rule_id', 'event_id', 'event_revision', 'state', 'reason_code', 'broadcast_id').first())


def stale_reminder_code(run, event, now):
    """Why a reminder delivery must not be handed off now, or None. The caller
    holds the event row lock (taken before the broadcast/delivery locks)."""
    if event is None or event.status != PublicationStatus.PUBLISHED:
        return SOURCE_UNPUBLISHED
    if event.schedule_revision != run.event_revision:
        return SOURCE_RESCHEDULED
    if now >= event.start_time:
        return REMINDER_GRACE_ELAPSED
    return None


def mark_run_cancelled(run_id, reason):
    AutomationRun.objects.filter(pk=run_id, state=AutomationRunState.QUEUED).update(
        state=AutomationRunState.CANCELLED, reason_code=reason, updated_at=timezone.now())


# ---------------------------------------------------------------- run presentation

def run_state(run, broadcast=None, counts=None):
    """(state, reason_code) shown for a run: stored for unmaterialized or
    cancelled runs, otherwise derived from its broadcast's aggregate state."""
    if run.state != AutomationRunState.QUEUED or run.broadcast_id is None:
        return run.state, run.reason_code or None
    broadcast = broadcast or run.broadcast
    state = broadcast.state
    if state == BroadcastState.UNCERTAIN:
        return 'uncertain', DELIVERY_UNCERTAIN
    if state == BroadcastState.COMPLETED_WITH_ERRORS:
        counts = counts or broadcasts.counts_of(broadcast)
        if not counts['sent']:
            return 'failed', PROVIDER_FAILURE
        return 'completed_with_errors', None
    return str(state), None


# ---------------------------------------------------------------- rule lifecycle

def version_conflict(current):
    return APIError('AUTOMATION_VERSION_CONFLICT',
                    'This rule changed since you loaded it. Reload it before continuing.',
                    status_code=409, fields={'version': [f'The current version is {current}.']})


def owner_inactive_error():
    return APIError('AUTOMATION_OWNER_INACTIVE',
                    'The rule owner is not an active admin. Reassign the rule before enabling it.',
                    status_code=409)


def _locked(rule_id, version):
    rule = get_object_or_404(AutomationRule.objects.select_for_update(), pk=rule_id)
    if version != rule.version:
        raise version_conflict(rule.version)
    return rule


SCHEDULE_FIELDS = ('timezone', 'weekday', 'local_time', 'offset_minutes', 'categories')


def update_rule(rule_id, version, validated):
    """Versioned PATCH. A schedule change of an enabled rule moves
    ``active_from`` so the edit never back-fills older occurrences."""
    with transaction.atomic():
        rule = _locked(rule_id, version)
        schedule_changed = any(name in validated and validated[name] != getattr(rule, name)
                               for name in SCHEDULE_FIELDS)
        for name, value in validated.items():
            setattr(rule, name, value)
        if schedule_changed and rule.enabled:
            rule.active_from = timezone.now()
        rule.version += 1
        rule.save()
    return rule


def stored_rule_errors(rule):
    """Field errors of the stored rule (enable requires a valid one)."""
    errors = {}
    if rule.timezone not in SUPPORTED_TIMEZONES:
        errors['timezone'] = [f'Only {", ".join(SUPPORTED_TIMEZONES)} is supported.']
    categories = rule.categories or []
    if any(c not in AUTOMATION_CATEGORIES for c in categories):
        errors['categories'] = ['Unknown category.']
    if rule.kind == AutomationKind.EVENT_REMINDER and not categories:
        errors['categories'] = ['Choose at least one event category.']
    subject = (rule.subject or '').strip()
    if not subject or broadcast_content.subject_error(subject):
        errors['template.subject'] = [broadcast_content.subject_error(subject) or 'Enter a subject.']
    problems = template_errors(rule.kind, rule.subject, rule.body, rule.body_html)
    if problems:
        errors['template'] = problems
    try:
        if rule.body_format == BodyFormat.RICH:
            broadcast_content.sanitize_rich_html(rule.body_html)
        elif not broadcast_content.normalize_body(rule.body or '').strip():
            errors['template.body'] = ['Enter the message template.']
    except broadcast_content.ContentError as error:
        errors['template.body_html'] = [str(error)]
    if rule.audience_type == 'department' and not rule.audience_department.strip():
        errors['audience'] = ['Enter a department.']
    return errors


def enable(rule_id, version):
    with transaction.atomic():
        rule = _locked(rule_id, version)
        if rule.enabled:
            return rule
        if owner_blocked_reason(rule):
            raise owner_inactive_error()
        errors = stored_rule_errors(rule)
        if errors:
            raise APIError('VALIDATION_ERROR', 'The saved rule is invalid. Correct it before enabling.',
                           status_code=400, fields=errors)
        rule.enabled = True
        rule.active_from = timezone.now()
        rule.version += 1
        rule.save()
    return rule


def disable(rule_id, version):
    """Stop new occurrences; cancel runs not yet materialized. Broadcasts
    already queued follow the normal delivery policy."""
    with transaction.atomic():
        rule = _locked(rule_id, version)
        if not rule.enabled:
            return rule
        rule.enabled = False
        rule.version += 1
        rule.save()
        AutomationRun.objects.filter(rule=rule, state=AutomationRunState.PENDING, broadcast__isnull=True).update(
            state=AutomationRunState.CANCELLED, reason_code=RULE_DISABLED, updated_at=timezone.now())
    return rule


# ---------------------------------------------------------------- preview

_SAMPLE_SUMMARY = 'Sample summary: no published event matches yet, so this preview uses placeholder text.'


def preview(rule_id, version, now=None):
    """Non-sending preview of the next occurrence with the currently known
    source. No run, broadcast, snapshot or delivery is written."""
    now = now or timezone.now()
    rule = get_object_or_404(AutomationRule, pk=rule_id)
    if version != rule.version:
        raise version_conflict(rule.version)
    errors = stored_rule_errors(rule)
    if errors:
        raise APIError('VALIDATION_ERROR', 'Correct the highlighted fields.', status_code=400, fields=errors)
    skip = None
    if rule.kind == AutomationKind.WEEKLY_DIGEST:
        scheduled_at = next_weekly_occurrence(rule, now)
        source = weekly_source(rule, scheduled_at)
        if not source.events:
            skip = NO_ELIGIBLE_SUMMARIES
    else:
        offset = timedelta(minutes=rule.offset_minutes)
        event = _matching_events(rule).filter(start_time__gt=now).order_by('start_time', 'id').first()
        if event is None:
            skip = NO_UPCOMING_EVENT
            sample = Event(pk=0, title='Sample event', category=(rule_categories(rule) or ['jumuah'])[0],
                           location='Venue', summary=_SAMPLE_SUMMARY, start_time=now + offset + timedelta(days=1),
                           end_time=now + offset + timedelta(days=1, hours=1))
            source = Source(scheduled_at=sample.start_time - offset, event=sample)
        else:
            source = Source(scheduled_at=event.start_time - offset, events=[event], event=event)
    count = broadcasts.audience_recipients(rule.audience_type, rule.audience_department, rule.audience_level).count()
    if not count and skip is None:
        skip = NO_RECIPIENTS
    try:
        rendered = render_preview(rule, source)
    except broadcast_content.ContentError as error:
        raise APIError('VALIDATION_ERROR', 'Correct the highlighted fields.', status_code=400,
                       fields={'template': [str(error)]}) from None
    data = source.as_dict()
    if source.event is not None and source.event.pk == 0:
        data = {**data, 'event': None, 'event_revision': None}
    data['scheduled_at'] = _iso(source.scheduled_at)
    return rule, rendered, count, skip, data


def broadcast_counts(broadcast_ids):
    """{broadcast_id: (broadcast, counts)} in one query."""
    rows = broadcasts.with_counts(Broadcast.objects.filter(pk__in=broadcast_ids))
    return {row.pk: (row, broadcasts.counts_of(row)) for row in rows}


def is_owner_candidate(user):
    return user is not None and user.is_active and getattr(user, 'role', None) == 'admin'
