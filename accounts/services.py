import logging

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from pythagoras.mail import InvalidMessage, OutboundEmail, SendResult, send_email

logger = logging.getLogger('accounts.email')

# Bump whenever the welcome subject/templates change, so a changed payload is
# never sent under an idempotency key that may already have been used.
WELCOME_MESSAGE_VERSION = 'v1'
WELCOME_SUBJECT = "Welcome to Pythagoras! 🎓"


def welcome_idempotency_key(user_id):
    return f'welcome/{user_id}/{WELCOME_MESSAGE_VERSION}'


class EmailService:
    """Templated application email through the shared transport (pythagoras.mail)."""

    @staticmethod
    def render(template_name, context):
        html = render_to_string(f"emails/{template_name}.html", context)
        try:
            text = render_to_string(f"emails/{template_name}.txt", context)
        except Exception:
            text = strip_tags(html)
        return html, text

    @classmethod
    def send_templated(cls, *, subject, template_name, context, to, idempotency_key) -> SendResult:
        """Send one templated message to one recipient from the server sender.

        Returns the structured transport outcome. Never raises for rendering,
        configuration or provider failures; those are logged without addresses.
        """
        try:
            html, text = cls.render(template_name, context)
            message = OutboundEmail(
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=to,
                subject=subject,
                html=html,
                text=text,
                reply_to=settings.EMAIL_REPLY_TO or None,
                idempotency_key=idempotency_key,
            )
        except InvalidMessage as exc:
            result = SendResult.failed('INVALID_MESSAGE')
            logger.warning('email_not_sent key=%s code=%s reason=%s',
                           idempotency_key, result.code, exc)
            return result
        except Exception:
            result = SendResult.failed('RENDER_ERROR')
            logger.error('email_not_sent key=%s code=%s', idempotency_key, result.code)
            return result
        try:
            result = send_email(message)
        except Exception:
            # Transports should not raise; treat anything unexpected as unresolved.
            result = SendResult.uncertain('TRANSPORT_EXCEPTION')
        log = logger.info if result.is_accepted else logger.warning
        log('email_result key=%s status=%s code=%s provider_id=%s', idempotency_key,
            result.status.value, result.code, result.provider_message_id)
        return result

    @classmethod
    def send_welcome_email(cls, user) -> SendResult:
        """Send the optional welcome message now. Never raises."""
        context = {
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "matric_number": user.matric_number,
            "department": user.department,
            "level": user.get_level_display() if user.level else None,
        }
        return cls.send_templated(
            subject=WELCOME_SUBJECT,
            template_name="welcome",
            context=context,
            to=user.email,
            idempotency_key=welcome_idempotency_key(user.pk),
        )

    @classmethod
    def schedule_welcome_email(cls, user):
        """Send after the account transaction commits; failure never rolls it back."""
        transaction.on_commit(lambda: cls.send_welcome_email(user))
