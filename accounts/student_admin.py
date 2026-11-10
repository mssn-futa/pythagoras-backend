"""Student administration domain logic (PLAT-05): lookup, filtering and the
serialized status-change protection shared by every operation that can change
an account's active state."""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q, Value
from django.db.models.functions import Concat

from pythagoras.utils import APIError

from .identity import normalize_department

User = get_user_model()

SELF_DEACTIVATION = "SELF_DEACTIVATION"
LAST_ADMIN = "LAST_ADMIN"


def students():
    return User.objects.filter(role=User.Role.STUDENT)


def lock_status_scope(target_pk):
    """Lock the target row and every active product-admin row in one ordered
    statement (consistent lock order, so concurrent status changes serialize
    instead of deadlocking). Must run inside ``transaction.atomic``.
    Returns the locked target or None when it does not exist."""
    rows = list(
        User.objects.select_for_update()
        .filter(Q(pk=target_pk) | Q(role=User.Role.ADMIN, is_active=True))
        .order_by("pk")
    )
    return next((row for row in rows if row.pk == target_pk), None)


def assert_status_change_allowed(target, actor, make_active):
    """Raise a 409 APIError if the change would deactivate the acting admin or
    the last active product admin. Call with rows locked by lock_status_scope."""
    if make_active or not target.is_active:
        return
    if actor is not None and target.pk == actor.pk:
        raise APIError(SELF_DEACTIVATION, "You cannot deactivate your own account.", status_code=409)
    if target.role == User.Role.ADMIN:
        others = User.objects.filter(role=User.Role.ADMIN, is_active=True).exclude(pk=target.pk)
        if not others.exists():
            raise APIError(
                LAST_ADMIN, "The last active administrator cannot be deactivated.", status_code=409
            )


def set_account_active(target_pk, make_active, actor=None):
    """Atomically activate/deactivate any account with the D1 protections.
    Returns the updated user, or None if it does not exist."""
    with transaction.atomic():
        target = lock_status_scope(target_pk)
        if target is None:
            return None
        assert_status_change_allowed(target, actor, make_active)
        if target.is_active != make_active:
            target.is_active = make_active
            target.save(update_fields=["is_active"])
        return target


def filter_students(queryset, params):
    """Apply validated list filters (see StudentListFilterSerializer)."""
    search = params.get("search")
    if search:
        queryset = queryset.annotate(
            full_name=Concat("first_name", Value(" "), "last_name")
        )
        for term in search.split():
            queryset = queryset.filter(
                Q(full_name__icontains=term)
                | Q(email__icontains=term)
                | Q(matric_number__icontains=term)
            )
    department = params.get("department")
    if department:
        queryset = queryset.filter(department__iexact=normalize_department(department))
    if params.get("level"):
        queryset = queryset.filter(level=params["level"])
    if params.get("is_active") is not None:
        queryset = queryset.filter(is_active=params["is_active"])
    return queryset.order_by("last_name", "first_name", "id")
