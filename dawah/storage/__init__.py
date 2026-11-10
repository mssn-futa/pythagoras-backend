"""Object-storage port for durable media (D2 "R2 upload validation and delivery").

Callers depend only on :class:`ObjectStorage`. Two adapters exist:

* :class:`dawah.storage.s3.S3ObjectStorage` - S3-compatible API configured for
  Cloudflare R2 (private staging/ready bucket, separate public delivery bucket
  served from an organization custom domain).
* :class:`dawah.storage.memory.InMemoryObjectStorage` - deterministic test double.

The port names *logical* buckets (``PRIVATE``/``PUBLIC``); bucket names,
endpoint and credentials stay inside the adapter. Object keys are always
constructed by the server. Signed URLs returned here are temporary bearer
capabilities: never persist them, return them to non-admin readers, or log them.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass

PRIVATE = 'private'
PUBLIC = 'public'
BUCKETS = (PRIVATE, PUBLIC)


class StorageError(Exception):
    """Provider/transport failure. ``str()`` is a fixed, secret-free code."""

    def __init__(self, code='STORAGE_ERROR'):
        super().__init__(code)
        self.code = code


class StorageUnavailable(StorageError):
    """Storage is not configured or the provider could not be reached."""

    def __init__(self, code='STORAGE_UNAVAILABLE'):
        super().__init__(code)


class ObjectNotFound(StorageError):
    def __init__(self, code='OBJECT_NOT_FOUND'):
        super().__init__(code)


class PreconditionFailed(StorageError):
    """A conditional operation (copy at ETag) did not match the stored object."""

    def __init__(self, code='PRECONDITION_FAILED'):
        super().__init__(code)


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    etag: str  # unquoted
    content_type: str


def normalize_etag(value):
    return (value or '').strip().strip('"')


class ObjectStorage:
    """Port. Adapters implement every method; all raise :class:`StorageError`."""

    def presign_put(self, key: str, *, content_type: str, expires_in: int) -> str:
        """Signed browser PUT to a *private staging* key with a signed Content-Type."""
        raise NotImplementedError

    def presign_get(self, key: str, *, expires_in: int, content_type: str, filename: str) -> str:
        """Short-lived signed GET of a private object (admin preview)."""
        raise NotImplementedError

    def head(self, key: str, *, bucket: str = PRIVATE) -> ObjectInfo | None:
        """Server-observed metadata, or ``None`` when the object does not exist."""
        raise NotImplementedError

    def copy_if_match(self, src_key: str, dst_key: str, *, etag: str, src_bucket: str = PRIVATE,
                      dst_bucket: str = PRIVATE, content_type: str,
                      content_disposition: str | None = None, cache_control: str | None = None) -> ObjectInfo:
        """Server-side copy only if the source still has ``etag``.

        Raises :class:`PreconditionFailed` when it changed and
        :class:`ObjectNotFound` when it is gone.
        """
        raise NotImplementedError

    def open_stream(self, key: str, *, bucket: str = PRIVATE, byte_range: tuple[int, int] | None = None):
        """Readable binary stream (``.read(n)``/``.close()``), optionally an inclusive byte range."""
        raise NotImplementedError

    def delete(self, key: str, *, bucket: str) -> None:
        """Idempotent delete (a missing object is not an error)."""
        raise NotImplementedError

    def public_url(self, key: str) -> str:
        """Canonical public delivery URL (custom domain) of a PUBLIC-bucket key."""
        raise NotImplementedError


class UnconfiguredStorage(ObjectStorage):
    """Used when the R2 settings are incomplete: every operation is unavailable."""

    def _fail(self, *args, **kwargs):
        raise StorageUnavailable('STORAGE_NOT_CONFIGURED')

    presign_put = presign_get = head = copy_if_match = open_stream = delete = public_url = _fail


_lock = threading.Lock()
_override = None
_default = None


def get_storage() -> ObjectStorage:
    """The configured adapter (an installed override wins, e.g. in tests)."""
    global _default
    if _override is not None:
        return _override
    with _lock:
        if _default is None:
            from .s3 import S3ObjectStorage
            _default = S3ObjectStorage.from_settings() or UnconfiguredStorage()
        return _default


def set_storage(storage: ObjectStorage | None):
    """Install (or with ``None`` remove) a process-wide adapter override."""
    global _override
    _override = storage


def reset_default_storage():
    """Forget the cached settings-built adapter (after settings change)."""
    global _default
    with _lock:
        _default = None


@contextmanager
def use_storage(storage: ObjectStorage):
    previous = _override
    set_storage(storage)
    try:
        yield storage
    finally:
        set_storage(previous)
