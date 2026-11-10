"""Resend HTTPS Email API adapter (stdlib only, no opaque retries).

Never logs or returns the API key, request body, or addresses.
"""
import email.utils
import json
import logging
import math
import re
import socket
import time
import urllib.error
import urllib.request

from .base import EmailTransport, OutboundEmail, SendResult

logger = logging.getLogger('pythagoras.mail')

RESEND_EMAILS_URL = 'https://api.resend.com/emails'
USER_AGENT = 'pythagoras-backend/1.0'
MAX_RESPONSE_BYTES = 64 * 1024
_SAFE_NAME = re.compile(r'^[a-z][a-z0-9_]{0,63}$')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the bearer credential elsewhere; refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return urllib.request.build_opener(_NoRedirect)


def _parse_retry_after(value):
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    return max(0, math.ceil(moment.timestamp() - time.time()))


def _provider_name(body):
    """Resend error ``name`` if it is a plain identifier; never the message."""
    try:
        name = json.loads(body.decode('utf-8')).get('name')
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    return name if isinstance(name, str) and _SAFE_NAME.match(name) else None


def classify_http_error(status, name, retry_after):
    """Map a non-2xx Resend response to a SendResult (D4)."""
    code = name.upper() if name else f'HTTP_{status}'
    if status == 429:
        return SendResult.retryable(code, retry_after)
    if status == 409:
        if name == 'concurrent_idempotent_requests':
            # Same key still in flight: retry the identical request later.
            return SendResult.retryable(code, retry_after)
        if name == 'invalid_idempotent_request':
            # Changed payload under a used key: investigate, never rotate the key.
            return SendResult.failed(code)
        return SendResult.uncertain(code)
    if status == 408 or status >= 500:
        return SendResult.uncertain(code)
    if 400 <= status < 500:
        return SendResult.failed(code)
    return SendResult.uncertain(code)


class ResendTransport(EmailTransport):
    def __init__(self, *, api_key, timeout, url=RESEND_EMAILS_URL):
        self._api_key = api_key or ''
        self._timeout = timeout
        self._url = url

    def __repr__(self):
        return f'ResendTransport(url={self._url!r}, timeout={self._timeout!r})'

    def _request(self, message):
        payload = {
            'from': message.from_email,
            'to': [message.to],
            'subject': message.subject,
            'html': message.html,
            'text': message.text,
        }
        if message.reply_to:
            payload['reply_to'] = message.reply_to
        return urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode('utf-8'),
            method='POST',
            headers={
                'Authorization': f'Bearer {self._api_key}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': USER_AGENT,
                'Idempotency-Key': message.idempotency_key,
            },
        )

    def send(self, message: OutboundEmail) -> SendResult:
        if not self._api_key:
            result = SendResult.failed('TRANSPORT_NOT_CONFIGURED')
        else:
            result = self._send(message)
        logger.info('email_send transport=resend key=%s status=%s code=%s provider_id=%s',
                    message.idempotency_key, result.status.value, result.code,
                    result.provider_message_id)
        return result

    def _send(self, message):
        try:
            with _opener().open(self._request(message), timeout=self._timeout) as response:
                status = response.status
                body = response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as error:
            try:
                body = error.read(MAX_RESPONSE_BYTES)
            except Exception:
                body = b''
            finally:
                error.close()
            if 300 <= error.code < 400:
                return SendResult.uncertain('UNEXPECTED_REDIRECT')
            return classify_http_error(error.code, _provider_name(body),
                                       _parse_retry_after(error.headers.get('Retry-After')))
        except (TimeoutError, socket.timeout):
            return SendResult.uncertain('TIMEOUT')
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                return SendResult.uncertain('TIMEOUT')
            return SendResult.uncertain('CONNECTION_ERROR')
        except (OSError, ValueError):
            return SendResult.uncertain('TRANSPORT_ERROR')

        if not 200 <= status < 300:
            return SendResult.uncertain(f'HTTP_{status}')
        try:
            provider_id = json.loads(body.decode('utf-8')).get('id')
        except (ValueError, AttributeError, UnicodeDecodeError):
            provider_id = None
        if not isinstance(provider_id, str) or not provider_id.strip():
            # Missing/malformed success ID is unresolved, never fabricated sent.
            return SendResult.uncertain('MALFORMED_SUCCESS')
        return SendResult.accepted(provider_id)
