import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags


class EmailService:
    """
    Service class for sending emails.
    """

    @staticmethod
    def send_email(
        subject: str,
        template_name: str,
        context: dict,
        recipient_list: list[str],
        from_email: str = None,
    ) -> bool:
        """
        Send an email using HTML template with plain text fallback.

        Args:
            subject: Email subject line
            template_name: Name of the template (without extension)
            context: Dictionary of context variables for the template
            recipient_list: List of recipient email addresses
            from_email: Sender email (defaults to DEFAULT_FROM_EMAIL)

        Returns:
            bool: True if email sent successfully, False otherwise
        """
        if from_email is None:
            from_email = settings.DEFAULT_FROM_EMAIL

        try:
            html_content = render_to_string(f"emails/{template_name}.html", context)

            try:
                text_content = render_to_string(f"emails/{template_name}.txt", context)
            except Exception:
                text_content = strip_tags(html_content)

            email = EmailMultiAlternatives(
                subject=subject,
                body=text_content,
                from_email=from_email,
                to=recipient_list,
            )
            email.attach_alternative(html_content, "text/html")

            email.send(fail_silently=False)

            return True

        except Exception as e:
            return False

    @classmethod
    def send_welcome_email(cls, user) -> bool:
        """
        Send welcome email to newly registered user.

        Args:
            user: CustomUser instance

        Returns:
            bool: True if email sent successfully, False otherwise
        """
        context = {
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "matric_number": user.matric_number,
            "department": user.department,
            "level": user.get_level_display() if user.level else None,
        }

        return cls.send_email(
            subject="Welcome to Pythagoras! 🎓",
            template_name="welcome",
            context=context,
            recipient_list=[user.email],
        )
