"""Deployment checks (`manage.py check --deploy`) for the email transport."""
from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security, deploy=True)
def email_transport_check(app_configs, **kwargs):
    if settings.EMAIL_TRANSPORT == 'memory':
        return [Warning('EMAIL_TRANSPORT=memory sends no email.', id='pythagoras.W101')]
    missing = [name for name in ('RESEND_API_KEY', 'DEFAULT_FROM_EMAIL') if not getattr(settings, name)]
    if missing:
        return [Warning(f'Resend transport is not configured: {", ".join(missing)} blank; '
                        'mail outcomes will be failed TRANSPORT_NOT_CONFIGURED/INVALID_MESSAGE.',
                        id='pythagoras.W102')]
    return []
