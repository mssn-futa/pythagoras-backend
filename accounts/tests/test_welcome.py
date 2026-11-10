from unittest import mock

from django.test import TestCase, override_settings

from accounts.services import EmailService, welcome_idempotency_key
from pythagoras.mail import SendResult, memory
from .helpers import APITestMixin, User, make_user, registration_payload


class WelcomeEmailTests(APITestMixin, TestCase):
    def register(self, **overrides):
        return self.client.post('/accounts/register/', registration_payload(**overrides), format='json')

    @override_settings(EMAIL_REPLY_TO='help@example.test')
    def test_sent_after_commit_with_stable_key_and_existing_templates(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.register()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(memory.outbox, [])  # nothing before commit
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        user = User.objects.get()
        [sent] = memory.outbox
        self.assertEqual(sent.idempotency_key, f'local/welcome/{user.pk}/v1')
        self.assertEqual(sent.idempotency_key, welcome_idempotency_key(user.pk))
        self.assertEqual(sent.to, 'student@example.test')
        self.assertEqual(sent.from_email, 'Pythagoras <noreply@example.test>')
        self.assertEqual(sent.reply_to, 'help@example.test')
        self.assertEqual(sent.subject, 'Welcome to Pythagoras! 🎓')
        self.assertIn('Assalaamu Alaykum, Amina!', sent.html)
        self.assertIn('<!DOCTYPE html>', sent.html)
        self.assertIn('Matric Number: CSC/21/001', sent.text)
        self.assertIn('Level: 300 Level', sent.text)
        self.assertNotIn('<p>', sent.text)

    def test_response_never_claims_delivery(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.register()
        self.assertNotIn('email', response.json()['message'].lower())
        self.assertNotIn('sent', response.json()['message'].lower())

    def test_transport_failure_timeout_or_exception_keeps_account(self):
        outcomes = [SendResult.uncertain('TIMEOUT'), SendResult.failed('VALIDATION_ERROR'),
                    SendResult.retryable('RATE_LIMIT_EXCEEDED', 2), RuntimeError('boom')]
        for index, outcome in enumerate(outcomes):
            with self.subTest(outcome=outcome), mock.patch(
                    'accounts.services.send_email',
                    **({'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome})):
                with self.assertLogs('accounts.email', level='WARNING'):
                    with self.captureOnCommitCallbacks(execute=True):
                        response = self.register(email=f's{index}@example.test', matric_number=f'M/{index}')
                self.assertEqual(response.status_code, 201)
                self.assertTrue(User.objects.filter(email=f's{index}@example.test').exists())
        self.assertEqual(User.objects.count(), len(outcomes))

    def test_missing_sender_is_logged_failure_not_error(self):
        user = make_user()
        with override_settings(DEFAULT_FROM_EMAIL=''), self.assertLogs('accounts.email', 'WARNING') as logs:
            result = EmailService.send_welcome_email(user)
        self.assertEqual(result, SendResult.failed('INVALID_MESSAGE'))
        self.assertNotIn('student@example.test', '\n'.join(logs.output))

    def test_resend_transport_receives_namespaced_key_and_welcome_bound(self):
        user = make_user()
        seen = {}

        def fake_send(transport, msg, deadline):
            seen['timeout'] = transport._timeout
            return SendResult.accepted('provider-id-1')

        with override_settings(EMAIL_TRANSPORT='resend', RESEND_API_KEY='re_sentinel',
                               RESEND_TIMEOUT_SECONDS=30, WELCOME_EMAIL_TIMEOUT_SECONDS=10,
                               EMAIL_IDEMPOTENCY_NAMESPACE='staging'), \
                mock.patch('pythagoras.mail.resend.ResendTransport._send', autospec=True,
                           side_effect=fake_send) as send:
            result = EmailService.send_welcome_email(user)
        self.assertEqual(result, SendResult.accepted('provider-id-1'))
        self.assertEqual(send.call_args.args[1].idempotency_key, f'staging/welcome/{user.pk}/v1')
        self.assertEqual(seen['timeout'], 10)
