"""Minimal, non-PII runtime health endpoints (RELEASE-09).

These are plain Django views, deliberately outside the DRF envelope, so that a
container supervisor can probe them without authentication or JSON-envelope
parsing. They never reveal configuration, hostnames, versions, or exception
text.

* ``/healthz/``: process liveness. Does not touch the database.
* ``/readyz/``: readiness. Performs ``SELECT 1`` on the default database.
"""
import logging

from django.db import connections
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

logger = logging.getLogger('pythagoras.health')


@never_cache
@require_safe
def healthz(request):
    return JsonResponse({'status': 'ok'})


@never_cache
@require_safe
def readyz(request):
    try:
        with connections['default'].cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 - class name only; never the message/DSN
        logger.error('readiness database check failed: %s', type(exc).__name__)
        return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok'})


urlpatterns = [
    path('healthz/', healthz, name='healthz'),
    path('readyz/', readyz, name='readyz'),
]
