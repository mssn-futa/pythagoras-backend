from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle

UPLOAD_RATE = '5/min'  # D5 engineering starting limit: uploads 5/min per admin


class UploadIntentRateThrottle(SimpleRateThrottle):
    """Per-admin (user ID) limit on upload intents. Counters use the default
    cache; deployment-wide enforcement needs a shared cache (D5)."""
    scope = 'dawah_upload'

    def get_rate(self):
        return api_settings.DEFAULT_THROTTLE_RATES.get(self.scope, UPLOAD_RATE)

    def get_cache_key(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return None  # permissions reject anonymous callers first
        return self.cache_format % {'scope': self.scope, 'ident': request.user.pk}


FINALIZE_RATE = '30/min'  # bounds concurrent validation work (decode/decompress memory) per admin


class UploadFinalizeRateThrottle(UploadIntentRateThrottle):
    """Per-admin limit on finalize calls (each may decode an image / walk an archive)."""
    scope = 'dawah_finalize'

    def get_rate(self):
        return api_settings.DEFAULT_THROTTLE_RATES.get(self.scope, FINALIZE_RATE)
