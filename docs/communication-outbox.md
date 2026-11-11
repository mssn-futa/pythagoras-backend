# Communication outbox (D4)

COMM-01 owns broadcast drafts, audiences, the queue transaction and the frozen
recipient snapshot. COMM-02 (worker, Resend, history, retries) builds on the
schema and rules below without changing them. Code: `dawah/models.py`
(Broadcast, Delivery, DeliveryAttempt, DeliveryGeneration), `dawah/broadcasts.py`.

## Queue snapshot

`POST /dawah/broadcasts/{id}/queue/` locks the draft, checks its version and
content, resolves active `role=student` accounts (department: `Lower(Trim())`
on both sides; level: exact), and in one transaction creates one `Delivery`
per recipient (unique per broadcast/student) plus the frozen broadcast fields
`snapshot_subject`, `snapshot_text`, `snapshot_html`,
`snapshot_template_version`, `recipient_count`, `queued_at`, `queued_by`.
Deliveries start `queued`, `retry_generation=0`, `attempt_count=0`,
`next_attempt_at=queued_at`, with empty provider/idempotency fields.
The worker sends `snapshot_*` content to `snapshot_email`; it never re-reads
the draft fields or the user's current address.

Lock order for every write: broadcast row, then its deliveries in ascending id.
After changing delivery outcomes, call `broadcasts.refresh_state(broadcast)`
while holding the broadcast lock; never hold locks during provider I/O.

## Retry eligibility (frozen contract)

Manual retry requires **positive evidence that the provider did not accept**
the current generation. A delivery is retry-eligible only when all hold:

- `state == "failed"`
- `nonacceptance_known is True`
- `provider_email_id == ""`
- `sent_at is None`

`nonacceptance_known` defaults to `False` and is set **only by the worker**,
together with `state="failed"`, in exactly these cases:

- a definitive provider rejection of this request (e.g. Resend 4xx for an
  invalid recipient, unverified sender/domain, invalid content) when no
  earlier handoff of this generation was ambiguous;
- a failure that happened before any provider handoff for this generation
  (no request was sent, e.g. invalid snapshot address, payload construction
  error);
- a generation whose **every** handoff received an explicit provider
  non-acceptance (429 rate limit or quota) when its safety window ends.

Never set it for outcomes where acceptance cannot be ruled out. These are
stored as `uncertain` (or `failed` with `nonacceptance_known=False`) and are
**never** retry-eligible:

- idempotency payload conflict (`IDEMPOTENCY_PAYLOAD_CONFLICT`),
- concurrent idempotent request (409 `CONCURRENT_IDEMPOTENT_REQUESTS`: an
  earlier request with this key is still in flight and may be accepted); it is
  recorded as an ambiguous (`uncertain`) attempt and never contributes to
  `nonacceptance_known`,
- safety deadline expired (`idempotency_deadline_at` passed) while unresolved,
- timeout / connection error / 5xx / malformed success without an email ID,
- any lease loss after `sending` was persisted.

The API mirrors the rule: `Delivery.eligible_actions` is `["retry"]` and the
broadcast includes `"retry"` only for eligible rows, and
`POST .../retry/` rejects the whole request with 409 `DELIVERY_NOT_RETRYABLE`
if any listed delivery is ineligible (400 for IDs of another broadcast).

## Generations and evidence

`retry_generation` 0 is the original send; each accepted manual retry adds 1.
Before `retry()` resets a delivery's live fields it writes an append-only
`DeliveryGeneration` row for the closing generation: `generation`, final
`state`, `attempt_count`, `error_code`, `nonacceptance_known`,
`provider_email_id`, `idempotency_key`, `request_snapshot`, `request_hash`,
`first_dispatch_at`, `idempotency_deadline_at`, `requested_at`/`requested_by`
(who started that generation; null for the original) and
`closed_at`/`closed_by` (the admin whose retry closed it). The live row then
returns to `queued` with a fresh attempt budget, `nonacceptance_known=False`
and empty request identity; the new generation gets a new key, e.g.
`idempotency_key('broadcast', delivery_id, 'generation', retry_generation)`.

Per-attempt audit within a generation goes to `DeliveryAttempt`
(unique per delivery/generation/attempt; outcome and safe error code only,
never raw provider errors, credentials, bodies or addresses).

## Counts and state

`counts.queued` covers every unresolved delivery (queued/claimed/sending).
`broadcasts.derive_state`: uncertain wins, then outstanding work
(`processing` once anything was dispatched, else `queued`), then
`completed_with_errors` when any failed, else `completed`.

## Worker (COMM-02)

Code: `dawah/outbox.py` (worker, recovery, backlog), command
`dawah/management/commands/dawah_outbox.py`, tests `dawah/tests/test_outbox.py`.

```sh
python manage.py dawah_outbox --once                      # production: one bounded pass, then exit
python manage.py dawah_outbox --once --budget-seconds 240 --max-sends 100
python manage.py dawah_outbox                             # continuous local/operator loop (poll 5 s)
python manage.py dawah_outbox --status                    # backlog JSON; sends nothing
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--once` | off | Scheduler tick, lease recovery, bounded drain, release unused claims, exit 0. |
| `--status` | off | Print backlog JSON (below) and exit. Mutually exclusive with `--once`. |
| `--budget-seconds` | 240 | `--once` wall budget. No new handoff starts once the remaining budget is below `RESEND_TIMEOUT_SECONDS` + 10 s margin. |
| `--max-sends` | none | Cap on real provider handoffs in this run (skips and pre-handoff failures do not count). |
| `--batch-size` | 20 | Rows claimed per batch. |
| `--poll-seconds` | 5 | Idle poll interval in continuous mode. |

Each run prints one JSON summary on stdout
(`{"mode","claimed","released","recovered","materialized","handoffs","outcomes","stop_reason","elapsed_seconds"}`,
`stop_reason` one of `drained`, `budget`, `max_sends`, `signal`). SIGTERM/SIGINT
stop new claims, let the in-flight send finish (bounded by
`RESEND_TIMEOUT_SECONDS`), release unused claims and exit 0. An unexpected error
releases the batch's unused claims and exits non-zero; an in-flight `sending`
row is then recovered by the next run as below.

The Cloudflare job role is `outbox-once` (fixed argv
`python manage.py dawah_outbox --once`, see the RELEASE-09 entrypoint); it
needs the web role's mail settings (`RESEND_API_KEY`, `DEFAULT_FROM_EMAIL`,
`EMAIL_REPLY_TO`, `EMAIL_IDEMPOTENCY_NAMESPACE`, `RESEND_TIMEOUT_SECONDS`,
`OUTBOX_REQUESTS_PER_SECOND`) plus
`SECRET_KEY`/`DATABASE_URL`. Run exactly one worker per environment (the
singleton job). Correctness does not depend on it: claims, leases and tokens
make concurrent workers safe, but rate pacing is per process.

### Per delivery

1. **Claim** (one transaction): up to 20 due `queued` rows
   (`next_attempt_at <= now`) with `FOR UPDATE SKIP LOCKED`, each gets a new
   `lease_token` and `lease_expires_at = now + 120 s`, state `claimed`. Commit;
   no lock is held afterwards. Unused claims of a batch have their leases
   renewed after each send.
2. **Prepare** (lock broadcast, then delivery): the token must match and the
   lease must not have expired (an expired claim is requeued, never sent). The
   recipient's `is_active` is rechecked: inactive becomes `skipped`
   (`RECIPIENT_INACTIVE`), or `uncertain` if an earlier same-key attempt of this
   generation may have been accepted. On the generation's first dispatch the
   request is frozen: `request_snapshot` (`from`, one-element `to` =
   `snapshot_email`, `subject`/`html`/`text` from the broadcast snapshot,
   `reply_to`), `idempotency_key = idempotency_key('broadcast', id, 'generation',
   n)`, `request_hash` (SHA-256 of key + canonical snapshot), `first_dispatch_at`
   and `idempotency_deadline_at = first_dispatch_at + 23 h`. Later attempts
   rebuild the message only from the stored snapshot after verifying its hash
   (mismatch: `uncertain REQUEST_SNAPSHOT_MISMATCH`, nothing sent). A message that
   cannot be formed (e.g. blank `DEFAULT_FROM_EMAIL`) is `failed INVALID_MESSAGE`
   with `nonacceptance_known` (no handoff happened). Otherwise `attempt_count+1`,
   state `sending`, lease renewed, an open `DeliveryAttempt` row; commit.
3. **Send** through `pythagoras.mail.get_transport(timeout=RESEND_TIMEOUT_SECONDS)`
   with no database lock held. A raising transport counts as
   `uncertain TRANSPORT_EXCEPTION`.
4. **Complete** (lock broadcast, then delivery): only if state is `sending` and
   token/generation/attempt still match; otherwise the result is logged as
   `outbox_stale_completion` (with status/code/provider ID) and nothing changes.
   The attempt row is closed and `broadcasts.refresh_state` runs under the lock.

| Transport result | Delivery outcome |
| --- | --- |
| `accepted` with ID | `sent`, `provider_email_id`, `sent_at` (provider acceptance, not inbox proof) |
| `accepted` without ID | treated as `uncertain MALFORMED_SUCCESS` |
| `failed` payload conflict (`INVALID_IDEMPOTENT_REQUEST`, memory `IDEMPOTENCY_PAYLOAD_MISMATCH`) | `failed IDEMPOTENCY_PAYLOAD_CONFLICT`, `nonacceptance_known=False`: investigate, never rotate the key |
| `failed` other, no ambiguous prior attempt | `failed <code>`, `nonacceptance_known=True` (retry-eligible) |
| `failed` other after an ambiguous prior attempt | `uncertain <code>` |
| `retryable` 429 rate limit / quota (`RATE_LIMIT_EXCEEDED`, `DAILY_QUOTA_EXCEEDED`, ...) | attempt `retryable`; the **whole process** pauses for Retry-After (default 60 s, clamped to the safety window), the current batch ends and its unused claims are released; the row is requeued with the same key at now + pause. Does **not** spend the attempt budget; only the safety window bounds it. If the pause no longer fits the window: `failed` + `nonacceptance_known` when every handoff was a 429/quota, else `uncertain` |
| `retryable` 409 `CONCURRENT_IDEMPOTENT_REQUESTS` | attempt `uncertain` (ambiguous); same key after `max(budget delay, Retry-After)`; budget/window spent: `uncertain` |
| `uncertain` (timeout, connection, 5xx, 408, other 409, malformed) | `queued`, same key, after the budget delay; when budget/window is spent: `uncertain` |

Budget: 3 budget-counted handoffs per generation (every handoff except
429/quota rejections); delays 60 s after the first, 300 s after the second. A
replay is only scheduled if it (plus one bounded send) still ends before
`idempotency_deadline_at`; a Retry-After beyond the window ends the
generation instead of overflowing. "Ambiguous prior attempt" means an attempt
row of this generation that is not `retryable` (429/quota) or `skipped`
(uncertain, concurrent-key, interrupted, lease lost, open).

**Late results.** A worker that finishes after losing its lease never changes
the delivery's state; it stores the late result on its attempt row
(`late_outcome`, `late_error_code`, `late_recorded_at`, `provider_email_id`)
and logs `outbox_stale_completion`. If that late result is an acceptance with an
email ID and the delivery is still in the same generation and `uncertain`,
the delivery is resolved to `sent` with that ID (`outbox_late_acceptance`):
the provider accepted this exact request/key.

**Startup limits.** `RESEND_TIMEOUT_SECONDS` must be below 110 (120 s lease
minus 10 s margin) so a live send can never outlast its lease, and
`OUTBOX_REQUESTS_PER_SECOND` must be between 1/60 and 100; settings refuse to
load otherwise. Pending claims' leases are also renewed after every pacing
wait, and 429 pauses are waited out before claiming, never while holding
claims.

### Recovery

At the start of every pass (and every continuous poll):

- `claimed` with an expired lease: back to `queued`, nothing was sent.
- `sending` with an expired lease (crash/kill after `sending` was persisted):
  the open attempt is closed `uncertain INTERRUPTED`; if budget remains and the
  safety window still covers a bounded send, the row is requeued for an
  immediate **same-request, same-key** replay (the provider deduplicates an
  accepted original); otherwise it becomes `uncertain` (`INTERRUPTED`, or
  `IDEMPOTENCY_WINDOW_EXPIRED` past the window).

At prepare time a stored request whose window no longer covers a bounded send
is never replayed: `uncertain IDEMPOTENCY_WINDOW_EXPIRED` after an ambiguous
attempt, otherwise (only definite non-acceptances so far) `failed
IDEMPOTENCY_WINDOW_EXPIRED` with `nonacceptance_known`.

### Rate budget

`OUTBOX_REQUESTS_PER_SECOND` (default 1, from 1/60 to 100) paces the
outbox process: one handoff slot per interval, no bursts, shared by every
worker thread in the process; a 429/quota response defers the next slot for
the whole process. Set it below the Resend account rate; the
difference is headroom for welcome/transactional mail, which sends inline and
is not paced. There is deliberately no account-wide limiter setting (welcome
and outbox share no counter); anything that still hits the account limit gets
429 and backs off with Retry-After as above. Pacing waits count against the
`--once` budget. Tune it against the Resend account's actual rate/quota.

### Backlog and logs

`--status` prints `counts` (per delivery state), `due`, `expired_leases`,
`oldest_outstanding_age_seconds` (oldest unresolved delivery),
`oldest_due_age_seconds` (oldest due-but-unsent row), `broadcasts_processing`,
`broadcasts_uncertain`. Logger `dawah.outbox` (stderr when nothing else is
configured) emits `outbox_sending`, `outbox_result`, `outbox_lease_recovered`,
`outbox_claim_expired`, `outbox_claims_released`, `outbox_stale_completion` and
`outbox_run` with ids, generation, attempt, state, safe code and provider email
ID only; never addresses, subjects, bodies, request snapshots or credentials.

### Delivery detail API

`GET /dawah/broadcasts/{id}/deliveries/{delivery_id}/` (active admin only; 404
for a delivery of another broadcast) returns the delivery-list fields plus
`broadcast_id`, `nonacceptance_known`, `next_attempt_at`, `idempotency_key`,
`request_hash`, `first_dispatch_at`, `idempotency_deadline_at`,
`retry_requested_at`/`_by_id`, `generations` (closed generations:
state, attempt count, safe code, nonacceptance, provider ID, key, hash,
dispatch/deadline, requested/closed times and admin ids) and `attempts`
(generation, attempt, started/finished, outcome, safe code, provider ID, late
outcome/code/time). It never contains the request snapshot, subject or body.

### Operator procedures

- **Failed (retry-eligible):** fix the cause (sender/domain/key/content), then
  `POST /dawah/broadcasts/{id}/retry/` with the delivery ids; the next pass
  sends generation n+1 with a new key.
- **Uncertain / payload conflict:** never retried automatically or by the API.
  Reconcile with the stored `provider_email_id`, `idempotency_key`,
  `request_hash`, attempt rows (incl. late results; see the delivery detail API)
  and the Resend dashboard; resending is a separate,
  explicitly authorized broadcast.
- **Schema changes to Delivery state:** stop the scheduled job (or let the
  current run finish), migrate, deploy, re-enable; leases make a killed run safe.
- `scheduler_tick(now)` in `dawah/outbox.py` is the COMM-06 hook (automation
  occurrence materialization); it is a no-op returning 0 until then.
