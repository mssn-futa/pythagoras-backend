from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from pythagoras.mail import memory

User = get_user_model()
PASSWORD = 'Correct-Horse-Battery-9'


def registration_payload(**overrides):
    payload = {
        'email': 'student@example.test',
        'password': PASSWORD,
        'password_confirm': PASSWORD,
        'first_name': 'Amina',
        'last_name': 'Yusuf',
        'matric_number': 'CSC/21/001',
        'department': 'Computer Science',
        'level': '300',
    }
    payload.update(overrides)
    return payload


def make_user(email='student@example.test', password=PASSWORD, **fields):
    fields.setdefault('first_name', 'Amina')
    fields.setdefault('last_name', 'Yusuf')
    return User.objects.create_user(email=email, password=password, **fields)


class APITestMixin:
    def setUp(self):
        super().setUp()
        mail_settings = override_settings(DEFAULT_FROM_EMAIL='Pythagoras <noreply@example.test>',
                                          EMAIL_REPLY_TO='')
        mail_settings.enable()
        self.addCleanup(mail_settings.disable)
        cache.clear()  # throttle counters
        memory.reset()
        self.client = APIClient()

    def login(self, identifier='student@example.test', password=PASSWORD):
        return self.client.post('/accounts/login/', {'email_or_matric': identifier, 'password': password},
                                format='json')

    def tokens(self, identifier='student@example.test', password=PASSWORD):
        response = self.login(identifier, password)
        assert response.status_code == 200, response.content
        return response.json()['data']['tokens']

    def bearer(self, access):
        return {'HTTP_AUTHORIZATION': f'Bearer {access}'}
