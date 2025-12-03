from django import forms
from django.contrib.auth.forms import UserCreationForm, UserChangeForm

from .models import CustomUser


class CustomUserCreationForm(UserCreationForm):
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
        )


class CustomUserChangeForm(UserChangeForm):
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
            "is_active",
            "is_staff",
        )
