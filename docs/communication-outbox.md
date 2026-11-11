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
together with `state="failed"`, for:

- a definitive provider rejection of this request (e.g. Resend 4xx for an
  invalid recipient, unverified sender/domain, invalid content), or
- a failure that happened before any provider handoff for this generation
  (no request was sent, e.g. invalid snapshot address, payload construction
  error).

Never set it for outcomes where acceptance cannot be ruled out. These are
stored as `uncertain` (or `failed` with `nonacceptance_known=False`) and are
**never** retry-eligible:

- idempotency payload conflict (`IDEMPOTENCY_PAYLOAD_CONFLICT`),
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
