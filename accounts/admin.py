from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.contrib.auth.admin import UserAdmin
from django.utils.translation import gettext_lazy as _

from . import student_admin
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

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is None:
            return form
        return type(form.__name__, (form,), {"actor": request.user})

    def _deletion_refused(self, request, pks):
        """Lock the scope and return an error message if deleting ``pks`` breaks
        the admin-retention rules (must run inside transaction.atomic)."""
        from pythagoras.utils import APIError
        pks = list(pks)
        if not pks:
            return None
        student_admin.lock_status_scope(pks[0], *pks[1:])
        try:
            student_admin.assert_deletion_allowed(pks, request.user)
        except APIError as exc:
            return str(exc.detail)
        return None

    def delete_view(self, request, object_id, extra_context=None):
        with transaction.atomic():
            if request.method == "POST" and object_id.isdigit():
                error = self._deletion_refused(request, [int(object_id)])
                if error:
                    self.message_user(request, error, messages.ERROR)
                    return HttpResponseRedirect(
                        reverse("admin:accounts_customuser_change", args=[object_id]))
            return super().delete_view(request, object_id, extra_context)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if "delete_selected" in actions:
            _, name, description = actions["delete_selected"]

            def protected_delete_selected(modeladmin, request, queryset):
                with transaction.atomic():
                    error = modeladmin._deletion_refused(
                        request, queryset.values_list("pk", flat=True))
                    if error:
                        modeladmin.message_user(
                            request, f"{error} Nothing was deleted.", messages.ERROR)
                        return None
                    return delete_selected(modeladmin, request, queryset)

            actions["delete_selected"] = (protected_delete_selected, name, description)
        return actions

    list_display = (
        "email",
        "first_name",
        "last_name",
        "matric_number",
        "department",
        "level",
        "role",
        "is_staff",
        "is_active",
    )
    list_filter = ("role", "is_staff", "is_active", "level", "department")
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
                    "role",
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
                    "role",
                    "is_staff",
                    "is_active",
                ),
            },
        ),
    )
