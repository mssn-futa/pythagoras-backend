# Backend foundation review — 2026-10-23

## Scope and references

PLAT-02 runtime/contracts, RELEASE-01 PostgreSQL CI, RELEASE-11 portable legacy
fixtures. Root docs revision `f654eacb59d3524a45025fbe144ff4cf6c8153b3`, including
D0/D1/D3/D5 and the lead's takeover integration amendment. Product WPs remain
bounded; passing this foundation is not domain API or production acceptance.

Worktrees under `/Users/ade/project-pythagoras-worktrees/`:
`backend-runtime-review`, `RELEASE-01-backend`, `RELEASE-11-backend`, and
`backend-foundation`. All writes/migrations used newly created local PostgreSQL
databases, not primary-checkout environment/data. The primary README deletion
and untracked files were preserved.

## Review findings and disposition

What this change does: PLAT-02 fixes cold DRF initialization and response/error
contracts. RELEASE-01 verifies startup, schema, migration drift, production
static build, and all tests on PostgreSQL. RELEASE-11 fixes invalid test data
that SQLite had accepted.

- Inspected all changed runtime settings, renderer/error handler, schema adapter,
  middleware, pagination, role/ownership permissions and compatibility imports,
  field helpers, root URLs, environment example, and connected tests/callers.
- Confirmed string-based DRF configuration avoids importing API defaults while
  settings are incomplete. JWT API configuration leaves Django-admin HTML,
  sessions and CSRF intact. Proxy header trust is explicit and defaults off.
- Response tests exercise raw/pre-enveloped success, empty 204, field/auth/state/
  throttle/provider/internal errors, safe request-ID logging, wire-schema
  validation and pagination ordering/caps. Error logs do not print exception
  text or request payloads. Settings tests use fake credential sentinels.
- Inspected CI triggers and steps: draft/ready PRs are not skipped; PostgreSQL is
  ephemeral; no production/provider secrets; migrations/drift/full tests have no
  skips or allowed failures. Controlled assertion/drift probes failed correctly.
- Reproduced the fixture failure on PostgreSQL locally and in Linux CI. Reviewed
  the four-line repair against `Question.Type` and `QuizService` exact-set grading.
  The three original assertions now run and pass, with no production changes.

Assumed load: shared helpers under normal Django requests and isolated CI jobs.
No claim is made for domain locking/concurrency or production-wide throttling;
the owning later packages must supply those tests and configuration.

Verdict: ship the reviewed foundation into the human-test feature branch after
the actual integrated head is green. Keep its PR to `dev` open for human testing.

## Executed verification

CPython 3.12, PostgreSQL 15, a fresh isolated review venv and cluster bound to
`127.0.0.1:55439`. Databases `pythagoras_runtime_review`, `pythagoras_release01`,
and `pythagoras_release11` were created for this session. Test commands create
and destroy their separate Django test databases.

- `python -m pip install -r requirements.txt` and `python -m pip check`: pass.
- `manage.py check`, fresh `migrate --noinput`, `makemigrations --check --dry-run`:
  pass. Relevant command help was inspected first.
- `manage.py spectacular --validate --file <isolated-output>`: zero errors;
  3 existing warnings / 2 unique quiz method-field annotations.
- `DEBUG=false manage.py check` and isolated `collectstatic --noinput`: pass;
  163 files copied, 777 post-processed.
- PLAT-02 full SQLite suite: 30 pass. PostgreSQL initially: 27 shared tests pass,
  3 grading fixtures error. After RELEASE-11: all 30 PostgreSQL tests pass;
  targeted grading tests: 3 pass. No tests skipped.
- Temporary false assertion and missing-migration probes: each exits 1. Probes
  removed; original model restored byte-for-byte; no migration files written;
  drift check returns `No changes detected`.
- Linux pipeline initially catches the 3 fixture errors (run 39948853233), then
  all stages pass with the repair (run 39949464040). See PR #26 and the subsequent
  feature-head checks for exact integrated-head results.

## Remaining work

PLAT-03 supplies complete identity/blacklist/profile/Resend behavior. ASSESS-01
still repairs assessment API/security, with later lifecycle/grading WPs required.
Shared pagination must be explicitly adopted by legacy manual APIViews. Existing
domain schema inaccuracies are not certified as fixed by the shared wrapper.
Actual Cloudflare proxy trust, container/PostgreSQL/provider deployment and
concurrency proofs remain assigned work. No production migration/provider call
or live end-to-end product acceptance was performed here.
