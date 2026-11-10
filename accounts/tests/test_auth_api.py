from datetime import timedelta
from unittest import mock

from django.contrib.auth.hashers import identify_hasher
from django.core.cache import cache
from django.test import TestCase
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.serializers import UserRegistrationSerializer
from .helpers import PASSWORD, APITestMixin, User, make_user, registration_payload

USER_KEYS = {'id', 'email', 'first_name', 'last_name', 'matric_number', 'department', 'level',
             'role', 'is_active'}


class RegistrationTests(APITestMixin, TestCase):
    def register(self, **overrides):
        return self.client.post('/accounts/register/', registration_payload(**overrides), format='json')

    def test_creates_active_student_with_hashed_password_and_normalized_identity(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.register(email='  Student@Example.TEST ', matric_number=' csc/21/001 ',
                                     department='  Computer Science ')
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body['message'], 'Account created.')
        self.assertTrue(body['success'])
        self.assertEqual(set(body['data']), USER_KEYS)
        user = User.objects.get()
        self.assertEqual((user.email, user.matric_number, user.department),
                         ('student@example.test', 'CSC/21/001', 'Computer Science'))
        self.assertEqual((user.role, user.is_active, user.is_staff, user.is_superuser),
                         ('student', True, False, False))
        self.assertNotEqual(user.password, PASSWORD)
        identify_hasher(user.password)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertNotIn(PASSWORD, response.content.decode())

    def test_optional_fields_may_be_unset_and_blank_matric_is_null(self):
        first = self.register(matric_number='', department='', level='')
        second = self.register(email='other@example.test', matric_number='   ')
        third = self.client.post('/accounts/register/', {
            k: v for k, v in registration_payload(email='third@example.test').items()
            if k not in ('matric_number', 'department', 'level')}, format='json')
        for response in (first, second, third):
            self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(User.objects.filter(matric_number__isnull=True).count(), 3)

    def test_privileged_input_is_rejected_not_ignored(self):
        for field, value in [('role', 'admin'), ('is_active', False), ('is_staff', True),
                             ('is_superuser', True), ('groups', [1]), ('user_permissions', [1]),
                             ('id', 99), ('date_joined', '2020-01-01T00:00:00Z')]:
            with self.subTest(field=field):
                cache.clear()
                response = self.register(**{field: value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['data']['code'], 'VALIDATION_ERROR')
                self.assertIn(field, response.json()['data']['fields'])
        self.assertFalse(User.objects.exists())

    def test_required_fields_password_rules_and_confirmation(self):
        response = self.client.post('/accounts/register/', {}, format='json')
        self.assertEqual(set(response.json()['data']['fields']),
                         {'email', 'password', 'password_confirm', 'first_name', 'last_name'})
        response = self.register(password_confirm='Different-Password-1')
        self.assertIn('password_confirm', response.json()['data']['fields'])
        response = self.register(password='123', password_confirm='123')
        self.assertIn('password', response.json()['data']['fields'])
        response = self.register(level='600')
        self.assertIn('level', response.json()['data']['fields'])
        self.assertFalse(User.objects.exists())

    def test_case_variant_email_and_matric_collisions_are_field_errors(self):
        self.assertEqual(self.register().status_code, 201)
        response = self.register(email='STUDENT@example.test', matric_number='NEW/1')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['data']['fields'],
                         {'email': ['An account with this email already exists.']})
        response = self.register(email='new@example.test', matric_number='csc/21/001')
        self.assertEqual(response.json()['data']['fields'],
                         {'matric_number': ['An account with this matric number already exists.']})
        self.assertEqual(User.objects.count(), 1)

    def test_concurrent_collision_becomes_validation_error(self):
        serializer = UserRegistrationSerializer(data=registration_payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        make_user(email='STUDENT@EXAMPLE.TEST', matric_number='other/1')  # wins the race
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError) as caught:
            serializer.save()
        self.assertIn('email', caught.exception.detail)
        self.assertEqual(User.objects.count(), 1)


class LoginTests(APITestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user(matric_number='CSC/21/001', department='Computer Science', level='300')

    def test_login_by_email_or_matric_returns_d1_shape(self):
        for identifier in ('student@example.test', ' STUDENT@Example.test ', 'CSC/21/001', 'csc/21/001'):
            with self.subTest(identifier=identifier):
                response = self.login(identifier)
                self.assertEqual(response.status_code, 200, response.content)
                body = response.json()
                self.assertEqual(body['message'], 'Signed in.')
                self.assertEqual(set(body['data']), {'user', 'tokens'})
                self.assertEqual(set(body['data']['tokens']), {'access', 'refresh'})
                self.assertEqual(body['data']['user'], {
                    'id': self.user.pk, 'email': 'student@example.test', 'first_name': 'Amina',
                    'last_name': 'Yusuf', 'matric_number': 'CSC/21/001',
                    'department': 'Computer Science', 'level': '300', 'role': 'student',
                    'is_active': True})
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login)

    def test_token_lifetimes(self):
        tokens = self.tokens()
        refresh = RefreshToken(tokens['refresh'])
        self.assertEqual(refresh['exp'] - refresh['iat'], int(timedelta(days=7).total_seconds()))
        access = refresh.access_token.__class__(tokens['access'])
        self.assertEqual(access['exp'] - access['iat'], int(timedelta(minutes=60).total_seconds()))

    def test_bad_credentials_are_generic(self):
        wrong_password = self.login(password='wrong-password')
        unknown_email = self.login('nobody@example.test')
        unknown_matric = self.login('NOPE/1')
        for response in (wrong_password, unknown_email, unknown_matric):
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {
                'success': False, 'data': {'code': 'INVALID_CREDENTIALS'},
                'message': 'Invalid email/matric number or password.'})
        self.assertEqual(wrong_password.content, unknown_email.content)

    def test_missing_fields_are_validation_errors(self):
        response = self.client.post('/accounts/login/', {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()['data']['fields']), {'email_or_matric', 'password'})

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save()
        response = self.login()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['data']['code'], 'ACCOUNT_INACTIVE')
        self.assertNotIn('tokens', response.json()['data'])
        # Wrong password for an inactive account stays generic.
        self.assertEqual(self.login(password='wrong').json()['data']['code'], 'INVALID_CREDENTIALS')

    def test_stale_bearer_header_does_not_block_login(self):
        response = self.client.post('/accounts/login/', {'email_or_matric': 'CSC/21/001', 'password': PASSWORD},
                                    format='json', HTTP_AUTHORIZATION='Bearer expired-or-garbage')
        self.assertEqual(response.status_code, 200)


class SessionLifecycleTests(APITestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user(matric_number='CSC/21/001')

    def refresh(self, token):
        return self.client.post('/accounts/refresh/', {'refresh': token}, format='json')

    def logout(self, token):
        return self.client.post('/accounts/logout/', {'refresh': token}, format='json')

    def test_refresh_rotates_and_old_refresh_is_rejected(self):
        tokens = self.tokens()
        response = self.refresh(tokens['refresh'])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['message'], 'Session refreshed.')
        data = response.json()['data']
        self.assertEqual(set(data), {'access', 'refresh'})
        self.assertNotEqual(data['refresh'], tokens['refresh'])
        self.assertTrue(BlacklistedToken.objects.filter(
            token__jti=RefreshToken(data['refresh'], verify=False)['jti']).count() == 0)
        reused = self.refresh(tokens['refresh'])
        self.assertEqual(reused.status_code, 401)
        self.assertEqual(reused.json()['data']['code'], 'TOKEN_INVALID')
        # The rotated token works once, and its access token authenticates.
        self.assertEqual(self.client.get('/accounts/me/', **self.bearer(data['access'])).status_code, 200)
        self.assertEqual(self.refresh(data['refresh']).status_code, 200)

    def test_rotation_is_single_use_even_if_blacklist_check_was_passed(self):
        tokens = self.tokens()
        from accounts import tokens as token_service
        self.assertEqual(self.refresh(tokens['refresh']).status_code, 200)
        # Simulate a concurrent request that validated before the first one committed.
        with mock.patch.object(token_service.RefreshToken, 'check_blacklist', lambda self: None):
            self.assertEqual(self.refresh(tokens['refresh']).status_code, 401)

    def test_access_token_is_not_a_refresh_token(self):
        tokens = self.tokens()
        self.assertEqual(self.refresh(tokens['access']).status_code, 401)
        self.assertEqual(self.refresh('garbage').status_code, 401)
        self.assertEqual(self.refresh('').status_code, 400)

    def test_logout_blacklists_and_replay_is_idempotent(self):
        tokens = self.tokens()
        response = self.logout(tokens['refresh'])
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b'')
        jti = RefreshToken(tokens['refresh'], verify=False)['jti']
        self.assertTrue(BlacklistedToken.objects.filter(token__jti=jti).exists())
        self.assertEqual(self.refresh(tokens['refresh']).status_code, 401)
        replay = self.logout(tokens['refresh'])
        self.assertEqual(replay.status_code, 204)
        self.assertEqual(replay.content, b'')
        # Access tokens stay valid until expiry (D0).
        self.assertEqual(self.client.get('/accounts/me/', **self.bearer(tokens['access'])).status_code, 200)

    def test_logout_of_rotated_and_expired_tokens_is_204_but_unknown_is_401(self):
        tokens = self.tokens()
        self.refresh(tokens['refresh'])
        self.assertEqual(self.logout(tokens['refresh']).status_code, 204)
        outstanding = OutstandingToken.objects.get(token=self.tokens()['refresh'])
        with mock.patch('rest_framework_simplejwt.tokens.Token.check_exp',
                        side_effect=__import__('rest_framework_simplejwt.exceptions',
                                               fromlist=['TokenError']).TokenError('expired')):
            self.assertEqual(self.logout(outstanding.token).status_code, 204)
        self.assertTrue(BlacklistedToken.objects.filter(token=outstanding).exists())
        forged = tokens['refresh'][:-4] + ('AAAA' if not tokens['refresh'].endswith('AAAA') else 'BBBB')
        for value in (forged, tokens['access'], 'not-a-token'):
            with self.subTest(value=value[:10]):
                response = self.logout(value)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()['data']['code'], 'TOKEN_INVALID')

    def test_inactive_user_blocked_on_refresh_me_and_protected_endpoints(self):
        tokens = self.tokens()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        response = self.refresh(tokens['refresh'])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['data']['code'], 'ACCOUNT_INACTIVE')
        self.assertNotIn('access', response.json()['data'])
        for method in ('get', 'patch'):
            response = getattr(self.client, method)('/accounts/me/', {}, format='json',
                                                     **self.bearer(tokens['access']))
            self.assertEqual(response.status_code, 403, response.content)
            self.assertEqual(response.json()['data']['code'], 'ACCOUNT_INACTIVE')
        # Another domain's protected endpoint uses the same shared authentication.
        response = self.client.get('/quiz/submissions/', **self.bearer(tokens['access']))
        self.assertEqual(response.json()['data']['code'], 'ACCOUNT_INACTIVE')
        self.assertEqual(response.status_code, 403)

    def test_full_journey(self):
        with self.captureOnCommitCallbacks(execute=True):
            created = self.client.post('/accounts/register/', registration_payload(
                email='new@example.test', matric_number='NEW/22/002'), format='json')
        self.assertEqual(created.status_code, 201)
        tokens = self.tokens('NEW/22/002')
        rotated = self.refresh(tokens['refresh']).json()['data']
        me = self.client.patch('/accounts/me/', {'level': '400'}, format='json', **self.bearer(rotated['access']))
        self.assertEqual(me.json()['data']['level'], '400')
        self.assertEqual(self.logout(rotated['refresh']).status_code, 204)
        self.assertEqual(self.refresh(rotated['refresh']).status_code, 401)


class CurrentUserTests(APITestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user(matric_number='CSC/21/001', department='Computer Science', level='300')
        self.other = make_user(email='other@example.test', first_name='Other')
        self.auth = self.bearer(self.tokens()['access'])

    def test_requires_authentication(self):
        response = self.client.get('/accounts/me/')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['data']['code'], 'AUTHENTICATION_REQUIRED')

    def test_get_returns_own_representation(self):
        response = self.client.get('/accounts/me/', **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['id'], self.user.pk)
        self.assertEqual(set(response.json()['data']), USER_KEYS)

    def test_patch_permitted_fields_only_affects_self(self):
        response = self.client.patch('/accounts/me/', {
            'first_name': ' Aminat ', 'last_name': 'Bello', 'department': ' Physics ', 'level': '400'},
            format='json', **self.auth)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['message'], 'Profile updated.')
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name, self.user.department, self.user.level),
                         ('Aminat', 'Bello', 'Physics', '400'))
        self.other.refresh_from_db()
        self.assertEqual(self.other.first_name, 'Other')

    def test_patch_rejects_protected_fields_without_applying_anything(self):
        for field, value in [('email', 'new@example.test'), ('matric_number', 'X/1'), ('role', 'admin'),
                             ('is_active', False), ('is_staff', True), ('is_superuser', True),
                             ('password', 'New-Password-123'), ('id', self.other.pk), ('groups', [])]:
            with self.subTest(field=field):
                response = self.client.patch('/accounts/me/', {field: value, 'first_name': 'Changed'},
                                             format='json', **self.auth)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['data']['fields'],
                                 {field: ['This field cannot be set or changed here.']})
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.email, self.user.role, self.user.is_active),
                         ('Amina', 'student@example.test', 'student', True))

    def test_patch_validates_level_and_names(self):
        for payload in ({'level': '600'}, {'first_name': ''}, {'last_name': '  '}):
            with self.subTest(payload=payload):
                response = self.client.patch('/accounts/me/', payload, format='json', **self.auth)
                self.assertEqual(response.status_code, 400)

    def test_admin_also_cannot_self_edit_protected_fields(self):
        admin = make_user(email='admin@example.test', role='admin')
        auth = self.bearer(self.tokens('admin@example.test')['access'])
        response = self.client.patch('/accounts/me/', {'role': 'student'}, format='json', **auth)
        self.assertEqual(response.status_code, 400)
        admin.refresh_from_db()
        self.assertEqual(admin.role, 'admin')


class ThrottleTests(APITestMixin, TestCase):
    def assert_limit(self, url, limit):
        for _ in range(limit):
            self.assertNotEqual(self.client.post(url, {}, format='json').status_code, 429)
        response = self.client.post(url, {}, format='json')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['data']['code'], 'THROTTLED')
        self.assertTrue(int(response['Retry-After']) > 0)

    def test_registration_5_per_minute(self):
        self.assert_limit('/accounts/register/', 5)

    def test_login_10_per_minute(self):
        self.assert_limit('/accounts/login/', 10)

    def test_refresh_10_per_minute(self):
        self.assert_limit('/accounts/refresh/', 10)

    def test_limits_are_per_ip(self):
        for _ in range(5):
            self.client.post('/accounts/register/', {}, format='json')
        self.assertEqual(self.client.post('/accounts/register/', {}, format='json').status_code, 429)
        other = self.client.post('/accounts/register/', {}, format='json', REMOTE_ADDR='10.0.0.2')
        self.assertEqual(other.status_code, 400)

    def test_client_forwarded_for_cannot_evade_limit_by_default(self):
        for i in range(5):
            self.client.post('/accounts/register/', {}, format='json', HTTP_X_FORWARDED_FOR=f'10.1.0.{i}')
        response = self.client.post('/accounts/register/', {}, format='json', HTTP_X_FORWARDED_FOR='10.9.9.9')
        self.assertEqual(response.status_code, 429)
