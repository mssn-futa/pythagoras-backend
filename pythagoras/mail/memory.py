"""In-memory transport for tests and local development. Sends nothing."""
import threading

from .base import EmailTransport, OutboundEmail, SendResult

_lock = threading.Lock()
outbox: list[OutboundEmail] = []
_accepted: dict[str, tuple[OutboundEmail, str]] = {}


def reset():
    with _lock:
        outbox.clear()
        _accepted.clear()


class MemoryTransport(EmailTransport):
    """Mimics provider idempotency: the same key and payload returns the
    original ID without a second outbox entry; a changed payload under a used
    key is a definitive failure."""

    def send(self, message: OutboundEmail) -> SendResult:
        with _lock:
            previous = _accepted.get(message.idempotency_key)
            if previous is not None:
                if previous[0] != message:
                    return SendResult.failed('IDEMPOTENCY_PAYLOAD_MISMATCH')
                return SendResult.accepted(previous[1])
            provider_id = f'memory-{len(_accepted) + 1}'
            _accepted[message.idempotency_key] = (message, provider_id)
            outbox.append(message)
            return SendResult.accepted(provider_id)
