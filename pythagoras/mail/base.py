"""Provider-agnostic outbound email contract (D4 "Resend transport contract")."""
from dataclasses import dataclass
from enum import Enum

MAX_IDEMPOTENCY_KEY_LENGTH = 256


class InvalidMessage(ValueError):
    """A message violates the frozen single-recipient contract.

    The message text names the offending field only, never its value.
    """


def _require_text(name, value, *, single_line=False):
    if not isinstance(value, str) or not value.strip():
        raise InvalidMessage(f'{name} must be a non-empty string.')
    if single_line and ('\r' in value or '\n' in value):
        raise InvalidMessage(f'{name} must be a single line.')


@dataclass(frozen=True)
class OutboundEmail:
    """One logical message to exactly one recipient.

    Freeze it when forming the request; every retry must reuse the identical
    instance (payload) and ``idempotency_key``.
    """

    from_email: str
    to: str
    subject: str
    html: str
    text: str
    idempotency_key: str
    reply_to: str | None = None

    def __post_init__(self):
        for name in ('from_email', 'to', 'subject', 'idempotency_key'):
            _require_text(name, getattr(self, name), single_line=True)
        _require_text('html', self.html)
        _require_text('text', self.text)
        if ',' in self.to or ';' in self.to:
            raise InvalidMessage('to must contain exactly one recipient.')
        if len(self.idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise InvalidMessage('idempotency_key exceeds 256 characters.')
        if self.reply_to is not None:
            _require_text('reply_to', self.reply_to, single_line=True)


class SendStatus(str, Enum):
    ACCEPTED = 'accepted'    # provider accepted; provider_message_id stored. Not inbox proof.
    RETRYABLE = 'retryable'  # definitely not accepted yet; same payload/key may be retried later.
    FAILED = 'failed'        # definitive rejection; do not retry without investigation.
    UNCERTAIN = 'uncertain'  # may or may not have been accepted; replay only same key in window.


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    provider_message_id: str | None = None
    code: str | None = None
    retry_after: int | None = None  # seconds, when the provider supplied it

    @classmethod
    def accepted(cls, provider_message_id):
        return cls(SendStatus.ACCEPTED, provider_message_id=provider_message_id)

    @classmethod
    def retryable(cls, code, retry_after=None):
        return cls(SendStatus.RETRYABLE, code=code, retry_after=retry_after)

    @classmethod
    def failed(cls, code):
        return cls(SendStatus.FAILED, code=code)

    @classmethod
    def uncertain(cls, code):
        return cls(SendStatus.UNCERTAIN, code=code)

    @property
    def is_accepted(self):
        return self.status is SendStatus.ACCEPTED


class EmailTransport:
    """Send one message; return a SendResult. Implementations must not raise
    for provider/network outcomes and must not retry opaquely."""

    def send(self, message: OutboundEmail) -> SendResult:  # pragma: no cover - interface
        raise NotImplementedError
