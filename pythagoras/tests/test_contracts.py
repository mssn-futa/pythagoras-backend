from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone as django_timezone
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, RefResolver
from rest_framework.test import APIClient

from accounts.models import CustomUser
from pythagoras.permissions import IsAdmin, IsAdminOrReadOnly, IsOwnerOrAdmin, IsStudent
from pythagoras.serializers import IdentifiedSerializer, UTCDateTimeField


@override_settings(ROOT_URLCONF='pythagoras.tests.urls')
class ResponseContractTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()

    def test_raw_and_pre_enveloped_success(self):
        for url, status, message in [('/raw/', 200, 'Request successful.'),
                                     ('/enveloped/', 201, 'Saved.')]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {'success': True, 'data': {'id': 1}, 'message': message})

    def test_empty_204_even_with_legacy_payload(self):
        response = self.client.delete('/raw/')
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b'')

    def test_field_and_nonfield_errors(self):
        response = self.client.get('/errors/validation/')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {
            'success': False,
            'data': {'code': 'VALIDATION_ERROR', 'fields': {'title': ['This field is required.']}},
            'message': 'Correct the highlighted fields.',
        })
        self.assertEqual(self.client.get('/errors/nonfield/').json()['data']['fields'],
                         {'non_field_errors': ['Invalid combination.']})

    def test_parse_and_serializer_errors(self):
        response = self.client.post('/raw/', '{broken', content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['data']['code'], 'MALFORMED_REQUEST')
        response = self.client.post('/raw/', {}, format='json')
        self.assertEqual(response.json()['data']['fields'], {'id': ['This field is required.']})

    def test_authentication_and_permission_errors(self):
        response = self.client.get('/protected/')
        self.assertEqual(response.status_code, 401)
        self.assertIn('Bearer', response['WWW-Authenticate'])
        response = self.client.get('/protected/', HTTP_AUTHORIZATION='Bearer invalid-token')
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json()['success'])
        self.assertNotIn('invalid-token', response.content.decode())
        response = self.client.get('/errors/permission/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['data']['code'], 'PERMISSION_DENIED')
        self.assertNotIn('private', response.content.decode())

    def test_missing_conflict_and_throttle(self):
        self.assertEqual(self.client.get('/errors/missing/').json()['data']['code'], 'NOT_FOUND')
        response = self.client.get('/errors/conflict/')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['data']['code'], 'QUIZ_IMMUTABLE')
        response = self.client.get('/errors/throttle/')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '12')
        self.assertEqual(response.json()['data']['code'], 'THROTTLED')

    def test_internal_errors_are_sanitized_in_debug_and_production(self):
        for debug in (True, False):
            with self.subTest(debug=debug), override_settings(DEBUG=debug):
                with self.assertLogs('pythagoras.api', level='ERROR') as captured:
                    response = self.client.get('/errors/internal/')
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json()['data'], {'code': 'INTERNAL_ERROR'})
                text = response.content.decode() + '\n'.join(captured.output)
                for forbidden in ['must-not-leak', 'RuntimeError', 'Traceback']:
                    self.assertNotIn(forbidden, text)
                self.assertIn(response['X-Request-ID'], captured.output[0])
                self.assertIsNone(captured.records[0].exc_info)

    def test_provider_and_direct_error_sanitization(self):
        with self.assertLogs('pythagoras.api', level='ERROR'):
            response = self.client.get('/errors/provider/')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['data']['code'], 'PROVIDER_UNAVAILABLE')
        self.assertNotIn('provider-key', response.content.decode())
        response = self.client.get('/errors/direct/')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['data']['code'], 'INTERNAL_ERROR')
        self.assertNotIn('private', response.content.decode())

    def test_request_ids_are_generated_not_trusted(self):
        first = self.client.get('/raw/', HTTP_X_REQUEST_ID='untrusted\nsecret')
        second = self.client.get('/raw/')
        self.assertRegex(first['X-Request-ID'], r'^[a-f0-9]{32}$')
        self.assertNotEqual(first['X-Request-ID'], second['X-Request-ID'])

    def test_schema_matches_success_error_and_empty_wire_bodies(self):
        schema = SchemaGenerator(patterns=__import__('pythagoras.tests.urls', fromlist=['urlpatterns']).urlpatterns).get_schema(public=True)
        cases = [('get', '/raw/', '200', self.client.get('/raw/').json()),
                 ('get', '/errors/{kind}/', '400', self.client.get('/errors/validation/').json())]
        for method, path, status, body in cases:
            wire_schema = schema['paths'][path][method]['responses'][status]['content']['application/json']['schema']
            Draft4Validator(wire_schema, resolver=RefResolver.from_schema(schema)).validate(body)
        self.assertNotIn('content', schema['paths']['/raw/']['delete']['responses']['204'])
        request_schema = schema['paths']['/raw/']['post']['requestBody']['content']['application/json']['schema']
        self.assertNotIn('success', request_schema)


@override_settings(ROOT_URLCONF='pythagoras.tests.urls')
class PaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        CustomUser.objects.bulk_create([
            CustomUser(email=f'contract-{index}@example.test', first_name='Same') for index in range(105)
        ])

    def test_default_page_size_and_id_tiebreaker(self):
        response = self.client.get('/items/')
        data = response.json()['data']
        ids = list(CustomUser.objects.order_by('id').values_list('id', flat=True))
        self.assertEqual(data['count'], 105)
        self.assertEqual([row['id'] for row in data['results']], ids[:20])
        self.assertIsNone(data['previous'])
        second = self.client.get('/items/?page=2').json()['data']
        self.assertEqual([row['id'] for row in second['results']], ids[20:40])

    def test_maximum_page_size_and_invalid_page(self):
        self.assertEqual(len(self.client.get('/items/?page_size=999').json()['data']['results']), 100)
        self.assertEqual(self.client.get('/items/?page=999').status_code, 404)

    def test_paginated_schema_matches_wire(self):
        from pythagoras.tests.urls import urlpatterns
        schema = SchemaGenerator(patterns=urlpatterns).get_schema(public=True)
        shape = schema['paths']['/items/']['get']['responses']['200']['content']['application/json']['schema']
        # OpenAPI 3.0 nullable must be translated for JSON Schema validation.
        wire = self.client.get('/items/?page=2').json()
        Draft4Validator(shape, resolver=RefResolver.from_schema(schema)).validate(wire)


class PermissionAndFieldTests(SimpleTestCase):
    def user(self, role='student', active=True, staff=False):
        return SimpleNamespace(pk=1, role=role, is_active=active, is_staff=staff, is_authenticated=True)

    def test_admin_uses_active_product_role_not_staff(self):
        permission = IsAdmin()
        for user, expected in [(self.user('admin'), True), (self.user(staff=True), False),
                               (self.user('admin', active=False), False)]:
            self.assertEqual(permission.has_permission(SimpleNamespace(user=user), None), expected)

    def test_student_readonly_and_ownership_boundaries(self):
        request = SimpleNamespace(user=self.user(), method='GET')
        self.assertTrue(IsStudent().has_permission(request, None))
        self.assertTrue(IsAdminOrReadOnly().has_permission(request, None))
        request.method = 'POST'
        self.assertFalse(IsAdminOrReadOnly().has_permission(request, None))
        owner = IsOwnerOrAdmin()
        self.assertTrue(owner.has_object_permission(request, None, SimpleNamespace(user_id=1)))
        self.assertFalse(owner.has_object_permission(request, None, SimpleNamespace(user_id=2)))
        request.user.is_active = False
        self.assertFalse(owner.has_object_permission(request, None, SimpleNamespace(user_id=1)))
        self.assertFalse(IsStudent().has_permission(request, None))

    def test_permission_compatibility_imports(self):
        from accounts.permissions import IsAdmin as LegacyIsAdmin
        self.assertIs(LegacyIsAdmin, IsAdmin)

    def test_numeric_ids_and_utc_timestamp_even_with_active_timezone(self):
        self.assertEqual(IdentifiedSerializer({'id': 7}).data, {'id': 7})
        with django_timezone.override(timezone(timedelta(hours=1))):
            value = UTCDateTimeField().to_representation(datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=1))))
        self.assertEqual(value, '2026-01-01T11:00:00Z')
