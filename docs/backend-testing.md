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

**2026-10-23 resolution:** RELEASE-11 PR #27 corrects only the fixture enum
values and the accidental answer-string concatenation. All 30 tests now pass
on PostgreSQL locally and in the complete Linux merge-candidate gate (run
39949464040). No model, service, schema, or grading assertion changed. The
description below records the original failure, not the composed current state.

At PLAT-02 `669cb9e`, the 27 shared runtime/contract tests pass on PostgreSQL.
All three historical `quiz.tests.TestQuizService` cases error in fixture setup:
`Question.type` is `varchar(5)`, but the fixture inserts `MULTIPLE_ANSWER`.
SQLite previously accepted the oversized value. Fresh migrations and drift
checks pass; no schema widening or fixture suppression is made in this WP.

RELEASE-11 owns this portable legacy fixture repair under the takeover
decomposition; ASSESS-01 still owns API/security behavior after PLAT-03. Passing
the legacy suite does not establish assessment endpoint safety. This workflow
exposed the baseline failure before its repair. Existing quiz schema type
warnings are retained for their owner.

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

### Takeover verification record — 2026-10-23

Used a newly initialized PostgreSQL 15 cluster bound to loopback port 55439,
with database `pythagoras_release01`, outside the primary checkout. Reused the
fresh CPython 3.12 review venv at
`/Users/ade/project-pythagoras-worktrees/backend-runtime-review/.venv`; the
application and management commands ran from the RELEASE-01 worktree. No
primary-checkout environment or database was copied.

Passed: dependency compatibility, system checks, fresh migrations, drift check,
schema validation (zero errors, 3 warnings / 2 unique), production system checks,
production `collectstatic` (163 copied, 777 post-processed), and all 27 shared
tests. The full 30-test PostgreSQL suite at the identical PLAT-02 code revision
failed on exactly the 3 fixture errors above; SQLite passed all 30.

The temporary false-assertion test exited 1 under
`manage.py test pythagoras.tests.test_ci_failure_probe --noinput`. Adding a
temporary `CustomUser.ci_failure_probe` field made the ordinary
`makemigrations --check --dry-run` exit 1 and report a required migration. Both
probes were removed; no migration was written, and the drift check returned to
`No changes detected`. No domain source changes remain in this WP.

First workflow attempt failed validation before starting jobs because the
runner context is unavailable in job-level environment definitions. The static
directory now uses a job-VM-local `/tmp` path. CI must still demonstrate the
actual matrix; the fixture failure remains an explicit integration blocker.

The corrected workflow subsequently ran all stages on Linux in run 39948853233:
only the full suite failed on the same three fixtures. RELEASE-11 then made the
complete merge-candidate pipeline green in run 39949464040, proving the gate
both detects and accepts the actual repaired database case. Review and final
integrated-head results are recorded in [foundation review](backend-foundation-review.md).
