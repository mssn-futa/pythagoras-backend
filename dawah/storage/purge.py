"""CDN cache-purge port for deleted public media URLs.

Deleting a public object does not evict copies already cached at the edge.
The cleanup job calls :func:`get_purger` ``.purge(urls)`` after deleting a
public copy. The default is a no-op; :class:`CloudflarePurger` (purge by URL)
is enabled only when both ``CLOUDFLARE_ZONE_ID`` and
``CLOUDFLARE_PURGE_API_TOKEN`` are configured. Already downloaded bytes can
never be recalled. Errors carry fixed codes only (never the token or URLs).
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

from django.conf import settings

PURGE_BATCH = 30  # Cloudflare purge-by-URL accepts at most 30 files per request on every plan
PURGE_TIMEOUT_SECONDS = 10


class PurgeError(Exception):
    def __init__(self, code='PURGE_FAILED'):
        super().__init__(code)
        self.code = code


class CachePurger:
    def purge(self, urls: list[str]) -> None:
        raise NotImplementedError


class NoopPurger(CachePurger):
    def purge(self, urls):
        return None


class CloudflarePurger(CachePurger):
    API = 'https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache'

    def __init__(self, zone_id, api_token, timeout=PURGE_TIMEOUT_SECONDS):
        self.url = self.API.format(zone=zone_id)
        self.token = api_token
        self.timeout = timeout

    def purge(self, urls):
        urls = [u for u in urls if u]
        for start in range(0, len(urls), PURGE_BATCH):
            self._post(urls[start:start + PURGE_BATCH])

    def _post(self, files):
        request = urllib.request.Request(
            self.url, method='POST', data=json.dumps({'files': files}).encode(),
            headers={'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as exc:
            raise PurgeError(f'PURGE_HTTP_{exc.code}') from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise PurgeError('PURGE_UNAVAILABLE') from None
        if not body.get('success'):
            raise PurgeError('PURGE_REJECTED')


def purger_from_settings():
    zone = (getattr(settings, 'CLOUDFLARE_ZONE_ID', '') or '').strip()
    token = (getattr(settings, 'CLOUDFLARE_PURGE_API_TOKEN', '') or '').strip()
    if zone and token and re.fullmatch(r'[0-9a-f]{32}', zone):
        return CloudflarePurger(zone, token)
    return NoopPurger()


_lock = threading.Lock()
_override = None
_default = None


def get_purger() -> CachePurger:
    global _default
    if _override is not None:
        return _override
    with _lock:
        if _default is None:
            _default = purger_from_settings()
        return _default


def set_purger(purger: CachePurger | None):
    global _override
    _override = purger


def reset_default_purger():
    global _default
    with _lock:
        _default = None


@contextmanager
def use_purger(purger: CachePurger):
    previous = _override
    set_purger(purger)
    try:
        yield purger
    finally:
        set_purger(previous)
