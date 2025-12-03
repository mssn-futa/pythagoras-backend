from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.translation import gettext_lazy as _

from .forms import CustomUserCreationForm, CustomUserChangeForm
from .models import CustomUser


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    """
    Custom admin for CustomUser model.
    """

    add_form = CustomUserCreationForm
    form = CustomUserChangeForm
    model = CustomUser

    list_display = (
        "email",
        "first_name",
        "last_name",
        "matric_number",
        "department",
        "level",
        "is_staff",
        "is_active",
    )
    list_filter = ("is_staff", "is_active", "level", "department")
    search_fields = ("email", "first_name", "last_name", "matric_number")
    ordering = ("email",)

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            _("Personal info"),
            {"fields": ("first_name", "last_name", "matric_number", "department", "level")},
        ),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "first_name",
                    "last_name",
                    "matric_number",
                    "department",
                    "level",
                    "password1",
                    "password2",
                    "is_staff",
                    "is_active",
                ),
            },
        ),
    )
