from django import forms
from django.contrib.auth.forms import UserCreationForm, UserChangeForm

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
            "is_active",
            "is_staff",
        )
