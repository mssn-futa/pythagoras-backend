from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower, Upper
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .identity import normalize_department, normalize_email, normalize_matric
from .managers import CustomUserManager


class CustomUser(AbstractBaseUser, PermissionsMixin):
    """
    Custom user model.
    """

    class Role(models.TextChoices):
        STUDENT = "student", _("Student")
        ADMIN = "admin", _("Admin")

    LEVEL_CHOICES = [
        ("100", "100 Level"),
        ("200", "200 Level"),
        ("300", "300 Level"),
        ("400", "400 Level"),
        ("500", "500 Level"),
    ]

    email = models.EmailField(_("email address"), unique=True)
    first_name = models.CharField(_("first name"), max_length=150, blank=True)
    last_name = models.CharField(_("last name"), max_length=150, blank=True)
    matric_number = models.CharField(
        _("matric number"), max_length=20, unique=True, blank=True, null=True
    )
    department = models.CharField(_("department"), max_length=100, blank=True)
    level = models.CharField(
        _("level"), max_length=3, choices=LEVEL_CHOICES, blank=True
    )
    role = models.CharField(
        _("role"),
        max_length=10,
        choices=Role.choices,
        default=Role.STUDENT,
        help_text=_("User role for access control."),
    )
    is_staff = models.BooleanField(
        _("staff status"),
        default=False,
        help_text=_("Designates whether the user can log into this admin site."),
    )
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_(
            "Designates whether this user should be treated as active. "
            "Unselect this instead of deleting accounts."
        ),
    )
    date_joined = models.DateTimeField(_("date joined"), default=timezone.now)

    objects = CustomUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        constraints = [
            models.UniqueConstraint(
                Lower("email"), name="accounts_user_email_ci_unique",
                violation_error_message=_("An account with this email already exists."),
            ),
            models.UniqueConstraint(
                Upper("matric_number"), name="accounts_user_matric_ci_unique",
                violation_error_message=_("An account with this matric number already exists."),
            ),
            models.CheckConstraint(
                condition=~Q(matric_number=""), name="accounts_user_matric_not_blank",
            ),
        ]

    def __str__(self):
        return self.email

    def normalize_identity(self):
        self.email = normalize_email(self.email)
        self.matric_number = normalize_matric(self.matric_number)
        self.department = normalize_department(self.department)

    def clean(self):
        super().clean()
        self.normalize_identity()

    def save(self, *args, **kwargs):
        # Every write path (API, admin, shell, commands) stores normalized identity.
        self.normalize_identity()
        super().save(*args, **kwargs)

    def get_full_name(self):
        """
        Return the first_name plus the last_name, with a space in between.
        """
        full_name = f"{self.first_name} {self.last_name}"
        return full_name.strip()

    def get_short_name(self):
        """
        Return the short name for the user.
        """
        return self.first_name

    @property
    def is_admin(self):
        """Check if user has admin role."""
        return self.role == self.Role.ADMIN

    @property
    def is_student(self):
        """Check if user has student role."""
        return self.role == self.Role.STUDENT
