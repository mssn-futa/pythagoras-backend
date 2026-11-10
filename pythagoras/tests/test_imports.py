import os
import subprocess
import sys
import tempfile

from django.conf import settings
from django.test import SimpleTestCase


class ColdProcessTests(SimpleTestCase):
    def run_process(self, code, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'DJANGO_SETTINGS_MODULE': 'pythagoras.settings',
                   'PYTHON_DOTENV_DISABLED': '1', 'DEBUG': 'true',
                   'SECRET_KEY': 'test-only-cold-start-key-with-sufficient-length-1234567890',
                   'DATABASE_URL': f'sqlite:///{directory}/db.sqlite3', **overrides}
            return subprocess.run([sys.executable, '-c', code], cwd=settings.BASE_DIR,
                                  env=env, text=True, capture_output=True, timeout=60)

    def test_shared_modules_import_before_drf_views(self):
        for module in ('pythagoras.utils', 'accounts.tokens', 'accounts.authentication',
                       'accounts.views', 'pythagoras.mail'):
            with self.subTest(module=module):
                result = self.run_process(f"""
import django
django.setup()
import {module}
import rest_framework.views
from rest_framework.settings import api_settings
from pythagoras.utils import StandardizedJSONRenderer
assert api_settings.DEFAULT_RENDERER_CLASSES == [StandardizedJSONRenderer]
""")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_email_settings_validation(self):
        ok = self.run_process("""
from django.conf import settings as s
print(s.WELCOME_EMAIL_TIMEOUT_SECONDS, s.EMAIL_IDEMPOTENCY_NAMESPACE)
""", RESEND_TIMEOUT_SECONDS='4')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout.split(), ['4', 'local'])
        for overrides, text in [
            ({'RESEND_TIMEOUT_SECONDS': '5', 'WELCOME_EMAIL_TIMEOUT_SECONDS': '6'},
             'WELCOME_EMAIL_TIMEOUT_SECONDS must not exceed'),
            ({'EMAIL_IDEMPOTENCY_NAMESPACE': 'prod/x'}, 'EMAIL_IDEMPOTENCY_NAMESPACE must be'),
            ({'EMAIL_IDEMPOTENCY_NAMESPACE': ''}, 'EMAIL_IDEMPOTENCY_NAMESPACE must be'),
        ]:
            with self.subTest(overrides=overrides):
                result = self.run_process('import pythagoras.settings', **overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(text, result.stderr)
        removed = self.run_process("""
from django.conf import settings as s
assert not hasattr(s, 'RESEND_REQUESTS_PER_SECOND')
assert not hasattr(s, 'RESEND_DAILY_REQUEST_BUDGET')
""")
        self.assertEqual(removed.returncode, 0, removed.stderr)
