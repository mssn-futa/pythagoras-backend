# Identity, authentication and sessions (PLAT-03)

Authority: D0 (roles, session policy, envelope), D1 (identity), D4 "Resend
transport contract" and D5 (configuration, throttling) in the Project Pythagoras
docs repository, revision `df67388`. All responses use the single D0 envelope
described in [shared-runtime.md](shared-runtime.md).

## Endpoints

| Route | Method | Auth | Success | Throttle (per IP) |
| --- | --- | --- | --- | --- |
| `/accounts/register/` | POST | none | 201 user | 5/min |
| `/accounts/login/` | POST | none | 200 user + tokens | 10/min |
| `/accounts/refresh/` | POST | none (body token) | 200 access + rotated refresh | 10/min |
| `/accounts/logout/` | POST | none (body token) | empty 204, also on replay | - |
| `/accounts/me/` | GET, PATCH | bearer, active account | 200 user | - |

Register, login, refresh and logout ignore any `Authorization` header, so a
stale access token never blocks sign-in. Access tokens last 60 minutes; refresh
tokens 7 days, single use (rotation with blacklist).

User shape (register, login, me):

```json
{"id":1,"email":"student@example.test","first_name":"Amina","last_name":"Yusuf","matric_number":"CSC/21/001","department":"Computer Science","level":"300","role":"student","is_active":true}
```

### Register

Request fields: `email`, `password`, `password_confirm`, `first_name`,
`last_name` (required); `matric_number`, `department`, `level` (optional;
`level` is one of `100`-`500` or blank). Any other key, including `role`,
`is_active`, `is_staff`, `is_superuser`, `groups`, is rejected with 400
`VALIDATION_ERROR` naming the field. The account is always an active student.

```json
{"success":true,"data":{"id":1,"email":"student@example.test", "...":"user shape"},"message":"Account created."}
```

A welcome email is attempted after the transaction commits. The response never
claims delivery and a mail failure never removes the account.

### Login

```json
{"email_or_matric":"CSC/21/001","password":"<user password>"}
```

An identifier containing `@` is matched as an email, otherwise as a matric
number; both are normalized first. Success:

```json
{"success":true,"data":{"user":{"id":1,"email":"student@example.test","first_name":"Amina","last_name":"Yusuf","matric_number":"CSC/21/001","department":"Computer Science","level":"300","role":"student","is_active":true},"tokens":{"access":"<token>","refresh":"<token>"}},"message":"Signed in."}
```

Unknown identifier and wrong password return the identical generic error:

```json
{"success":false,"data":{"code":"INVALID_CREDENTIALS"},"message":"Invalid email/matric number or password."}
```

(status 401). A deactivated account with the correct password returns 403
`ACCOUNT_INACTIVE`; with a wrong password it gets the generic 401.

### Refresh

Request `{"refresh":"<token>"}`; success `data` is
`{"access":"<token>","refresh":"<rotated token>"}` with message
`Session refreshed.`. The presented token is blacklisted in the same
transaction under a row lock, so reuse (also concurrent reuse) returns 401
`TOKEN_INVALID`. An inactive account returns 403 `ACCOUNT_INACTIVE`.

### Logout

Request `{"refresh":"<token>"}`. Blacklists that refresh token and returns an
empty 204. Replaying a token this server issued that is already revoked,
rotated or expired also returns 204 and reveals nothing. Unrecognized/forged
input returns 401 `TOKEN_INVALID`. Access tokens remain valid until expiry (D0),
except that inactive accounts are always rejected.

### Current user

`GET /accounts/me/` returns the user shape. `PATCH /accounts/me/` accepts only
`first_name`, `last_name` (non-blank), `department` and `level`:

```json
{"first_name":"Amina","department":"Computer Science","level":"400"}
```

Any other key (`email`, `matric_number`, `role`, `is_active`, `password`, ...)
returns 400 with `{"field":["This field cannot be set or changed here."]}` and
nothing is applied. Message on success: `Profile updated.`.

### Inactive accounts

`accounts.authentication.ActiveUserJWTAuthentication` (the project default)
returns 403 `ACCOUNT_INACTIVE` for a valid access token of a deactivated
account on every endpoint, including domain apps.

## Student administration (PLAT-05)

Admin only (`IsActiveAdmin`); anonymous 401, student 403 `PERMISSION_DENIED`,
deactivated admin 403 `ACCOUNT_INACTIVE`. No throttle (D5 lists none for these).

| Route | Method | Success |
| --- | --- | --- |
| `/accounts/students/` | GET | 200 paginated student-role users |
| `/accounts/students/{id}/` | GET | 200 student; unknown or administrator id 404 |
| `/accounts/students/{id}/` | PATCH | 200 `Student updated.` |

List filters (AND): `search` (every term matches full name, email or matric,
case-insensitive), `department` (trimmed, case-insensitive exact), `level`
(`100`-`500`), `is_active` (`true`/`false`); empty values are ignored, invalid
ones are 400 field errors. `page`/`page_size` (default 20, max 100); ordered by
last name, first name, id. Rows are the user shape plus `date_joined`:

```json
{"success":true,"data":{"count":1,"next":null,"previous":null,"results":[{"id":1,"email":"student@example.test","first_name":"Amina","last_name":"Yusuf","matric_number":"CSC/21/001","department":"Computer Science","level":"300","role":"student","is_active":true,"date_joined":"2026-09-14T08:30:00Z"}]},"message":"Request successful."}
```

PATCH accepts `first_name`, `last_name`, `department`, `level`, `email`,
`matric_number`, `is_active`, normalized exactly as registration; collisions are
400 field errors. Any other key (`role`, `is_staff`, `groups`, `password`, ...)
is 400 `{"role":["This field cannot be set or changed here."]}` and nothing is
applied. Deactivation applies on the student's next request (existing access
tokens get 403, refresh fails 403).

`accounts.student_admin.set_account_active()` / `lock_status_scope()` /
`assert_status_change_allowed()` are the shared status protection: the target
and all active admin rows are locked (`select_for_update`, pk order) and a
change that would deactivate the acting admin (409 `SELF_DEACTIVATION`) or the
last active admin (409 `LAST_ADMIN`) is refused. Use them for any future
operation that can change an admin's status.

## Shared permissions for domain apps

```python
from accounts.permissions import IsActiveAdmin, IsActiveStudent, IsActiveUser
# same classes as: IsAdmin, IsStudent (also IsAdminOrReadOnly, IsOwnerOrAdmin)
```

All require an authenticated, active account. Admin means `role == "admin"`;
`is_staff` is only Django-admin access. `pythagoras.permissions` holds the
definitions.

## Identity normalization and constraints

- Email: trimmed and fully lowercased; unique on `Lower(email)`.
- Matric number: trimmed, uppercased, blank becomes NULL; unique on
  `Upper(matric_number)`; a check constraint forbids `''`.
- Department: trimmed display string.

Normalization happens in `CustomUser.save()` (all write paths), in API
serializers and in the Django admin forms. Ordinary duplicates are field
errors; a concurrent duplicate that loses the race also becomes a 400 field
error, never a raw IntegrityError.

### Migrations and preflight

| Migration | Effect |
| --- | --- |
| `accounts.0003_normalize_identities` | Aborts (changing nothing) if normalized email or matric values collide (two or more blank/whitespace-only emails count as a collision), listing only user IDs; otherwise normalizes stored email/matric/department in place. Never merges, deletes or re-roles users. |
| `accounts.0004_customuser_identity_constraints` | Adds the two case-insensitive unique constraints and the non-blank matric check. |
| `token_blacklist.0001`-`0013` (library) | SimpleJWT outstanding/blacklisted token tables. |

Procedure for an existing database (after a backup):

```sh
python manage.py identity_collision_report   # exit 0: "No identity collisions found."
python manage.py migrate --noinput
```

When the report exits 1 it prints lines such as `email: user IDs 4, 9`. Resolve
them manually with lead approval (e.g. correct one account's email/matric in
the Django admin); do not delete or merge accounts automatically. Rerun the
report until it passes, then migrate.

## Administrator provisioning

- `python manage.py createsuperuser` creates `role=admin`, `is_staff` and
  `is_superuser` (D0 technical superuser).
- Existing accounts are never promoted implicitly (including existing staff).
  Promote explicitly and idempotently:

```sh
python manage.py provision_admin --email lead@example.test   # role=admin only
python manage.py provision_admin --user-id 4 --staff         # also Django admin access
python manage.py provision_admin --existing-superusers       # reconcile legacy superusers
```

The command refuses unknown or inactive accounts and reports `already
provisioned; no change` on repeat. The Django user admin lists and filters by
role.

## Email transport (`pythagoras.mail`)

```python
from pythagoras.mail import (OutboundEmail, SendResult, SendStatus, get_transport,
                             idempotency_key, send_email)

key = idempotency_key("broadcast", delivery_id, "generation", 1)  # "{namespace}/broadcast/..."
message = OutboundEmail(from_email=settings.DEFAULT_FROM_EMAIL, to="one@example.test",
                        subject="...", html="...", text="...",
                        idempotency_key=key, reply_to=None)
result = send_email(message)               # never raises for transport problems
result = send_email(message, timeout=10)   # optional tighter total bound
```

`idempotency_key(*parts)` prefixes `EMAIL_IDEMPOTENCY_NAMESPACE` (D4's
`{environment}/...`), rejects empty parts or parts containing `/`, and enforces
the 256-character limit. All callers (COMM-02 included) should build keys
with it so environments sharing a Resend account never collide.

`OutboundEmail` is frozen, single-recipient and validated (no lists/commas,
single-line headers, key 1-256 characters). `SendResult.status` is:

| Status | Meaning | Resend mapping |
| --- | --- | --- |
| `accepted` | `provider_message_id` set; not proof of inbox delivery | 2xx with a non-empty `id` |
| `retryable` | Not accepted; same payload/key may be retried after `retry_after` seconds | 429 (`Retry-After` parsed), 409 `concurrent_idempotent_requests` |
| `failed` | Definitive; investigate, never rotate the key | other 4xx, 409 `invalid_idempotent_request`, missing API key |
| `uncertain` | May have been accepted; replay only the same request/key inside the safety window | total deadline exceeded, socket timeout, connection error, malformed HTTP (bad/overlong status line, incomplete read), any unexpected transport exception, 408, 5xx, unknown 409, redirect, 2xx without an id |

`code` is the provider error name uppercased (only plain identifiers) or
`HTTP_<status>`/`DEADLINE_EXCEEDED`/`TIMEOUT`/`CONNECTION_ERROR`/`TRANSPORT_ERROR`.
The Resend adapter posts to `https://api.resend.com/emails` with
`Authorization: Bearer`, `Idempotency-Key` and `User-Agent: pythagoras-backend/1.0`
and makes exactly one attempt. Its timeout is a **total wall-clock deadline**
covering DNS resolution, connect, send and read: the request runs in a daemon
worker joined against the deadline (DNS has no socket timeout), each socket
operation gets only the remaining budget, and the body is read one receive at a
time, capped at 64 KiB. Exceeding the deadline returns `uncertain
DEADLINE_EXCEEDED` (the request may still reach the provider). It refuses
redirects and logs only key/status/code/provider ID (never the API key, body or
addresses). It uses only the standard library.
`MemoryTransport` (`pythagoras.mail.memory.outbox`, `memory.reset()`) mimics
idempotency for tests and development. The test runner
(`pythagoras.test_runner.IsolatedTestRunner`) forces `memory` and a blank key.

Welcome mail: `accounts.services.EmailService` renders the existing
`emails/welcome.html`/`.txt` templates and sends with key
`{EMAIL_IDEMPOTENCY_NAMESPACE}/welcome/{user_id}/v1` (e.g.
`production/welcome/42/v1`) from `DEFAULT_FROM_EMAIL` (optional
`EMAIL_REPLY_TO`), inside `transaction.on_commit`, bounded by
`WELCOME_EMAIL_TIMEOUT_SECONDS`. Bump `WELCOME_MESSAGE_VERSION` whenever the
subject or templates change.

## Configuration

| Variable | Meaning |
| --- | --- |
| `EMAIL_TRANSPORT` | `resend` (default) or `memory` (sends nothing). |
| `RESEND_API_KEY` | Backend-only Resend send key. Blank yields `failed TRANSPORT_NOT_CONFIGURED`. |
| `DEFAULT_FROM_EMAIL` | Verified sender; blank yields `failed INVALID_MESSAGE` for welcome mail. |
| `EMAIL_REPLY_TO` | Optional reply-to. |
| `RESEND_TIMEOUT_SECONDS` | Total wall-clock bound per Resend request (default 30). |
| `WELCOME_EMAIL_TIMEOUT_SECONDS` | Bound for the welcome send inside the registration request (default 10, or `RESEND_TIMEOUT_SECONDS` if lower); startup fails if set above `RESEND_TIMEOUT_SECONDS`. |
| `EMAIL_IDEMPOTENCY_NAMESPACE` | Idempotency key prefix, 1-64 of `[A-Za-z0-9._-]`, default `local`. **Staging and production must set distinct values** (e.g. `staging`, `production`). |
| `TRUST_CLOUDFLARE_PROXY` | When true, throttles identify the client by the last `X-Forwarded-For` hop (`NUM_PROXIES=1`); otherwise by `REMOTE_ADDR`. |

`manage.py check --deploy` warns when the transport is `memory` or Resend is
missing its key/sender. Request-rate/daily budgets are not configured here;
COMM-02 introduces them with its rate budgeting.

## Known limitations

- Throttle counters use Django's default (process-local) cache. Deployment-wide
  enforcement needs a shared cache configured by the release/deployment WPs.
- The welcome send runs synchronously after commit, so a slow provider can add
  up to `WELCOME_EMAIL_TIMEOUT_SECONDS` (default 10 s) to the registration
  response. It is not retried; outcomes are only logged (no persisted delivery
  record). A send abandoned at its deadline may finish in its daemon thread.
- Reuse of a rotated refresh token is rejected but does not revoke the rest of
  that user's sessions.
- Tokens live in browser memory/`sessionStorage` per D0, not HttpOnly cookies.
