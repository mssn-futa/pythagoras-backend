"""Shared outbound email transport.

Callers build a frozen :class:`OutboundEmail` and call :func:`send_email` (or
``get_transport().send``). The provider is chosen by ``settings.EMAIL_TRANSPORT``
('resend' or 'memory'), so callers never import a provider directly.
"""
from django.conf import settings
from django.utils.module_loading import import_string

from .base import EmailTransport, InvalidMessage, OutboundEmail, SendResult, SendStatus

TRANSPORTS = {
    'resend': 'pythagoras.mail.resend.ResendTransport',
    'memory': 'pythagoras.mail.memory.MemoryTransport',
}

__all__ = ['EmailTransport', 'InvalidMessage', 'OutboundEmail', 'SendResult', 'SendStatus',
           'get_transport', 'send_email', 'TRANSPORTS']


def get_transport() -> EmailTransport:
    name = settings.EMAIL_TRANSPORT
    cls = import_string(TRANSPORTS[name])
    if name == 'resend':
        return cls(api_key=settings.RESEND_API_KEY, timeout=settings.RESEND_TIMEOUT_SECONDS)
    return cls()


def send_email(message: OutboundEmail) -> SendResult:
    return get_transport().send(message)
