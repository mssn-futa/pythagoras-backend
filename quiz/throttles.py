"""Per-student assessment throttles (D5 engineering starting limits).

* attempt start: 20/min per student (``quiz_attempt_start``)
* submissions:   60/min per student (``quiz_submission``)

Keyed by the authenticated user's ID, not the client IP, so students behind one
NAT (a lecture hall) do not share a budget. A rate can be tuned through
``REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'][scope]``; without an entry the D5
default below applies (no settings change is required). Counters use the
default cache: a shared cache is required for deployment-wide enforcement.

Throttles run after authentication and permission checks, so anonymous,
inactive and non-student requests never consume a student's budget. 60/min
leaves ample room for the frontend's bounded replay of one frozen submit
payload after an uncertain outcome (a 429 is itself treated as uncertain and
retried later with the same payload).
"""
from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


class _PerStudentThrottle(SimpleRateThrottle):
    default_rate = None

    def get_rate(self):
        rates = getattr(settings, 'REST_FRAMEWORK', {}).get('DEFAULT_THROTTLE_RATES', {})
        return rates.get(self.scope, self.default_rate)

    def get_cache_key(self, request, view):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return None  # unreachable behind the student permission; never throttle anonymously
        return self.cache_format % {'scope': self.scope, 'ident': user.pk}


class AttemptStartRateThrottle(_PerStudentThrottle):
    scope = 'quiz_attempt_start'
    default_rate = '20/min'


class SubmissionRateThrottle(_PerStudentThrottle):
    scope = 'quiz_submission'
    default_rate = '60/min'
