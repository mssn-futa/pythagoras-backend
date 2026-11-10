import email.message
import http.client
import io
import json
import logging
import os
import socket
import threading
import time
import urllib.error
from dataclasses import FrozenInstanceError
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from pythagoras.mail import (
    InvalidMessage, OutboundEmail, SendResult, SendStatus, get_transport, memory, send_email,
)
from pythagoras.mail.memory import MemoryTransport
from pythagoras.mail.resend import RESEND_EMAILS_URL, ResendTransport

API_KEY = 're_test_sentinel_key_must_not_leak'
RECIPIENT = 'recipient-private@example.test'


def message(**overrides):
    values = dict(from_email='Pythagoras <noreply@example.test>', to=RECIPIENT,
                  subject='Hello', html='<p>Body-secret-html</p>', text='Body-secret-text',
                  idempotency_key='welcome/7/v1', reply_to=None)
    values.update(overrides)
    return OutboundEmail(**values)


class FakeResponse:
    def __init__(self, status=200, body=b'{"id":"4ef9a417-02e9-4d39-ad75-9611e0fcc33c"}'):
        self.status = status
        self._body = io.BytesIO(body)

    def read(self, limit=-1):
        return self._body.read(limit)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(status, body=b'', headers=None):
    msg = email.message.Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return urllib.error.HTTPError(RESEND_EMAILS_URL, status, 'error', msg, io.BytesIO(body))


class OutboundEmailContractTests(SimpleTestCase):
    def test_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            message().to = 'other@example.test'

    def test_single_recipient_and_required_fields(self):
        for overrides in ({'to': ['a@example.test']}, {'to': 'a@x.test, b@x.test'},
                          {'to': 'a@x.test;b@x.test'}, {'to': ''}, {'from_email': ''},
                          {'html': ''}, {'text': '  '}, {'subject': 'a\nBcc: x@y.test'},
                          {'idempotency_key': ''}, {'idempotency_key': 'k' * 257},
                          {'reply_to': 'a@x.test\r\n'}):
            with self.subTest(overrides=list(overrides)):
                with self.assertRaises(InvalidMessage) as caught:
                    message(**overrides)
                self.assertNotIn(RECIPIENT, str(caught.exception))
        message(idempotency_key='k' * 256)


class TransportSelectionTests(SimpleTestCase):
    def test_tests_force_memory_transport_and_blank_key(self):
        self.assertEqual(settings.EMAIL_TRANSPORT, 'memory')
        self.assertEqual(settings.RESEND_API_KEY, '')
        self.assertIsInstance(get_transport(), MemoryTransport)

    @override_settings(EMAIL_TRANSPORT='resend', RESEND_API_KEY=API_KEY, RESEND_TIMEOUT_SECONDS=7)
    def test_resend_selected_by_setting(self):
        transport = get_transport()
        self.assertIsInstance(transport, ResendTransport)
        self.assertNotIn(API_KEY, repr(transport))
        self.assertNotIn(API_KEY, str(vars(transport).get('_url')))


class MemoryTransportTests(SimpleTestCase):
    def setUp(self):
        memory.reset()

    def test_records_and_honours_idempotency(self):
        first = send_email(message())
        again = send_email(message())
        self.assertEqual(first, again)
        self.assertTrue(first.is_accepted)
        self.assertEqual(len(memory.outbox), 1)
        changed = send_email(message(subject='Different'))
        self.assertEqual(changed, SendResult.failed('IDEMPOTENCY_PAYLOAD_MISMATCH'))
        self.assertEqual(len(memory.outbox), 1)


class ResendTransportTests(SimpleTestCase):
    def setUp(self):
        self.transport = ResendTransport(api_key=API_KEY, timeout=7)

    def send(self, outcome, msg=None):
        opener = mock.Mock()
        if isinstance(outcome, BaseException):
            opener.open.side_effect = outcome
        else:
            opener.open.return_value = outcome
        with mock.patch('pythagoras.mail.resend._opener', return_value=opener), \
                self.assertLogs('pythagoras.mail', level='DEBUG') as logs:
            result = self.transport.send(msg or message())
        self.opener = opener
        self.logs = '\n'.join(logs.output)
        return result

    def assert_private(self, result):
        text = self.logs + repr(result)
        for secret in (API_KEY, RECIPIENT, 'Body-secret-html', 'Body-secret-text', 'noreply@example.test'):
            self.assertNotIn(secret, text)

    def test_request_shape_headers_and_timeout(self):
        result = self.send(FakeResponse(), message(reply_to='help@example.test'))
        self.assertEqual(result, SendResult.accepted('4ef9a417-02e9-4d39-ad75-9611e0fcc33c'))
        request = self.opener.open.call_args.args[0]
        # Per-socket timeout is the remaining share of the 7 s total budget.
        self.assertEqual(set(self.opener.open.call_args.kwargs), {'timeout'})
        self.assertTrue(0 < self.opener.open.call_args.kwargs['timeout'] <= 7)
        self.assertEqual(self.opener.open.call_count, 1)  # no opaque retries
        self.assertEqual(request.full_url, 'https://api.resend.com/emails')
        self.assertEqual(request.get_method(), 'POST')
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers['authorization'], f'Bearer {API_KEY}')
        self.assertEqual(headers['idempotency-key'], 'welcome/7/v1')
        self.assertEqual(headers['content-type'], 'application/json')
        self.assertTrue(headers['user-agent'].startswith('pythagoras-backend/'))
        self.assertEqual(json.loads(request.data), {
            'from': 'Pythagoras <noreply@example.test>', 'to': [RECIPIENT], 'subject': 'Hello',
            'html': '<p>Body-secret-html</p>', 'text': 'Body-secret-text',
            'reply_to': 'help@example.test'})
        self.assert_private(result)
        self.assertIn('status=accepted', self.logs)

    def test_reply_to_omitted_when_unset(self):
        self.send(FakeResponse())
        payload = json.loads(self.opener.open.call_args.args[0].data)
        self.assertNotIn('reply_to', payload)
        self.assertNotIn('cc', payload)
        self.assertNotIn('bcc', payload)

    def test_missing_or_malformed_success_id_is_uncertain(self):
        for body in (b'{}', b'{"id": ""}', b'not json', b'{"id": 5}'):
            with self.subTest(body=body):
                self.assertEqual(self.send(FakeResponse(body=body)), SendResult.uncertain('MALFORMED_SUCCESS'))

    def test_classification(self):
        cases = [
            (http_error(429, b'{"name":"rate_limit_exceeded","message":"x"}', {'Retry-After': '3'}),
             SendResult.retryable('RATE_LIMIT_EXCEEDED', 3)),
            (http_error(429, b'{"name":"daily_quota_exceeded"}'),
             SendResult.retryable('DAILY_QUOTA_EXCEEDED', None)),
            (http_error(409, b'{"name":"concurrent_idempotent_requests"}', {'Retry-After': '1'}),
             SendResult.retryable('CONCURRENT_IDEMPOTENT_REQUESTS', 1)),
            (http_error(409, b'{"name":"invalid_idempotent_request"}'),
             SendResult.failed('INVALID_IDEMPOTENT_REQUEST')),
            (http_error(409, b''), SendResult.uncertain('HTTP_409')),
            (http_error(422, b'{"name":"validation_error","message":"' + RECIPIENT.encode() + b'"}'),
             SendResult.failed('VALIDATION_ERROR')),
            (http_error(401, b'{"name":"missing_api_key"}'), SendResult.failed('MISSING_API_KEY')),
            (http_error(403, b'<html>' + API_KEY.encode()), SendResult.failed('HTTP_403')),
            (http_error(400, b'{"name":"Not An Identifier!"}'), SendResult.failed('HTTP_400')),
            (http_error(500, b'{"name":"application_error"}'), SendResult.uncertain('APPLICATION_ERROR')),
            (http_error(503), SendResult.uncertain('HTTP_503')),
            (http_error(302, b'', {'Location': 'https://elsewhere.test/'}),
             SendResult.uncertain('UNEXPECTED_REDIRECT')),
            (socket.timeout('timed out'), SendResult.uncertain('TIMEOUT')),
            (TimeoutError(), SendResult.uncertain('TIMEOUT')),
            (urllib.error.URLError(socket.timeout('timed out')), SendResult.uncertain('TIMEOUT')),
            (urllib.error.URLError(ConnectionRefusedError()), SendResult.uncertain('CONNECTION_ERROR')),
            (ConnectionResetError(), SendResult.uncertain('TRANSPORT_ERROR')),
            (http.client.BadStatusLine('garbage'), SendResult.uncertain('TRANSPORT_ERROR')),
            (http.client.LineTooLong('status line'), SendResult.uncertain('TRANSPORT_ERROR')),
            (http.client.IncompleteRead(b'{"id"', 20), SendResult.uncertain('TRANSPORT_ERROR')),
            (http.client.RemoteDisconnected('closed'), SendResult.uncertain('TRANSPORT_ERROR')),
            (RuntimeError('unexpected bug'), SendResult.uncertain('TRANSPORT_ERROR')),
        ]
        for outcome, expected in cases:
            with self.subTest(expected=expected):
                result = self.send(outcome)
                self.assertEqual(result, expected)
                self.assert_private(result)

    def test_retry_after_http_date(self):
        result = self.send(http_error(429, b'', {'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT'}))
        self.assertEqual((result.status, result.retry_after), (SendStatus.RETRYABLE, 0))

    def test_missing_key_never_sends(self):
        transport = ResendTransport(api_key='', timeout=7)
        opener = mock.Mock()
        with mock.patch('pythagoras.mail.resend._opener', return_value=opener), \
                self.assertLogs('pythagoras.mail', level='INFO'):
            result = transport.send(message())
        self.assertEqual(result, SendResult.failed('TRANSPORT_NOT_CONFIGURED'))
        opener.open.assert_not_called()

    def test_redirects_are_not_followed(self):
        from pythagoras.mail.resend import _NoRedirect, _opener
        self.assertTrue(any(isinstance(h, _NoRedirect) for h in _opener().handlers))
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 307, '', {}, 'https://evil.test'))


class DeployCheckTests(SimpleTestCase):
    def test_deploy_warnings(self):
        from pythagoras.mail.checks import email_transport_check
        with override_settings(EMAIL_TRANSPORT='memory'):
            self.assertEqual(email_transport_check(None)[0].id, 'pythagoras.W101')
        with override_settings(EMAIL_TRANSPORT='resend', RESEND_API_KEY='', DEFAULT_FROM_EMAIL='a@x.test'):
            warning = email_transport_check(None)[0]
            self.assertEqual(warning.id, 'pythagoras.W102')
        with override_settings(EMAIL_TRANSPORT='resend', RESEND_API_KEY=API_KEY, DEFAULT_FROM_EMAIL='a@x.test'):
            self.assertEqual(email_transport_check(None), [])


class IdempotencyKeyTests(SimpleTestCase):
    def test_namespaced_keys(self):
        from pythagoras.mail import idempotency_key
        self.assertEqual(settings.EMAIL_IDEMPOTENCY_NAMESPACE, 'local')
        self.assertEqual(idempotency_key('welcome', 7, 'v1'), 'local/welcome/7/v1')
        with override_settings(EMAIL_IDEMPOTENCY_NAMESPACE='production'):
            self.assertEqual(idempotency_key('broadcast', 3, 'generation', 1),
                             'production/broadcast/3/generation/1')
        for bad in ((), ('a/b',), ('',), (' x',), ('x' * 300,)):
            with self.subTest(parts=bad), self.assertRaises(ValueError):
                idempotency_key(*bad)

    @override_settings(EMAIL_TRANSPORT='resend', RESEND_API_KEY=API_KEY, RESEND_TIMEOUT_SECONDS=30)
    def test_timeout_override_only_tightens(self):
        self.assertEqual(get_transport()._timeout, 30)
        self.assertEqual(get_transport(timeout=10)._timeout, 10)
        self.assertEqual(get_transport(timeout=99)._timeout, 30)


class LocalServerTransportTests(SimpleTestCase):
    """Real sockets against a local server; no provider traffic."""

    def serve(self, handler):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        stop = threading.Event()

        def run():
            listener.settimeout(5)
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                try:
                    data = b''
                    while b'\r\n\r\n' not in data:
                        data += conn.recv(65536)
                    handler(conn, stop)
                except OSError:
                    pass
            listener.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(stop.set)
        self.addCleanup(thread.join, 10)
        return f'http://127.0.0.1:{listener.getsockname()[1]}/emails'

    def send(self, url, timeout):
        transport = ResendTransport(api_key=API_KEY, timeout=timeout, url=url)
        env = {'NO_PROXY': '*', 'no_proxy': '*'}
        with mock.patch.dict(os.environ, env), self.assertLogs('pythagoras.mail', 'INFO') as logs:
            started = time.monotonic()
            result = transport.send(message())
            elapsed = time.monotonic() - started
        self.assertNotIn(API_KEY, '\n'.join(logs.output))
        return result, elapsed

    def test_slow_drip_response_hits_total_deadline(self):
        def drip(conn, stop):
            body = b'{"id":"4ef9a417-02e9-4d39-ad75-1"}'
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                         b'Content-Length: %d\r\n\r\n' % len(body))
            for byte in body:
                if stop.wait(0.3):
                    return
                conn.sendall(bytes([byte]))

        result, elapsed = self.send(self.serve(drip), timeout=1)
        self.assertEqual(result, SendResult.uncertain('DEADLINE_EXCEEDED'))
        self.assertLess(elapsed, 2.5)

    def test_fast_local_success(self):
        def ok(conn, stop):
            body = b'{"id":"local-id-1"}'
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s' % (len(body), body))

        result, _ = self.send(self.serve(ok), timeout=5)
        self.assertEqual(result, SendResult.accepted('local-id-1'))

    def test_garbage_status_line_is_uncertain_not_raised(self):
        result, _ = self.send(self.serve(lambda conn, stop: conn.sendall(b'garbage\r\n\r\n')), timeout=5)
        self.assertEqual(result, SendResult.uncertain('TRANSPORT_ERROR'))

    def test_overlong_status_line_is_uncertain_not_raised(self):
        result, _ = self.send(self.serve(
            lambda conn, stop: conn.sendall(b'HTTP/1.1 200 ' + b'x' * 70000 + b'\r\n\r\n')), timeout=5)
        self.assertEqual(result, SendResult.uncertain('TRANSPORT_ERROR'))

    def test_truncated_body_is_never_accepted(self):
        result, _ = self.send(self.serve(lambda conn, stop: conn.sendall(
            b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"id":"x"')), timeout=5)
        self.assertEqual(result.status, SendStatus.UNCERTAIN)

    def test_oversized_body_is_capped(self):
        def big(conn, stop):
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n{"id":"' + b'a' * 200000)

        result, _ = self.send(self.serve(big), timeout=5)
        self.assertEqual(result, SendResult.uncertain('MALFORMED_SUCCESS'))

    def test_unbounded_dns_is_cut_by_total_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def hanging_getaddrinfo(*args, **kwargs):
            release.wait(10)
            raise socket.gaierror('released')

        with mock.patch('socket.getaddrinfo', hanging_getaddrinfo):
            result, elapsed = self.send('https://api.resend.invalid/emails', timeout=1)
        self.assertEqual(result, SendResult.uncertain('DEADLINE_EXCEEDED'))
        self.assertLess(elapsed, 2.5)
