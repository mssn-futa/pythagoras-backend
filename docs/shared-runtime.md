# Shared backend runtime and API contracts (PLAT-02)

Authority: `docs/design-doc/D0-phase-1-architecture-and-shared-contracts.md`,
`D1-platform-identity-and-student-administration.md`, and
`D5-release-operations-and-handover.md` in the separate Project Pythagoras docs
repository, revision `485dea11c7304d245d9884f25bc355089fdb70fa`.
WP: `docs/wps/wps-platform-identity/PLAT-02-backend-runtime-and-shared-contracts.md`.

## Isolated setup

Use an isolated Git worktree, CPython 3.12 (verified), a new virtual environment,
and a worktree-local database. Do not reuse the primary checkout's environment
or database. This guide supersedes historical SMTP/runtime setup for this WP.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Populate `SECRET_KEY` in the ignored `.env` using a locally generated value
(for example Python's `secrets.token_urlsafe(64)`). Do not commit it or use a
production credential. The process fails when the key is absent. `.env` is
resolved relative to this worktree; explicitly supplied environment values win.
Use `PYTHON_DOTENV_DISABLED=1` for an environment-only process.

For this runtime WP, `DATABASE_URL=sqlite:///db.sqlite3` is acceptable. The
default when unset is an absolute worktree-local SQLite path. Constraint,
locking and concurrency acceptance in subsequent WPs requires isolated
PostgreSQL. Production uses external PostgreSQL with TLS.

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py test pythagoras.tests --noinput
.venv/bin/python manage.py test --noinput
.venv/bin/python manage.py help makemigrations
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py help spectacular
.venv/bin/python manage.py spectacular --validate --file .venv/schema.yaml
.venv/bin/python manage.py help collectstatic
DEBUG=false STATIC_ROOT="$PWD/.static-test" .venv/bin/python manage.py collectstatic --noinput
```

`collectstatic` writes only to the explicitly selected, ignored destination.
Production storage uses WhiteNoise's compressed manifest backend via Django's
`STORAGES`; normal uploads remain local until DAWAH-02 supplies R2 storage.
No model migrations are introduced by PLAT-02.

## Effective defaults and stable imports

- Renderer/handler: `pythagoras.utils.StandardizedJSONRenderer`,
  `pythagoras.utils.exception_handler`.
- Responses: `pythagoras.utils.success_response`, `APIError`.
- Schema: `pythagoras.schema.EnvelopeAutoSchema`.
- Pagination: `pythagoras.pagination.StandardPagination` (20 default, 100 max,
  `page` / `page_size`). Queryset ordering gains a primary-key tie-breaker.
- Fields: `pythagoras.serializers.IdentifiedSerializer` (numeric ID) and
  `UTCDateTimeField` (UTC with `Z`, including under an activated local timezone).
  The default DRF format is ISO-8601 and Django's default timezone is UTC.
- Permissions: `pythagoras.permissions.IsAdmin`, `IsStudent`,
  `IsAdminOrReadOnly`, `IsOwnerOrAdmin`. Existing `accounts.permissions`
  imports re-export the same classes. All require active authenticated users;
  product admin checks `role=admin`, never `is_staff`. Owner objects expose
  `user_id`, or are the user itself. Domain-specific owner fields need their
  own object check before access/mutation.
- API authentication is JWT bearer only. Django-admin session authentication,
  HTML rendering and CSRF middleware are retained separately.

Do not import DRF or project runtime utilities from settings. Class settings
are dotted strings. Root URLs never mutate effective DRF settings.

### Success and errors

Views may return raw DRF `Response(payload)` or `success_response(payload,
'Saved.', status=201)`. A complete existing envelope is preserved once.
Do not wrap data in a second domain envelope.

```json
{"success":true,"data":{"id":1},"message":"Saved."}
```

Paginated generic views use the same outer envelope with:

```json
{"count":1,"next":null,"previous":null,"results":[{"id":1}]}
```

Manually implemented APIViews must explicitly call the shared paginator;
changing the global default does not paginate their existing arrays. Preserve
domain filtering and select deterministic ordering before pagination.
Use 200 for retrieval/update/synchronous replay, 201 for create, 202 for queue
acknowledgement, and 204 for deletion. A 204 always has an empty byte body.

Raise DRF `ValidationError` for serializer/field failures:

```json
{"success":false,"data":{"code":"VALIDATION_ERROR","fields":{"title":["This field is required."]}},"message":"Correct the highlighted fields."}
```

Domain conflicts use explicit public errors:

```python
raise APIError('QUIZ_IMMUTABLE', 'Published quizzes cannot be changed.', status_code=409)
```

Other status mappings include 400 `MALFORMED_REQUEST` for parsing, 401
`AUTHENTICATION_REQUIRED`, 403 `PERMISSION_DENIED`, 404 `NOT_FOUND`, 429
`THROTTLED`, 500 `INTERNAL_ERROR`, and 503 `PROVIDER_UNAVAILABLE`.
`WWW-Authenticate` and `Retry-After` are preserved. Internal/provider failures
use fixed public text; arbitrary exception details and tracebacks are never
included by the shared handler. Its error log contains only the server-generated
request ID and safe error code. `X-Request-ID` is returned on HTTP responses.
Pass only intentionally public messages/fields into `APIError`/`ValidationError`.

### Schema adoption

Keep response annotations focused on **data payloads**. The shared schema
class adds the outer envelope and standard error shapes. Pagination appears
where the view actually declares/uses it; 204 has no content schema. Request
bodies remain unwrapped. `/schema/` serves the OpenAPI document itself and `/`
continues serving Swagger HTML. Root app mounts stay `/accounts/`, `/dawah/`,
and `/quiz/`.

Existing domain payload annotations need owner-WP correction where they were
already inaccurate: login declares a user but returns user/tokens; question
create/update and submission-create annotations differ from the serializers
actually returned. Existing `get_is_graded` method fields lack type annotations
and generate schema warnings. PLAT-02 tests verify shared wire shapes with
executable fixture views; they do not certify these legacy domain endpoints.
PLAT-03 and ASSESS-01 own the respective domain corrections and inactive-token
semantics. Canonical slash routes/aliases remain domain work.

## Configuration inventory

Only `SECRET_KEY` is required for startup. Provider slots may stay blank while
unimplemented; filling them does not enable a provider or prove its readiness.

| Environment settings | Meaning/default |
| --- | --- |
| `SECRET_KEY` | Required environment secret, no committed fallback |
| `DEBUG` | Defaults false; booleans accept true/false or 1/0 |
| `DATABASE_URL` | Worktree-local SQLite default; production external PostgreSQL/TLS |
| `ALLOWED_HOSTS` | Comma/whitespace-separated hosts; localhost/127.0.0.1 default |
| `CORS_ALLOWED_ORIGINS` | Exact frontend origins; empty default; credential-free bearer API |
| `CSRF_TRUSTED_ORIGINS` | Exact trusted admin origins; empty default |
| `TRUST_CLOUDFLARE_PROXY` | False default; opt in only after router trust verification |
| `SECURE_SSL_REDIRECT` | Defaults true outside DEBUG; local example disables it |
| `STATIC_ROOT` | Absolute isolated destination recommended; default worktree/staticfiles |
| `RESEND_API_KEY` | Backend-only send key; blank default |
| `DEFAULT_FROM_EMAIL` | Verified sender/domain; blank default |
| `EMAIL_REPLY_TO` | Optional reply-to; blank default |
| `RESEND_TIMEOUT_SECONDS` | 30-second bounded HTTPS setting |
| `RESEND_REQUESTS_PER_SECOND`, `RESEND_DAILY_REQUEST_BUDGET` | 2 / 100 starting budgets; configure to actual account limits |
| `R2_ACCOUNT_ID`, `R2_S3_ENDPOINT_URL` | Account and S3 API endpoint |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | Bucket-scoped backend-only credentials |
| `R2_PRIVATE_BUCKET`, `R2_PUBLIC_BUCKET` | Private staging/ready and separate public delivery buckets |
| `R2_PUBLIC_DOMAIN` | Public media HTTPS custom-domain base; no r2.dev dependency |
| `R2_UPLOAD_INTENT_TTL_SECONDS`, `R2_PREVIEW_TTL_SECONDS` | 900 / 300 seconds; signed capabilities remain short-lived |
| `R2_CORS_ALLOWED_ORIGINS` | Exact bucket upload origins; bucket CORS must be provisioned separately |
| `AUTOMATION_TIMEZONE` | Africa/Lagos |
| `AUTOMATION_CATCH_UP_DAYS`, `AUTOMATION_REMINDER_GRACE_SECONDS` | 1 day / 900 seconds, per D4 |

Positive numeric slots reject invalid/nonpositive values without echoing them.
All provider values are backend-only; no `VITE_` secrets. Public GA4 measurement
configuration belongs to the frontend. No YouTube credentials are needed.
The legacy Django email backend is in-memory until PLAT-03 adopts Resend; there
is no production SMTP fallback and no actual email acceptance claim here.
PLAT-03 owns the welcome adapter, COMM-02/06 own budget enforcement/automation,
and DAWAH-02 owns storage/URL validation and TTL enforcement.

## Production trust boundary and downstream handoff

`DEBUG=false` enables secure admin cookies and manifest static storage.
`TRUST_CLOUDFLARE_PROXY=true` trusts `X-Forwarded-Proto: https` and forwarded host.
Enable it only when the verified Worker/router strips client-supplied forwarding
headers, supplies canonical values, and prevents direct bypass to the container.
RELEASE-09 must prove this; the setting alone does not authenticate a proxy.
Use exact ALLOWED_HOSTS/CSRF/CORS origins. API CORS permits Authorization and
exposes X-Request-ID/Retry-After; it does not enable browser cookie credentials.

PLAT-03, RELEASE-01 and RELEASE-09 can consume this runtime foundation once its
commits are available in their base. CI configuration belongs to RELEASE-01;
Cloudflare/container/provider integration is not established by local runtime
tests. No accounts/dawah/quiz model or migration chains are changed.
