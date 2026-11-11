"""Shared outbound email transport.

Callers build a frozen :class:`OutboundEmail` and call :func:`send_email` (or
``get_transport().send``). The provider is chosen by ``settings.EMAIL_TRANSPORT``
('resend' or 'memory'), so callers never import a provider directly.
"""
from django.conf import settings
from django.utils.module_loading import import_string

from .base import (
    MAX_IDEMPOTENCY_KEY_LENGTH, EmailTransport, InvalidMessage, OutboundEmail, SendResult, SendStatus,
)

TRANSPORTS = {
    'resend': 'pythagoras.mail.resend.ResendTransport',
    'memory': 'pythagoras.mail.memory.MemoryTransport',
}

__all__ = ['EmailTransport', 'InvalidMessage', 'OutboundEmail', 'SendResult', 'SendStatus',
           'get_transport', 'idempotency_key', 'send_email', 'TRANSPORTS']


def idempotency_key(*parts) -> str:
    """Environment-scoped provider key: ``{EMAIL_IDEMPOTENCY_NAMESPACE}/part/...``.

    Example: ``idempotency_key('welcome', 7, 'v1')`` -> ``'production/welcome/7/v1'``.
    Parts must be non-empty and contain no '/'. Use one stable key per logical
    message; never mint a new one to resend.
    """
    if not parts:
        raise ValueError('idempotency_key needs at least one part.')
    clean = []
    for part in parts:
        text = str(part)
        if not text or '/' in text or text.strip() != text:
            raise ValueError('idempotency key parts must be non-empty, trimmed and contain no "/".')
        clean.append(text)
    key = '/'.join([settings.EMAIL_IDEMPOTENCY_NAMESPACE, *clean])
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError('idempotency key exceeds 256 characters.')
    return key


def get_transport(*, timeout=None) -> EmailTransport:
    """Configured transport. ``timeout`` (seconds) may only tighten the
    RESEND_TIMEOUT_SECONDS total bound."""
    name = settings.EMAIL_TRANSPORT
    cls = import_string(TRANSPORTS[name])
    if name == 'resend':
        bound = settings.RESEND_TIMEOUT_SECONDS
        if timeout is not None:
            bound = min(timeout, bound)
        return cls(api_key=settings.RESEND_API_KEY, timeout=bound)
    return cls()


def send_email(message: OutboundEmail, *, timeout=None) -> SendResult:
    return get_transport(timeout=timeout).send(message)
