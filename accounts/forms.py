from django import forms
from django.contrib.auth.forms import UserCreationForm, UserChangeForm

from . import student_admin
from .identity import normalize_email, normalize_matric
from .models import CustomUser


class NormalizedIdentityFormMixin:
    """Case-insensitive uniqueness errors instead of database IntegrityErrors."""

    def _others(self):
        qs = CustomUser.objects.all()
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        return qs

    def clean_email(self):
        email = normalize_email(self.cleaned_data.get("email"))
        if email and self._others().filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_matric_number(self):
        matric = normalize_matric(self.cleaned_data.get("matric_number"))
        if matric and self._others().filter(matric_number__iexact=matric).exists():
            raise forms.ValidationError("An account with this matric number already exists.")
        return matric


class CustomUserCreationForm(NormalizedIdentityFormMixin, UserCreationForm):
    """
    form for creating new users
    """

    class Meta:
        model = CustomUser
        fields = (
            "email",
            "first_name",
            "last_name",
            "matric_number",
            "department",
            "level",
            "role",
        )


class CustomUserChangeForm(NormalizedIdentityFormMixin, UserChangeForm):
    """
    form for updating users.

    The Django admin change view runs inside one transaction, so the row locks
    taken here hold until the save commits (D1 admin-retention invariants).
    """

    actor = None  # set by CustomUserAdmin.get_form

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and ("is_active" in cleaned or "role" in cleaned):
            from pythagoras.utils import APIError
            locked = student_admin.lock_status_scope(self.instance.pk)
            if locked is not None:
                try:
                    student_admin.assert_status_change_allowed(
                        locked, self.actor, cleaned.get("is_active", locked.is_active),
                        cleaned.get("role", locked.role))
                except APIError as exc:
                    raise forms.ValidationError(str(exc.detail), code=exc.code.lower())
        return cleaned

    class Meta:
        model = CustomUser
        fields = (
            "email",
            "first_name",
            "last_name",
            "matric_number",
            "department",
            "level",
            "role",
            "is_active",
            "is_staff",
        )
