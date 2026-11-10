# Cloudflare container runtime and job proof (RELEASE-09)

Status: **local half complete; staged Cloudflare proof BLOCKED** (no Cloudflare
account, API token, or managed PostgreSQL has been supplied). Nothing in this
document is deployment evidence. Public Cloudflare documentation is a reference,
not proof that this application runs there.

Authority: `D5-release-operations-and-handover.md` (Environment and production
configuration; Cloudflare hosting topology; Bounded scheduled processing) and
`D0-phase-1-architecture-and-shared-contracts.md` ("Runtime and external
providers") in the Project Pythagoras docs repository, revision `df67388`.
WP: `docs/wps/wps-release-operations/RELEASE-09-cloudflare-container-runtime-and-job-proof.md`.

## Architecture

```
public HTTPS --> Worker (src/index.ts, fetch)
                   |  replaces X-Forwarded-Proto/Host/For from the verified URL,
                   |  strips client forwarding headers, then ->
                   v
                 WebContainer (Durable Object, @cloudflare/containers)
                   |  start if needed -> TCP port 8000 -> REAL HTTP GET /readyz/
                   |  must be 200 {"status":"ok"} (DB included) before traffic
                   v
                 container: PYTHAGORAS_ROLE=web  (gunicorn pythagoras.wsgi)  --TLS--> PostgreSQL (external)

Cron Trigger (* * * * *, UTC) --> Worker scheduled() --> JobContainer "job-singleton".tick()
                   |  active run?  yes -> ATTACH (no second start)
                   |               no  -> start container, PYTHAGORAS_ROLE=job-probe
                   v
                 container: python -m pythagoras.job_probe  --> exit code
                   '--> container stop event (monitored exit) --> recorded in the DO, logged
```

* One image, one `Dockerfile`, two Container applications (`pythagoras-web`,
  `pythagoras-job`) declared in `cloudflare/wrangler.jsonc`.
* No public HTTP endpoint starts a job, selects a role, or passes arguments. The
  Worker `fetch()` only touches `env.WEB`; `JobContainer.fetch()` returns 404; the
  sole caller of `tick()` is `scheduled()`. The role is chosen by the controller via
  a fixed environment value, never from request data, and no `entrypoint` override
  is ever passed (a source-level test enforces this).
* Cron/Durable Object coordination is **not** exactly-once correctness. Database
  uniqueness/leases remain the authority for real jobs (COMM-02/06, RELEASE-03).
* Durable data belongs in PostgreSQL/R2. Container disk and Durable Object storage
  hold only the controller's run record (last 20 runs) and are disposable.

## Image roles (`docker-entrypoint.sh`)

The entrypoint takes **at most one** argument (a role) or `PYTHAGORAS_ROLE`
(default `web`). Anything else exits nonzero; there is no argv passthrough.

| Role | Behavior | Exit |
|------|----------|------|
| `web` | `gunicorn pythagoras.wsgi:application` on `$PORT` (default 8000), 2 workers x 4 threads (`WEB_CONCURRENCY` overrides workers), `exec` so SIGTERM reaches gunicorn | gunicorn |
| `job-probe` | `timeout` around `python -m pythagoras.job_probe`: Django `check --database default`, connect, `SELECT 1`, report server major version and `pg_stat_ssl` TLS flag as one JSON line; hard watchdog at `JOB_PROBE_BUDGET_SECONDS` (default 240) | 0 ok, 1 failure, 124 budget exceeded |
| `migrate` | **Operator only.** `manage.py migrate --noinput`; refuses unless `PYTHAGORAS_ALLOW_MIGRATE=yes`. Not in the controller allow-list, never run by `web` or `job-probe`. | 78 without opt-in |
| anything else | rejected | 64 |

Health endpoints (plain Django views in `pythagoras/health.py`, envelope-free,
`Cache-Control: no-store`, GET/HEAD only, no auth, no PII):
`GET /healthz/` liveness `{"status":"ok"}`; `GET /readyz/` runs `SELECT 1` and
returns `{"status":"ok"}` or `503 {"status":"unavailable"}` (log line carries the
exception class only).

Image build choices:

* `python:3.12-slim` pinned by digest `sha256:5e2bdb1b...fe13a4fa5b` (CPython
  3.12.16, Debian 13, linux/amd64). Bump deliberately.
* Non-root `app` (uid/gid 10001); `/app` and `/app/staticfiles` are read-only to it.
* `collectstatic` runs **at build** with a throwaway random `SECRET_KEY` generated
  inside that single `RUN` (never in a layer, `ENV`, `ARG`, or file; the command in
  `docker history` shows only the unexpanded `$(python ...)`). Reason: WhiteNoise's
  manifest storage requires the manifest before the first request, and baking it
  keeps cold starts fast and the runtime filesystem read-only. The real
  `SECRET_KEY` must be injected at start; the settings refuse to import without it.
* Optional BuildKit secret `build_ca` lets TLS-intercepting build networks supply a
  CA bundle without it entering a layer (`--secret id=build_ca,src=...`). Without
  it that step is a no-op. CI does not use it.
* `PYTHON_DOTENV_DISABLED=1` in the image: configuration is environment-only.

## Trusted proxy headers

PLAT-02 settings (unchanged here): `TRUST_CLOUDFLARE_PROXY` (env bool, **default
off**) drives both `SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO','https')`
and `USE_X_FORWARDED_HOST`. `SECURE_SSL_REDIRECT` defaults to `not DEBUG`, and
`SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` to `not DEBUG`.

Findings and consequences:

1. With `DEBUG=false` and the proxy flag **off**, every plain-HTTP request,
   including a health probe, is 301-redirected to HTTPS. The Cloudflare deployment
   must therefore set `TRUST_CLOUDFLARE_PROXY=true` and the Worker must always send
   `X-Forwarded-Proto: https` and `X-Forwarded-Host` (it does, for real traffic and
   its own readiness probe).
2. Gunicorn independently honours `X-Forwarded-Proto` from `127.0.0.1` by default,
   which would make the Django flag meaningless locally. The entrypoint therefore
   passes `--forwarded-allow-ips '*'` only when `TRUST_CLOUDFLARE_PROXY` is
   true and `''` otherwise: a single policy. Without this the spoofed-header case
   returned 200 with the flag off; with it, 301.
3. Trusting these headers is safe only because the Worker overwrites them
   (`src/routing.ts`); the container must never be reachable except via the Worker.
4. `X-Forwarded-For` is set to Cloudflare's `CF-Connecting-IP` (client value
   discarded) so DRF throttle identification is not spoofable.
5. `ALLOWED_HOSTS` is validated against `X-Forwarded-Host`, so it must contain the
   public API hostname (and the `workers.dev` hostname if that is used for the proof).

Required deployment environment (names only; see the inventory below):
`DEBUG=false`, `TRUST_CLOUDFLARE_PROXY=true`, `SECURE_SSL_REDIRECT=true`,
`ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` (https origin of the API for admin),
`CORS_ALLOWED_ORIGINS`.

## Environment and secret inventory (names only; no values anywhere in the repo)

Worker Secrets (`wrangler secret put`/`--secrets-file`; declared in
`secrets.required`):

| Name | Role(s) | Notes |
|------|---------|-------|
| `SECRET_KEY` | web, job-probe | required |
| `DATABASE_URL` | web, job-probe | required; `postgresql://...?sslmode=verify-full&sslrootcert=system` |
| `RESEND_API_KEY` | web only | forwarded when bound (PLAT-03 / COMM) |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | web only | forwarded when bound (DAWAH-02); distinct from the deploy token |

Non-secret `vars` (placeholders in `wrangler.jsonc`, `*.staging.invalid`; override
per environment): `PUBLIC_API_HOST` (controller only), `ALLOWED_HOSTS`,
`CSRF_TRUSTED_ORIGINS`, `CORS_ALLOWED_ORIGINS`, `DEBUG`, `TRUST_CLOUDFLARE_PROXY`,
`SECURE_SSL_REDIRECT`, `WEB_CONCURRENCY`; plus any of the other names in
`WEB_PLAIN_NAMES` (`src/config.ts`) when those features land. `job-probe` receives
only `SECRET_KEY`, `DATABASE_URL`, `JOB_PROBE_BUDGET_SECONDS`, `PYTHAGORAS_ROLE`,
`PORT`. Missing required secrets make the Worker answer `503 service_misconfigured`
and the cron tick skip, logging the missing **names** only.

Deploy credentials (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`) are separate,
local to the operator, and never in the Worker or image.

Log scrubbing: controller logs are JSON lines with event names and coarse fields;
every line also passes through a scrubber that removes bound secret values and URL
credentials. Error messages are never logged, only error class names. The Containers
helper returns raw exception text in a `500` body when its proxy hop fails;
`scrubProxyError` replaces that with a fixed `502 {"error":"upstream_unavailable"}`.

## Versions used

| Component | Version |
|-----------|---------|
| Docker engine / BuildKit | 29.9.2 (linux/amd64) |
| Base image | `python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b` (CPython 3.12.16) |
| Django / gunicorn / psycopg2-binary | 5.2.9 / 23.0.0 / 2.9.11 (bundled libpq 17.6) |
| Local PostgreSQL | 16.15 (host), 16-alpine (TLS test container) |
| Node / npm | 22.23.4 / 10.9.9 |
| `wrangler` | 4.160.0 |
| `@cloudflare/containers` | 0.3.7 |
| `@cloudflare/workers-types` | 5.20261110.1 |
| `vitest` / `typescript` | 5.1.0 / 5.9.3 |
| Wrangler `compatibility_date` | 2026-10-01 |

All exact-pinned in `cloudflare/package.json` with a committed `package-lock.json`.

## Local proof: commands and actual results (2026-11-10)

Docker commands used `--network host` against the isolated database
`postgresql://ci@127.0.0.1:55432/pyth_release09`. Credentials below are throwaway
values generated per run.

### Image
```sh
docker build --platform linux/amd64 --secret id=build_ca,src=/root/.ccr/ca-bundle.crt -t pythagoras-backend:release09 .
```
Succeeded (the build network in the proof sandbox intercepts TLS, hence the
optional secret). `Architecture=amd64 Os=linux`, user `10001:10001`, **uncompressed
size 333 MB; `docker save | gzip -1` about 74 MB**. `docker history --no-trunc`
shows no key, password, or ARG; the `collectstatic` layer shows the unexpanded
command substitution. Image env contains no `SECRET_KEY`. `touch /app/x` as the
runtime user: permission denied.

### Roles
* `bogus` -> exit 64; `web id` (extra arg) -> 64; `sh` -> 64; `PYTHAGORAS_ROLE=` (empty) -> 64.
* `migrate` without the flag, and with `PYTHAGORAS_ALLOW_MIGRATE=true` -> exit 78, no database touched.
  Running `web` against the empty database left 0 tables (no automatic migration).
  `PYTHAGORAS_ALLOW_MIGRATE=yes ... migrate` applied all migrations.

### `web` role
* `DEBUG=false`, trust flag off: plain request -> 301; spoofed `X-Forwarded-Proto` -> 301 (after the gunicorn fix); bad Host -> 400.
* `TRUST_CLOUDFLARE_PROXY=true`: `/healthz/` 200 `{"status": "ok"}`; `/readyz/` 200 (DB reached);
  without the proto header -> 301 to `https://api.test.example/...`; `X-Forwarded-Host: evil.example` -> 400;
  `/admin/login/` 200 "Log in | Django site admin" with `Secure` + `SameSite=Lax` CSRF cookie;
  admin POST without CSRF -> 403; with CSRF token + trusted `Origin` -> 200 (login form, bad credentials message);
  with a foreign `Origin` -> 403; `/static/admin/css/base.css` 200 (WhiteNoise); `HEAD /healthz/` 200.
* Database unreachable at boot: `/healthz/` 200, `/readyz/` 503 `{"status": "unavailable"}`, log shows only `OperationalError`.
* `docker stop` -> clean gunicorn shutdown in about 1.3 s.
* Cold start of the container to first `/readyz/` 200 locally (image cached): about 1.3 s;
  idle memory about 102 MiB with 2 workers.

### Persistence and ephemeral disk
A Django session row was written to PostgreSQL and `/tmp/marker.txt` to container
disk. After `docker restart` (same container) the file remained (a restart is not a
Cloudflare sleep). After `docker rm` and a **fresh** `docker run` of the same image,
the file was gone (`No such file`) and the PostgreSQL row was read back. This models
Cloudflare behavior (each wake is a fresh container filesystem) and is not a
Cloudflare result.

### `job-probe`
* Against local PG: exit 0, `{"elapsed_ms": 899, "ok": true, "probe": "job-probe", "server_major": 16, "tls": false, "vendor": "postgresql"}`.
* Unreachable DB (port 1): exit 1, `{"error": "OperationalError", "ok": false, ...}` with no DSN text.
* A server that accepts TCP and never answers, `PGCONNECT_TIMEOUT=60`, `JOB_PROBE_BUDGET_SECONDS=5`:
  watchdog fired, **exit 124 after 5.3 s** (`budget_exceeded`).
* `JOB_PROBE_BUDGET_SECONDS=abc` -> exit 64.

### Native TLS (throwaway `postgres:16-alpine`, `ssl=on`, self-signed CA, SAN `localhost`/`127.0.0.1`)
| `DATABASE_URL` query | Result |
|----------------------|--------|
| `sslmode=disable` | ok, `tls:false` |
| `sslmode=require` | ok, `tls:true` |
| `sslmode=verify-full&sslrootcert=/certs/ca.crt` | ok, `tls:true` |
| `sslmode=verify-full` (CA not trusted) | exit 1 |
| `sslmode=verify-full&sslrootcert=system` + `SSL_CERT_FILE=ca.crt` | ok, `tls:true` |

`/readyz/` over `verify-full` also returned 200. psycopg2-binary 2.9.11 bundles
libpq 17.6, which supports `sslrootcert=system` (OpenSSL default trust store), the
appropriate setting for a publicly trusted provider such as Neon. This proves the
driver and image, **not** Neon or any Cloudflare-to-database network path.

### Secret redaction
`SECRET_KEY` and the database password (planted in `DATABASE_URL`) were grep-ed in
`docker logs` of web runs (including an unreachable-database run): 0 matches.
Entry-point output never echoes environment values.

### Controller and config
```sh
cd cloudflare && npm ci --ignore-scripts && npm run typecheck && npm test
```
`tsc --noEmit` clean; **vitest: 6 files, 35 tests passed** (job overlap/attach
including truly concurrent ticks, monitored success/failure exit, idempotent stop
events, reconciliation after a coordinator restart, overdue force-stop at
budget+60 s, adoption of an unrecorded active container, start failure,
missing secrets, no secret in logs, role allow-list rejecting `migrate`/arbitrary
strings, header replacement/stripping, HTTP-based readiness including 5xx and
retry, proxy-error scrubbing, structural check that the public fetch path never
reaches the job binding or an entrypoint override).

```sh
npx wrangler --version                       # 4.160.0
WRANGLER_SEND_METRICS=false npx wrangler deploy --dry-run --outdir .wrangler-dryrun
```
`wrangler deploy --dry-run` needs no login and no network for Cloudflare: it
bundled the Worker, **built both container images with local Docker** (using the
Dockerfile), listed the DO and variable bindings and exited 0. A deliberately wrong
`class_name` made it fail ("Durable Objects ... not exported ... NoSuchClass"), so it
does validate the DO/container wiring. It does not validate cron syntax, account
entitlement, or image acceptance by the registry.

Django suite (PostgreSQL, env as in `docs/backend-testing.md`): `manage.py check`
clean, `makemigrations --check --dry-run` no changes, `manage.py test --noinput`
**37 tests OK** (30 existing + 7 added for health/probe).

## What changed in shared files

* `pythagoras/urls.py`: **one line** added at the top of `urlpatterns`:
  `path('', include('pythagoras.health')),`.
* `pythagoras/settings.py`: **no edit.** The probe is `python -m pythagoras.job_probe`
  (not a management command) specifically to avoid an `INSTALLED_APPS` change; the
  libpq connect bound uses `PGCONNECT_TIMEOUT`.
* The existing CI workflow is untouched; `.github/workflows/cloudflare-runtime.yml`
  is a separate lane (controller tests + offline dry run; image build + role,
  probe and web checks on a PostgreSQL service). It has no deploy step or secret.
* `render.yaml` and `build.sh` are left as-is (legacy; `build.sh` migrates, the image never does).

## Known limits and cost items to verify on the real account

Unverified until the lead supplies access; record actual values then.

* Workers Paid plan active and **Containers entitlement/beta access** on the account;
  per-account limits on concurrent container instances, total image storage, and
  max image size (this image is about 74 MB compressed; `wrangler` instance types
  per the installed config schema: `lite` 1/16 vCPU 256 MiB 2 GB, `basic` 1/4 vCPU
  1 GiB 4 GB, `standard-1` 1/2 vCPU 4 GiB 8 GB; the config uses `basic` for both,
  idle web memory was about 102 MiB locally).
* Billing dimensions to read off the dashboard after the proof: container
  vCPU/memory/disk-time while running (the cron job container runs about 1-2 s per
  minute, 1,440 starts/day), Durable Object requests and duration (the web DO and the
  job DO), Worker requests, cron invocations, egress, and registry storage.
  A one-minute cron means a cold container start per minute unless the platform
  reuses an instance: measure start latency and cost, and consider a longer
  interval for the final scheduler if cost or start latency is unacceptable.
* Cold start on Cloudflare (image pull plus Django import plus DB connect) and the
  Worker's `READY_BUDGET_MS` (45 s) / `sleepAfter` (web 10 m) tuning.
* Whether `start()` on the job container returns promptly and the stop event
  (`onStop` exit code) is delivered after a clean exit; this implementation relies
  on `@cloudflare/containers` 0.3.7 `onStop({exitCode})` and `getState()`
  (`stopped_with_code`) and cannot confirm them without the platform.
* Scheduled-handler and container monitor wall-time limits relative to the 240 s
  budget (D5 states the budget is below them; verify).
* Outbound TCP 5432/TLS egress from Containers (`enableInternet = true`) to the
  chosen PostgreSQL provider, and the provider's connection limits. Web container
  uses gunicorn 2 workers x 4 threads with `CONN_MAX_AGE=600` (PLAT-02 settings):
  up to 8 persistent connections per container, 16 for `max_instances: 2`, plus 1 for
  the job. Prefer the provider's **direct** (non-pooled) endpoint for this proof:
  transaction-mode poolers need `DISABLE_SERVER_SIDE_CURSORS`, which settings do not
  set.
* Image updates and Worker updates roll out at different times (see
  `--containers-rollout` in `wrangler deploy --help`): verify compatible rollout and
  rollback.
* D0 describes "Gunicorn-ASGI"; this proof serves WSGI (`pythagoras.wsgi`, gthread)
  as instructed. `asgi.py` exists; switching worker class is a RELEASE-03 decision.

## BLOCKED: staged Cloudflare proof

Required from the lead (none exists yet):

1. Cloudflare account on **Workers Paid** with Containers enabled; account id; an
   API token with Workers Scripts/Containers/Durable Objects edit (and Workers
   Routes/DNS edit if a custom domain is used).
2. An isolated managed PostgreSQL (Neon recommended) with a TLS `DATABASE_URL`
   (direct endpoint, test schema only, never production data).
3. A staging hostname/zone decision (or permission to use `workers.dev`), and
   staging `SECRET_KEY`. No Resend or R2 credentials are needed for this proof.

Checklist (all currently **not done**):

- [ ] Account/plan/Containers entitlement recorded (`wrangler whoami`; dashboard)
- [ ] Image accepted and pushed by `wrangler deploy` (registry, size, limits)
- [ ] Django served through Worker -> DO -> container routing; `/healthz/`, `/readyz/`, admin login over the real hostname
- [ ] Readiness gate observed on a cold start (503 `container_not_ready` then 200)
- [ ] Forwarded host/proto, admin CSRF and cookies correct over the real hostname
- [ ] Neon (or approved PG) TLS connection with `sslmode=verify-full&sslrootcert=system`
- [ ] Secret injection works; secrets absent from `wrangler tail` output and dashboard variables
- [ ] Cron Trigger fires with no user traffic; `job_run_started` then `job_run_succeeded` in logs
- [ ] Overlap: a run longer than one minute is attached to by the next tick (temporarily raise `JOB_PROBE` duration, or inspect `job_tick_attached`)
- [ ] Monitored failure: unreachable DB secret -> `job_run_failed` with exit code 1
- [ ] Idle/restart: web sleeps after `sleepAfter`, wakes, state read back from PostgreSQL
- [ ] Ephemeral disk observed on Cloudflare
- [ ] Cold-start latency, memory, plan limits, and billing/usage figures recorded
- [ ] Missed/duplicate tick behavior over an extended window
- [ ] Teardown (`wrangler delete`) and removal of the proof database

Commands for the lead (names and flags verified against the installed Wrangler
4.160.0 `--help`; run in `cloudflare/` after `npm ci --ignore-scripts`):

```sh
export CLOUDFLARE_API_TOKEN=...   CLOUDFLARE_ACCOUNT_ID=...      # operator shell only
export WRANGLER_SEND_METRICS=false
npx wrangler whoami
npx wrangler deploy --dry-run --outdir .wrangler-dryrun        # offline sanity (already passes)

# secrets: JSON or .env file outside the repo, e.g. {"SECRET_KEY":"...","DATABASE_URL":"postgresql://...?sslmode=verify-full&sslrootcert=system"}
npx wrangler deploy \
  --var PUBLIC_API_HOST:<host> --var ALLOWED_HOSTS:<host> \
  --var CSRF_TRUSTED_ORIGINS:https://<host> --var CORS_ALLOWED_ORIGINS:https://<frontend-origin> \
  --secrets-file /path/outside/repo/staging-secrets.json
# (alternative: wrangler secret put SECRET_KEY / DATABASE_URL, or wrangler secret bulk <file>, after the first deploy)

npx wrangler secret list
npx wrangler containers list
npx wrangler containers info <APP_ID>
npx wrangler containers instances <APP_ID> --json
npx wrangler containers images list
npx wrangler deployments list
npx wrangler tail --format json --search job_run        # watch cron: job_run_started / job_run_succeeded / job_tick_attached
curl -fsS https://<host>/healthz/ ; curl -fsS https://<host>/readyz/
npx wrangler delete                                     # teardown
```

Run the one-time operator migration against the proof database from a trusted
machine, never from Cloudflare:

```sh
docker run --rm -e SECRET_KEY=... -e DATABASE_URL=... -e PYTHAGORAS_ALLOW_MIGRATE=yes pythagoras-backend:release09 migrate
```

If any step fails, record it as a runtime-compatibility **blocker** with the exact
error; do not treat the design assumption as passed.

## Handoff to RELEASE-03

Extend, do not replace: swap the `job-probe` role for the bounded outbox command
(`JOB_BUDGET_SECONDS` already 240; stop dispatching when the remaining budget cannot
cover a Resend call plus shutdown margin), add `job-outbox` to `ROLES` in
`docker-entrypoint.sh` **and** `cloudflare/src/config.ts` together, add
`CSRF`/CORS/R2 vars, move the cron to the final environment-specific binding name,
and decide `migrate` orchestration (separate operator-triggered step before the
image/Worker rollout; new container starts never migrate).
