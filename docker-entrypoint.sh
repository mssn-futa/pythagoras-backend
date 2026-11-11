#!/bin/sh
# Fixed-role entrypoint for the Pythagoras backend image (RELEASE-09).
#
# Roles (an allow-list; nothing else is ever executed):
#   web        gunicorn serving pythagoras.wsgi on $PORT (default 8000)
#   job-probe  bounded, harmless database probe (python -m pythagoras.job_probe)
#   outbox-once  one bounded broadcast outbox pass (COMM-02):
#              `manage.py dawah_outbox --once --budget-seconds $OUTBOX_BUDGET_SECONDS`
#   migrate    OPERATOR ONLY: runs `manage.py migrate --noinput`; refuses unless
#              PYTHAGORAS_ALLOW_MIGRATE=yes. No other role ever migrates.
#
# The role comes from the single positional argument or, if absent, from
# PYTHAGORAS_ROLE (default: web). Extra arguments are rejected: this script is
# not a command passthrough. Environment values are never echoed.
set -eu

fail() { echo "entrypoint: $1" >&2; exit "${2:-64}"; }

if [ "$#" -gt 1 ]; then
  fail "exactly one role is accepted; arguments are not passed through"
fi
if [ "$#" -eq 1 ]; then
  role="$1"
elif [ "${PYTHAGORAS_ROLE+set}" = "set" ]; then
  role="$PYTHAGORAS_ROLE"
else
  role="web"
fi

cd /app

case "$role" in
  web)
    port="${PORT:-8000}"
    case "$port" in ''|*[!0-9]*) fail "PORT must be numeric" ;; esac
    workers="${WEB_CONCURRENCY:-2}"
    case "$workers" in ''|*[!0-9]*) fail "WEB_CONCURRENCY must be numeric" ;; esac
    # --forwarder-headers "" stops gunicorn from turning client headers named
    # SCRIPT_NAME / PATH_INFO into WSGI environ keys (default SCRIPT_NAME,PATH_INFO).
    # Bound every libpq connect so a black-holed database cannot outlast the
    # Worker's readiness budget (Django has no connect_timeout of its own here).
    export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
    # Gunicorn independently honours X-Forwarded-Proto from 127.0.0.1 by default.
    # Keep one policy: only when TRUST_CLOUDFLARE_PROXY is true (the Worker
    # strips client forwarding headers and sets its own) is it honoured.
    case "$(printf %s "${TRUST_CLOUDFLARE_PROXY:-false}" | tr 'A-Z' 'a-z')" in
      true|1) forwarded_ips="*" ;;
      *) forwarded_ips="" ;;
    esac
    echo "entrypoint: role=web port=$port workers=$workers"
    exec gunicorn pythagoras.wsgi:application \
      --bind "0.0.0.0:$port" \
      --forwarded-allow-ips "$forwarded_ips" \
      --forwarder-headers "" \
      --workers "$workers" --threads 4 \
      --timeout 60 --graceful-timeout 20 --keep-alive 5 \
      --access-logfile - --error-logfile -
    ;;
  job-probe)
    budget="${JOB_PROBE_BUDGET_SECONDS:-240}"
    case "$budget" in ''|*[!0-9]*|0) fail "JOB_PROBE_BUDGET_SECONDS must be a positive integer" ;; esac
    # libpq connect bound so a black-holed database cannot consume the budget.
    export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
    echo "entrypoint: role=job-probe budget_seconds=$budget"
    # The Python watchdog exits 124 at the budget; `timeout` is the outer backstop.
    exec timeout --kill-after=5 "$((budget + 10))" python -m pythagoras.job_probe
    ;;
  outbox-once)
    budget="${OUTBOX_BUDGET_SECONDS:-240}"
    case "$budget" in ''|*[!0-9]*|0) fail "OUTBOX_BUDGET_SECONDS must be a positive integer" ;; esac
    export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
    echo "entrypoint: role=outbox-once budget_seconds=$budget"
    # The command stops dispatching before the budget and exits; `timeout` is the
    # outer backstop (SIGTERM first, which the command handles gracefully).
    exec timeout --kill-after=10 "$((budget + 30))" \
      python manage.py dawah_outbox --once --budget-seconds "$budget"
    ;;
  migrate)
    if [ "${PYTHAGORAS_ALLOW_MIGRATE:-}" != "yes" ]; then
      fail "migrate role is operator-only; refusing without PYTHAGORAS_ALLOW_MIGRATE=yes" 78
    fi
    echo "entrypoint: role=migrate (operator opt-in)"
    exec python manage.py migrate --noinput
    ;;
  *)
    fail "unknown role (allowed: web, job-probe, outbox-once, migrate)" 64
    ;;
esac
