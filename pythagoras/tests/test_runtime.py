import json
import os
import subprocess
import sys
import tempfile

from django.conf import settings
from django.test import SimpleTestCase, override_settings


class ColdRuntimeTests(SimpleTestCase):
    def run_process(self, code, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'DJANGO_SETTINGS_MODULE': 'pythagoras.settings',
                   'PYTHON_DOTENV_DISABLED': '1', 'DEBUG': 'true',
                   'SECRET_KEY': 'test-only-cold-start-key-with-sufficient-length-1234567890',
                   'DATABASE_URL': f'sqlite:///{directory}/db.sqlite3', **overrides}
            return subprocess.run([sys.executable, '-c', code], cwd=settings.BASE_DIR,
                                  env=env, text=True, capture_output=True, timeout=30)

    def test_effective_classes_in_a_fresh_process_before_url_import(self):
        result = self.run_process("""
import django
django.setup()
from rest_framework.settings import api_settings
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication
from pythagoras.utils import StandardizedJSONRenderer, exception_handler
from pythagoras.schema import EnvelopeAutoSchema
from pythagoras.pagination import StandardPagination
assert api_settings.DEFAULT_RENDERER_CLASSES == [StandardizedJSONRenderer]
assert APIView.renderer_classes == [StandardizedJSONRenderer]
assert api_settings.DEFAULT_AUTHENTICATION_CLASSES == [JWTAuthentication]
assert api_settings.DEFAULT_SCHEMA_CLASS is EnvelopeAutoSchema
assert api_settings.EXCEPTION_HANDLER is exception_handler
assert api_settings.DEFAULT_PAGINATION_CLASS is StandardPagination
import pythagoras.wsgi, pythagoras.asgi
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_secret_is_required_and_never_echoed(self):
        result = self.run_process('import pythagoras.settings', SECRET_KEY='')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SECRET_KEY must be configured', result.stderr)

    def test_environment_consumption(self):
        result = self.run_process("""
import json
from django.conf import settings as s
assert s.SECRET_KEY == 'test-only-cold-start-key-with-sufficient-length-1234567890'
assert s.RESEND_API_KEY == 'fake-resend-sentinel'
assert s.R2_SECRET_ACCESS_KEY == 'fake-r2-sentinel'
print(json.dumps({
    'debug': s.DEBUG, 'hosts': s.ALLOWED_HOSTS, 'cors': s.CORS_ALLOWED_ORIGINS,
    'csrf': s.CSRF_TRUSTED_ORIGINS, 'proxy': s.SECURE_PROXY_SSL_HEADER,
    'forwarded_host': s.USE_X_FORWARDED_HOST, 'storage': s.STORAGES['staticfiles']['BACKEND'],
    'timeout': s.RESEND_TIMEOUT_SECONDS, 'sender': s.DEFAULT_FROM_EMAIL,
    'reply': s.EMAIL_REPLY_TO, 'private': s.R2_PRIVATE_BUCKET,
    'public': s.R2_PUBLIC_BUCKET, 'ttl': s.R2_PREVIEW_TTL_SECONDS,
    'cookies': [s.CSRF_COOKIE_SECURE, s.SESSION_COOKIE_SECURE],
}))
""", DEBUG='false', ALLOWED_HOSTS='api.example.test, localhost  127.0.0.1',
            CORS_ALLOWED_ORIGINS='https://app.example.test', CSRF_TRUSTED_ORIGINS='https://api.example.test',
            TRUST_CLOUDFLARE_PROXY='true', RESEND_API_KEY='fake-resend-sentinel',
            RESEND_TIMEOUT_SECONDS='4', DEFAULT_FROM_EMAIL='App <app@example.test>',
            EMAIL_REPLY_TO='help@example.test', R2_SECRET_ACCESS_KEY='fake-r2-sentinel',
            R2_PRIVATE_BUCKET='private-test', R2_PUBLIC_BUCKET='public-test', R2_PREVIEW_TTL_SECONDS='60')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data['debug'])
        self.assertEqual(data['hosts'], ['api.example.test', 'localhost', '127.0.0.1'])
        self.assertEqual(data['cors'], ['https://app.example.test'])
        self.assertEqual(data['csrf'], ['https://api.example.test'])
        self.assertEqual(data['proxy'], ['HTTP_X_FORWARDED_PROTO', 'https'])
        self.assertTrue(data['forwarded_host'])
        self.assertEqual(data['cookies'], [True, True])
        self.assertEqual(data['storage'], 'whitenoise.storage.CompressedManifestStaticFilesStorage')
        self.assertEqual((data['timeout'], data['ttl']), (4, 60))
        self.assertEqual((data['private'], data['public']), ('private-test', 'public-test'))
        self.assertEqual((data['sender'], data['reply']), ('App <app@example.test>', 'help@example.test'))

    def test_proxy_headers_are_untrusted_by_default(self):
        result = self.run_process("""
from django.test import RequestFactory
from django.conf import settings
request = RequestFactory().get('/', HTTP_X_FORWARDED_PROTO='https')
assert not request.is_secure()
assert settings.SECURE_PROXY_SSL_HEADER is None
assert not settings.USE_X_FORWARDED_HOST
""", TRUST_CLOUDFLARE_PROXY='false')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_configuration_fails_without_disclosing_value(self):
        for variable in ['DEBUG', 'RESEND_TIMEOUT_SECONDS', 'R2_PREVIEW_TTL_SECONDS']:
            result = self.run_process('import pythagoras.settings', **{variable: 'private-sentinel'})
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('private-sentinel', result.stderr)


class AdminAndOriginTests(SimpleTestCase):
    def test_admin_login_and_csrf_remain_html(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        response = client.get('/admin/login/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response['Content-Type'])
        self.assertContains(response, 'csrfmiddlewaretoken')
        self.assertEqual(client.post('/admin/login/', {'username': 'test', 'password': 'test'}).status_code, 403)

    @override_settings(CORS_ALLOWED_ORIGINS=['https://app.example.test'])
    def test_cors_allows_only_configured_origin_and_bearer_header(self):
        response = self.client.options('/accounts/login/', HTTP_ORIGIN='https://app.example.test',
                                       HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                                       HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization,content-type')
        self.assertEqual(response['Access-Control-Allow-Origin'], 'https://app.example.test')
        self.assertIn('authorization', response['Access-Control-Allow-Headers'])
        self.assertNotIn('Access-Control-Allow-Credentials', response)
        response = self.client.options('/accounts/login/', HTTP_ORIGIN='https://unknown.example.test',
                                       HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST')
        self.assertNotIn('Access-Control-Allow-Origin', response)

    def test_schema_endpoint_returns_a_document_not_an_envelope(self):
        response = self.client.get('/schema/', HTTP_ACCEPT='application/vnd.oai.openapi+json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('openapi', response.json())
        self.assertNotIn('success', response.json())
