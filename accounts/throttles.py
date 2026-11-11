"""Authentication throttles (D5). Rates: REST_FRAMEWORK DEFAULT_THROTTLE_RATES."""
import hashlib

from rest_framework.throttling import SimpleRateThrottle


class _PerIPThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


class RegistrationRateThrottle(_PerIPThrottle):
    scope = 'auth_register'


class LoginRateThrottle(_PerIPThrottle):
    scope = 'auth_login'


class RefreshRateThrottle(SimpleRateThrottle):
    """Per session: keyed on the presented refresh token, else the client IP.

    Every page load refreshes, and many students can share one campus NAT
    address, so a per-IP limit at the D5 rate would lock out a whole hall.
    The token is never stored, only its digest.
    """

    scope = 'auth_refresh'

    def get_cache_key(self, request, view):
        token = request.data.get('refresh') if hasattr(request.data, 'get') else None
        if isinstance(token, str) and token:
            ident = 'token:' + hashlib.sha256(token.encode()).hexdigest()
        else:
            ident = 'ip:' + self.get_ident(request)
        return self.cache_format % {'scope': self.scope, 'ident': ident}


class RefreshIPRateThrottle(_PerIPThrottle):
    """Ceiling per client IP so arbitrary tokens cannot bypass the session limit."""

    scope = 'auth_refresh_ip'
