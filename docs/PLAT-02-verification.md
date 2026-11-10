# PLAT-02 verification record

Docs revision: `485dea11c7304d245d9884f25bc355089fdb70fa`.
Backend base: `209d46e18007796bcb701b12d16cc7cf4c3e45c6`.
Worktree: `/Users/ade/project-pythagoras-worktrees/PLAT-02-backend`.
Runtime: fresh CPython 3.12 virtual environment with pinned requirements.
Database: isolated worktree `db.sqlite3`; Django tests use their separate test DB.
No production/provider credentials were used.

## Baseline

- `.venv/bin/python manage.py check`: passed.
- `.venv/bin/python manage.py test --noinput`: 3 existing grading tests passed.
- `.venv/bin/python manage.py shell -c "from rest_framework.settings import api_settings; print('renderers:', [c.__module__ + '.' + c.__name__ for c in api_settings.DEFAULT_RENDERER_CLASSES]); print('schema:', api_settings.DEFAULT_SCHEMA_CLASS.__module__)"`:
  reproduced built-in JSONRenderer/BrowsableAPIRenderer and built-in DRF schema
  instead of configured project defaults. Cold-process regression tests now
  assert effective defaults before root URL import, plus ASGI/WSGI startup.

## Commands and results

Run in the worktree with its ignored local environment:

| Command | Result |
| --- | --- |
| `python3.12 -m venv .venv` | Fresh isolated environment created |
| `.venv/bin/python -m pip install -r requirements.txt` | Pinned requirements installed, including django-cors-headers 4.9.0 |
| `.venv/bin/python manage.py check` | Passed; no issues |
| `.venv/bin/python manage.py test pythagoras.tests --noinput` | Initial 25 shared tests passed; subsequent regression coverage included in full suite below |
| `.venv/bin/python manage.py migrate --noinput` | All existing migrations applied successfully to isolated SQLite |
| `.venv/bin/python manage.py help makemigrations` | Confirmed supported drift flags |
| `.venv/bin/python manage.py makemigrations --check --dry-run` | Passed; no changes detected |
| `.venv/bin/python manage.py help spectacular` | Confirmed supported generation/validation flags |
| `.venv/bin/python manage.py spectacular --validate --file .venv/schema.yaml` | Passed; zero errors, 3 warnings / 2 unique existing quiz method-field warnings |
| `.venv/bin/python manage.py help collectstatic` | Confirmed supported static flags |
| `DEBUG=false STATIC_ROOT="/Users/ade/project-pythagoras-worktrees/PLAT-02-backend/.static-test" .venv/bin/python manage.py collectstatic --noinput` | Passed; 163 copied, 777 post-processed |
| `DEBUG=false .venv/bin/python manage.py check` | Passed; no issues |
| `.venv/bin/python manage.py test --noinput` | Final run passed: 30 tests (27 shared runtime/contract tests and 3 existing grading tests), including isolated production-mode collectstatic/admin/HTTPS test |
| `.venv/bin/python -m pip check` | Passed; no broken requirements |
| `git diff --check` | Passed |

The production-mode subprocess test collects static into an owned
temporary directory, verifies hashed admin CSS is served, confirms secure admin
CSRF cookies/HTML and rejection without CSRF, and exercises forwarded HTTPS
with redirects enabled. The same production-like checks were also exercised
manually against the isolated `.static-test` output, including unwrapped OpenAPI.

## Scope of evidence and outstanding acceptance

- Shared wire tests exercise raw/pre-enveloped success and errors, 204, field
  and authentication errors, internal/provider sanitization, correlation IDs,
  throttling headers, pagination/order, and OpenAPI payload validation.
- Fake sentinel values test environment consumption without real credentials.
  No Resend/R2/Cloudflare provider acceptance is claimed.
- Existing quiz `get_is_graded` fields lack schema type hints (2 unique
  warnings). Domain payload annotation mismatches remain owner-WP work as
  described in `shared-runtime.md`; shared schema wrapping is validated.
- No model/migration chains were edited. No production database was touched.
- `gh pr checks 24` reports no checks. `gh pr view 24 --json statusCheckRollup`
  reports an empty list; `gh run list --branch platform/PLAT-02-backend-runtime-and-shared-contracts --json databaseId,status,conclusion,workflowName`
  reports no runs. CI acceptance is unavailable, not passed. RELEASE-01 owns
  backend CI configuration; this PR remains draft for review/integration.
- SQLite verifies initial runtime/migrations only. PostgreSQL constraint,
  concurrency and actual Cloudflare router trust acceptance remain downstream.
