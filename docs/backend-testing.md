# Backend verification

## Runtime and isolation

Use CPython 3.12, `requirements.txt`, and PostgreSQL 15. GitHub Actions provisions
a fresh service/database for each job; no deployment or repository secrets are
used. Dummy CI credentials are not production credentials. The workflow runs on
draft and ready PRs, `main`/`dev` pushes, and manual dispatch, with no skip filters
or allowed failures.

Create a venv in an isolated worktree and install:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

Supply environment separately, without committing or printing local secrets.
`PYTHON_DOTENV_DISABLED=1` prevents accidental `.env` loading. Set `SECRET_KEY`
to a disposable test value, `DEBUG=true`, `ALLOWED_HOSTS=localhost,127.0.0.1,testserver`,
and `DATABASE_URL` to an explicitly isolated PostgreSQL database whose user can
create Django's test database. Never point these commands at shared/production
data. Set `STATIC_ROOT` to a worktree-local or temporary output directory.

## Executable check matrix

Command help was inspected before using migration/schema/static flags.

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py spectacular --validate --file .venv/schema.yaml
DEBUG=false .venv/bin/python manage.py check
DEBUG=false .venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py test --noinput
```

The workflow executes all of these gates, plus dependency compatibility. The
full test command has no label/skip filter. For development only, a targeted
command such as `manage.py test pythagoras.tests --noinput` is useful; it does
not replace complete-suite acceptance. PostgreSQL failures must not be hidden
by switching CI to SQLite.

The runtime tests also use isolated cold subprocesses/SQLite to test startup,
production static delivery, Django-admin HTML/CSRF and forwarded HTTPS. These
do not replace PostgreSQL model/constraint/transaction checks.

## Known baseline blocker

At PLAT-02 `669cb9e`, the 27 shared runtime/contract tests pass on PostgreSQL.
All three historical `quiz.tests.TestQuizService` cases error in fixture setup:
`Question.type` is `varchar(5)`, but the fixture inserts `MULTIPLE_ANSWER`.
SQLite previously accepted the oversized value. Fresh migrations and drift
checks pass; no schema widening or fixture suppression is made in this WP.

ASSESS-01 owns the portable assessment fixtures and safety repair, after its
PLAT-03 identity prerequisite. RELEASE-01 remains draft/blocked until that
integration is verified and the complete PostgreSQL suite is green. This
workflow deliberately exposes the failure rather than certifying a false green
baseline. Existing quiz schema type warnings are also retained for their owner.

## Controlled failure proof

Before acceptance, demonstrate that:

1. A temporary discovered Django test with a false assertion makes the configured
   test command exit nonzero.
2. A temporary model field without a migration makes `makemigrations --check
   --dry-run` exit nonzero, without writing migration files.
3. Remove both probes and rerun the normal checks. Never commit the deliberate
   defects, fake migration history, or weaken assertions to pass.

Record actual local and CI outcomes in the WP PR. No live provider/network
integration or production-engine concurrency coverage is implied by startup
and fixture tests. Domain WPs add their required tests to this full-suite gate.
