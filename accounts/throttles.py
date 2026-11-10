"""Per-IP authentication throttles (D5). Rates: REST_FRAMEWORK DEFAULT_THROTTLE_RATES."""
from rest_framework.throttling import SimpleRateThrottle


class _PerIPThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


class RegistrationRateThrottle(_PerIPThrottle):
    scope = 'auth_register'


class LoginRateThrottle(_PerIPThrottle):
    scope = 'auth_login'


class RefreshRateThrottle(_PerIPThrottle):
    scope = 'auth_refresh'
