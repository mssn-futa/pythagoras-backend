"""Fixed, harmless scheduled-job probe (RELEASE-09).

Run as ``python -m pythagoras.job_probe``. It takes no arguments. It runs
Django's database-aware system check, opens the default database connection,
executes ``SELECT 1`` and reports the server major version and whether the
session is TLS-protected, then exits.

Exit codes: 0 success, 1 failure, 124 wall-clock budget exceeded.

For PostgreSQL it also fails unless the session is TLS-protected AND the
configured ``sslmode`` is ``verify-full``; set JOB_PROBE_ALLOW_INSECURE_DB=yes
only for local/CI databases.

Only coarse, non-secret facts are printed (never the DSN, host, user, or
exception messages, which can embed connection strings).
"""
import json
import os
import sys
import threading
import time

DEFAULT_BUDGET_SECONDS = 240
EXIT_FAILURE = 1
EXIT_TIMEOUT = 124


class ProbePolicyError(Exception):
    """Fixed, non-secret failure reason (the message is safe to print)."""


def budget_seconds(environ=os.environ):
    raw = environ.get('JOB_PROBE_BUDGET_SECONDS', str(DEFAULT_BUDGET_SECONDS))
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise SystemExit('JOB_PROBE_BUDGET_SECONDS must be a positive integer.')
    return value


def _emit(**fields):
    print(json.dumps({'probe': 'job-probe', **fields}, sort_keys=True), flush=True)


def _watchdog(seconds):
    """Hard stop even while blocked inside libpq (a signal handler would not run)."""
    def expire():
        _emit(ok=False, error='budget_exceeded', budget_seconds=seconds)
        os._exit(EXIT_TIMEOUT)

    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    timer.start()
    return timer


def run():
    budget = budget_seconds()
    started = time.monotonic()
    watchdog = _watchdog(budget)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pythagoras.settings')
    try:
        import django
        from django.core.management import call_command
        from django.db import connection

        django.setup()
        call_command('check', database=['default'], verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            assert cursor.fetchone() == (1,)
            vendor = connection.vendor
            fields = {'vendor': vendor, 'tls': None, 'server_major': None}
            if vendor == 'postgresql':
                fields['server_major'] = connection.pg_version // 10000
                cursor.execute('SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()')
                row = cursor.fetchone()
                fields['tls'] = bool(row[0]) if row else False
                options = connection.settings_dict.get('OPTIONS') or {}
                fields['sslmode'] = options.get('sslmode') or 'default'
                if os.environ.get('JOB_PROBE_ALLOW_INSECURE_DB') != 'yes':
                    # Production proof must use verified TLS. The opt-out is for local/CI only.
                    if not fields['tls']:
                        raise ProbePolicyError('tls_required')
                    if fields['sslmode'] != 'verify-full':
                        raise ProbePolicyError('sslmode_must_be_verify_full')
        connection.close()
    except ProbePolicyError as exc:
        watchdog.cancel()
        _emit(ok=False, error=str(exc))
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - class name only
        watchdog.cancel()
        _emit(ok=False, error=type(exc).__name__)
        return EXIT_FAILURE
    watchdog.cancel()
    _emit(ok=True, elapsed_ms=int((time.monotonic() - started) * 1000), **fields)
    return 0


if __name__ == '__main__':
    sys.exit(run())
