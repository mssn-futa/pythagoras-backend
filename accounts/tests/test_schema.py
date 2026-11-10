from django.test import TestCase
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, RefResolver

from .helpers import APITestMixin, make_user, registration_payload


class AuthSchemaTests(APITestMixin, TestCase):
    """The published schema and its examples describe the real enveloped wire payloads."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(public=True)

    def validate(self, path, method, status, body):
        content = self.schema['paths'][path][method]['responses'][status]['content']['application/json']
        validator = Draft4Validator(content['schema'], resolver=RefResolver.from_schema(self.schema))
        validator.validate(body)
        return content

    def test_real_responses_and_examples_match_schema(self):
        with self.captureOnCommitCallbacks(execute=True):
            created = self.client.post('/accounts/register/', registration_payload(), format='json')
        login = self.login('CSC/21/001')
        tokens = login.json()['data']['tokens']
        refreshed = self.client.post('/accounts/refresh/', {'refresh': tokens['refresh']}, format='json')
        me = self.client.get('/accounts/me/', **self.bearer(refreshed.json()['data']['access']))
        bad = self.login(password='wrong')
        cases = [('/accounts/register/', 'post', '201', created), ('/accounts/login/', 'post', '200', login),
                 ('/accounts/refresh/', 'post', '200', refreshed), ('/accounts/me/', 'get', '200', me),
                 ('/accounts/login/', 'post', '401', bad)]
        for path, method, status, response in cases:
            with self.subTest(path=path, status=status):
                self.assertEqual(str(response.status_code), status)
                content = self.validate(path, method, status, response.json())
                for example in content.get('examples', {}).values():
                    self.validate(path, method, status, example['value'])
        login_example = self.schema['paths']['/accounts/login/']['post']['responses']['200'][
            'content']['application/json']['examples']['SignedIn']['value']
        self.assertEqual(set(login_example['data']), set(login.json()['data']))
        self.assertEqual(set(login_example['data']['user']), set(login.json()['data']['user']))
        self.assertNotIn('content', self.schema['paths']['/accounts/logout/']['post']['responses']['204'])
        self.assertEqual(self.schema['paths']['/accounts/me/']['get']['security'], [{'jwtAuth': []}])
