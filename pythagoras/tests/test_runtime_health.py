import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

from django.conf import settings
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase


class HealthEndpointTests(TestCase):
    def test_healthz_is_envelope_free_and_uncached(self):
        response = self.client.get('/healthz/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})
        self.assertIn('no-store', response['Cache-Control'])

    def test_readyz_checks_database(self):
        response = self.client.get('/readyz/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_readyz_does_not_leak_failure_details(self):
        secret = 'postgresql://user:hunter2@db.internal/x'
        with mock.patch('pythagoras.health.connections') as connections:
            connections.__getitem__.return_value.cursor.side_effect = OperationalError(secret)
            response = self.client.get('/readyz/')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})
        self.assertNotIn('hunter2', response.content.decode())

    def test_only_safe_methods(self):
        self.assertEqual(self.client.post('/healthz/').status_code, 405)
        self.assertEqual(self.client.head('/healthz/').status_code, 200)


class JobProbeProcessTests(SimpleTestCase):
    def run_probe(self, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'PYTHON_DOTENV_DISABLED': '1', 'DEBUG': 'true',
                   'SECRET_KEY': 'test-only-job-probe-key-with-sufficient-length-1234567890',
                   'DATABASE_URL': f'sqlite:///{directory}/db.sqlite3',
                   'STATIC_ROOT': f'{directory}/static', **overrides}
            return subprocess.run([sys.executable, '-m', 'pythagoras.job_probe'],
                                  cwd=settings.BASE_DIR, env=env, text=True,
                                  capture_output=True, timeout=60)

    def test_success_reports_only_coarse_facts(self):
        result = self.run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(report['ok'])
        self.assertNotIn('test-only-job-probe-key', result.stdout + result.stderr)

    def test_unreachable_database_fails_without_leaking_dsn(self):
        result = self.run_probe(
            DATABASE_URL='postgresql://probe:hunter2@127.0.0.1:1/none',
            PGCONNECT_TIMEOUT='3')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('hunter2', result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout.strip().splitlines()[-1])['ok'])

    def test_postgres_without_verified_tls_fails_unless_explicitly_allowed(self):
        url = os.environ.get('DATABASE_URL', '')
        if not url.startswith('postgres'):
            self.skipTest('requires the PostgreSQL test database')
        denied = self.run_probe(DATABASE_URL=url, JOB_PROBE_ALLOW_INSECURE_DB='no')
        self.assertEqual(denied.returncode, 1, denied.stdout)
        self.assertIn(json.loads(denied.stdout.strip().splitlines()[-1])['error'],
                      {'tls_required', 'sslmode_must_be_verify_full'})
        allowed = self.run_probe(DATABASE_URL=url, JOB_PROBE_ALLOW_INSECURE_DB='yes')
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)

    def test_invalid_budget_is_rejected(self):
        result = self.run_probe(JOB_PROBE_BUDGET_SECONDS='0')
        self.assertNotEqual(result.returncode, 0)
