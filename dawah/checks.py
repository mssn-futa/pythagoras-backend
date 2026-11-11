"""Deployment checks for dawah settings (``manage.py check``)."""
from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, register


@register()
def frontend_base_url_check(app_configs, **kwargs):
    """Production reminder links must be https without credentials (COMM-06)."""
    value = getattr(settings, 'FRONTEND_BASE_URL', '') or ''
    if settings.DEBUG or not value:
        return []
    parts = urlsplit(value)
    if parts.scheme != 'https' or not parts.hostname or '@' in parts.netloc:
        return [Error('FRONTEND_BASE_URL must be an https URL without user info when DEBUG is false.',
                      id='dawah.E001')]
    return []
