from django.contrib.auth.base_user import BaseUserManager
from django.utils.translation import gettext_lazy as _

from .identity import normalize_email


class CustomUserManager(BaseUserManager):
    """
    Custom user model manager where email is the unique identifier
    for authentication instead of username
    """

    @classmethod
    def normalize_email(cls, email):
        # Whole-address lowercase, matching the case-insensitive constraint.
        return normalize_email(email or "")

    def get_by_natural_key(self, username):
        return self.get(**{self.model.USERNAME_FIELD: normalize_email(username)})

    def create_user(self, email, password=None, **extra_fields):
        """
        Create and save a regular user with the given email and password
        """
        if not email:
            raise ValueError(_("The Email field must be set"))
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """
        Create and save a technical superuser. Superusers are also product
        administrators (D0): role=admin with staff/superuser flags.
        """
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("role", self.model.Role.ADMIN)

        if extra_fields.get("is_staff") is not True:
            raise ValueError(_("Superuser must have is_staff=True."))
        if extra_fields.get("is_superuser") is not True:
            raise ValueError(_("Superuser must have is_superuser=True."))
        if extra_fields.get("role") != self.model.Role.ADMIN:
            raise ValueError(_("Superuser must have role=admin."))

        return self.create_user(email, password, **extra_fields)
