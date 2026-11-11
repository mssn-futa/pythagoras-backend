# Communication automations (D4, COMM-06)

Weekly digests and event reminders reuse the broadcast outbox: an occurrence
becomes one ordinary queued `Broadcast` (frozen content and recipient
snapshot, one `Delivery` per recipient) that the `dawah_outbox` worker sends
through the shared transport. There is no separate sender, scheduler,
template or summary generator. Code: `dawah/automations.py` (scheduling,
content, cancellation, lifecycle), `dawah/automation_serializers.py`,
`dawah/automation_views.py`; models `AutomationRule`, `AutomationRun`,
`Event.schedule_revision` (migration `0010`).

## API (admin only: active `role=admin`; students 403)

All responses use the D0 envelope; lists are D0-paginated (`page`,
`page_size` ≤ 100).

| Route | Behavior |
|---|---|
| `GET /dawah/automations/` | Rules, newest first |
| `POST /dawah/automations/` | Create a **disabled** rule owned by the caller (201) |
| `GET /dawah/automations/{id}/` | One rule |
| `PATCH /dawah/automations/{id}/` | Versioned edit, or `{owner_id, version}` reassignment |
| `POST /dawah/automations/{id}/enable/` | `{version}` → rule |
| `POST /dawah/automations/{id}/disable/` | `{version}` → rule |
| `POST /dawah/automations/{id}/preview/` | `{version}` → non-sending preview |
| `GET /dawah/automations/{id}/runs/` | Occurrence history, newest `scheduled_at` first |

**Rule payload**: `id, kind (weekly_digest|event_reminder), enabled, audience
{type, department?, level?}, categories[], timezone, weekday (0=Monday; null
for reminders), local_time ("HH:MM"; null for reminders), offset_minutes (null
for weekly), template {subject, body_format, body, body_html}, owner_id,
owner_active, blocked_reason (null | "owner_inactive"), version, created_at,
updated_at`. `template.body` is the plain template or the generated text
fallback of a rich one; `body_html` is null for plain.

**Create** weekly: `{kind, audience, timezone, weekday, local_time, template,
enabled:false[, categories]}`; reminder: `{kind, audience, offset_minutes,
categories, template, enabled:false}`. `enabled` may be omitted; `true` is a
400. Defaults (D4): weekly Monday 08:00 `Africa/Lagos`, reminder 1440 minutes;
a missing `template` gets the default template for the kind (so the literal
D4 request examples work). Keys of the other kind are 400 (`Not allowed for
this rule type.`).

**PATCH** takes the fields of the rule's kind plus `version`; `kind` and
`enabled` are 400. `owner_id` must be sent alone with `version` and name an
active admin. Saving never enables. A schedule change (timezone, weekday,
local_time, offset, categories) of an enabled rule sets `active_from = now`,
so it never back-fills older occurrences.

**Validation** (400 `VALIDATION_ERROR`, flat `fields`): `kind`, `enabled`,
`audience`, `categories` (tokens `dawah_night`, `usroh`, `special_lecture`,
`jumuah`, case-insensitive on input, stored lower-case; reminders need ≥ 1;
weekly empty = all four), `timezone` (only `Africa/Lagos`), `weekday` (0–6),
`local_time` (`HH:MM`), `offset_minutes` (1–43200), `template` (unknown
placeholders, `{% %}`/`{# #}` code), `template.subject` (one line ≤ 200),
`template.body` / `template.body_html` (same limits and sanitizer as broadcast
drafts; rich templates never take `body`), `template.body_format`, `version`,
`owner_id`.

**Errors**: 409 `AUTOMATION_VERSION_CONFLICT` (stale version on PATCH /
enable / disable / preview; `fields.version` names the current version); 409
`AUTOMATION_OWNER_INACTIVE` (enable with an inactive or non-admin owner);
enable of an invalid stored rule is 400 `VALIDATION_ERROR`. Enabling an
enabled rule (or disabling a disabled one) returns it unchanged.

### Placeholders

`{{name}}` only (whitespace inside the braces allowed); anything else is
rejected on save and enable.

| Kind | Placeholders |
|---|---|
| weekly_digest | `week_start`, `week_end`, `summary_count`, `summaries` |
| event_reminder | `event_title`, `event_start`, `event_location`, `event_summary`, `event_url` |

Times are formatted in the rule time zone, e.g. `Monday 9 November 2026,
08:00 (Africa/Lagos)`. `summaries` lists each event (title, start, authored
summary, visible resource links: promoted audio/document URLs and YouTube
watch URLs); in rich templates it is generated HTML (`h3`, `p`, `ul/li/a`),
and every value is HTML-escaped. `event_url` is
`{FRONTEND_BASE_URL}/dawah/events/{id}` (setting/env `FRONTEND_BASE_URL`;
empty when unset). The subject is substituted as text, collapsed to one line
and truncated to 200 characters. The substituted body goes through
`sanitize_rich_html` (rich) or the plain path, then `broadcasts.queue`, which
renders with `render_content` and freezes `snapshot_*`.

### Preview

`{subject (rendered), body, body_format, body_html (template), rendered_text,
rendered_html, audience_count, skip_reason_code, source}`. Weekly previews the
**next** occurrence with the events currently in its window; reminders preview
the next matching published event. `source = {window_start, window_end,
scheduled_at, events[{id, title, category, start_time}], event?,
event_revision?}`. `skip_reason_code`: `no_eligible_summaries`,
`no_recipients`, `content_invalid` (the composed message fails the same
`broadcasts.checked_render` checks queue applies: subject, body limits, rich
sanitization, 50,000-character text and 400,000-character email limits;
`rendered_html`/`rendered_text` are then empty), or `no_upcoming_event`
(reminder without a matching event: the preview then uses sample values and
`source.event` is null). Nothing is written.

### Run payload

`{id, occurrence_key, scheduled_at, state, reason_code, broadcast_id,
recipient_count, counts{queued, sent, failed, uncertain, skipped, cancelled},
source{window_start, window_end, events[], event?, event_revision?},
created_at, updated_at}`. `counts`/`recipient_count`/`broadcast_id` are null
until materialized.

States: stored `skipped`, `missed`, `blocked`, `cancelled`, `failed`
(content), `pending` (transient, never committed by the current tick);
materialized runs show the broadcast's state: `queued`, `processing`,
`completed`, `completed_with_errors`, `failed` (every delivery failed, reason
`provider_failure`), `uncertain` (reason `delivery_uncertain`). An
uncertain broadcast always shows `uncertain`, even when the stored run state
is `cancelled`. A run is stored `cancelled` only when at least one of its
deliveries actually became `cancelled`; the cancel reason is always kept in
`source.cancel_reason`. Weekly sources carry `events_omitted` when the digest
cap left events out.

Reason codes: `no_eligible_summaries`, `no_recipients`,
`missed_catch_up_window`, `reminder_grace_elapsed`, `source_unpublished`,
`source_rescheduled`, `rule_disabled`, `owner_inactive`, `content_invalid`,
`provider_failure`, `delivery_uncertain`, `superseded_after_handoff`,
`window_overlap`, `materialize_error`.

Example (weekly, materialized):

```json
{"id": 1, "occurrence_key": "rule-3:2026-11-09T07:00:00Z", "scheduled_at": "2026-11-09T07:00:00Z",
 "state": "queued", "reason_code": null, "broadcast_id": 7, "recipient_count": 4,
 "counts": {"queued": 4, "sent": 0, "failed": 0, "uncertain": 0, "skipped": 0, "cancelled": 0},
 "source": {"window_start": "2026-11-02T07:00:00Z", "window_end": "2026-11-09T07:00:00Z",
            "events": [{"id": 5, "title": "Friday khutbah", "category": "jumuah", "start_time": "2026-11-06T12:00:00Z"}]}}
```

Reminder example: `occurrence_key "rule-4:event-5:rev-2"`, `source.event
{id, title, category, start_time}`, `source.event_revision 2`, state
`cancelled`, reason `source_rescheduled`.

## Recurrence, catch-up and cancellation

The outbox scheduler tick (`outbox.scheduler_tick` → `automations.tick(now)`)
runs at the start of every `dawah_outbox --once` pass and every continuous
poll, and again between claim batches (no claims held) whenever the last tick
is at least 60 s old (`outbox.TICK_INTERVAL_SECONDS`), so a long drain does
not delay occurrences. Each enabled rule is evaluated in its own transaction holding
its row lock (`SELECT ... FOR UPDATE SKIP LOCKED`: a concurrent pass skips it).
`AutomationRun` is unique per `(rule, occurrence_key)`; a repeated pass,
restart or race finds the existing run and creates nothing. The run, the
broadcast, its snapshot and deliveries are committed together (savepoint per
occurrence). An unexpected materialization error rolls that occurrence back
for a retry on the next pass; after 3 consecutive failures of the same
occurrence it is recorded `failed` (`materialize_error`) and never retried.
A failing rule is logged (ids and exception class only) and does not stop
other rules or the drain.

- **Window/time**: computed from the scheduled occurrence, never from worker
  execution time. Only occurrences scheduled at/after `active_from` (last
  enable or schedule change) are considered, so enabling never replays the
  past and existing manual broadcasts are untouched.
- **Weekly**: occurrence at `weekday`/`local_time` in `timezone`; window
  `[occurrence - 7 days, occurrence)`. Selects published events whose
  `Lower(Trim(category))` is one of the rule categories (empty = all four),
  starting in the window, with a non-blank authored summary. No eligible
  summaries or no active recipients → `skipped` (no empty mail). Only the
  latest due occurrence is sent, and only within the catch-up window (default
  24 h, `AUTOMATION_WEEKLY_CATCH_UP_MINUTES`); older unprocessed occurrences
  within 28 days are recorded `missed` (`missed_catch_up_window`). An
  occurrence whose window overlaps the window of an already materialized run
  of the same rule (e.g. after a weekday/time edit) is `skipped`
  (`window_overlap`), so no event is digested twice.
- **Digest size caps**: at most 20 events per digest (earliest first; the
  rest are counted in `source.events_omitted` and mentioned as "…and N more
  events"), summaries clipped to 1,500 characters, at most 10 resource links
  per event. A typical full digest stays far below the 50,000-character text
  and 400,000-character email limits; anything over them is `content_invalid`.
- **Reminder**: occurrence at `start_time - offset_minutes` for each
  published matching event; key `rule-{id}:event-{event}:rev-{revision}`. Sent
  only within the grace (default 15 min, `AUTOMATION_REMINDER_GRACE_MINUTES`)
  and before the event starts; otherwise `missed` (`reminder_grace_elapsed`).
  Horizon 7 days; at most 200 events per rule and pass. If any earlier run of
  the same rule for the same event (another revision) has a delivery that was
  or may have been handed off (`sending`, `sent`, `uncertain`, or `failed`
  without known nonacceptance), the new revision is `skipped`
  (`superseded_after_handoff`): a correction is a separately authorized
  message. The revision increments on any start **or** end change (D4).
- **Owner**: an inactive or non-admin owner (or a deleted one) records the due
  occurrence as `blocked` (`owner_inactive`); nothing is queued until the rule
  is reassigned (later occurrences only; blocked ones are not replayed).
- **Reschedule/unpublish**: `services.update_event` (and the Django admin
  event form, from the locked row) increments
  `Event.schedule_revision` when `start_time`/`end_time` change and, under the
  event lock, cancels unsent deliveries (`queued`/`claimed`) of reminder runs
  for older revisions; `unpublish_event` does the same for all of the event's
  reminder runs. The run becomes `cancelled` (`source_rescheduled` /
  `source_unpublished`) unless everything was already handed off (then its
  history stays). The next pass computes the new revision's occurrence (sent
  if still within grace, else `missed`).
- **Handoff recheck**: the worker locks the event before the broadcast and
  delivery of an automated reminder and refuses the handoff when the event is
  unpublished/deleted, its revision changed or it has started (delivery
  `cancelled` with `SOURCE_UNPUBLISHED`/`SOURCE_RESCHEDULED`/`EVENT_STARTED`,
  attempt row `skipped`). This also covers edits made outside the API.
- **Never rewritten**: `sending`, `sent`, `uncertain` deliveries. A queued
  delivery with an earlier ambiguous same-key attempt becomes `uncertain`
  (not `cancelled`): it may already have been accepted. Mail handed to the
  provider cannot be recalled; a correction is a separate broadcast.
- **Disable**: no new occurrences; already queued broadcasts follow the normal
  delivery policy (cancel stale reminders via the event, as above).

Lock order: rule → event → broadcast → deliveries (ascending id). No lock is
held during provider I/O.

## Operations

- Production liveness is the Cloudflare Cron-started `python manage.py
  dawah_outbox --once` (RELEASE-03); locally `python manage.py dawah_outbox`.
  Rules, occurrences and deduplication live only in PostgreSQL. A pass that
  does not run within the catch-up/grace window records `missed`, it never
  sends late.
- Environment: `FRONTEND_BASE_URL` (reminder links, optional; with
  `DEBUG=false` it must be `https` without user info, system check
  `dawah.E001`),
  `AUTOMATION_WEEKLY_CATCH_UP_MINUTES` / `AUTOMATION_REMINDER_GRACE_MINUTES`
  (Django settings overrides, optional); delivery itself uses the outbox
  settings (`docs/communication-outbox.md`).
- Logs: `automation_run_queued|closed|cancelled rule= run= ...` and
  `automation_tick_failed rule=`; ids, states and codes only.

## Limitations

- After provider handoff a cancelled reminder may still be delivered; the
  delivery stays `sent`/`uncertain` with its evidence.
- An event unpublished and republished at the same revision does not get a
  new reminder (its run stays `cancelled`).
- Event time changes made outside `services.update_event` and the Django
  admin (shell, raw SQL) do not bump `schedule_revision`; the handoff recheck
  still blocks sending after the event starts or once unpublished.
- Weekly digests are not cancelled when an included event is later
  unpublished (the frozen snapshot is sent as materialized).
- Only `Africa/Lagos` is accepted (frontend contract); the code is generic
  over IANA zones.
