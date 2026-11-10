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


def lock_status_scope(target_pk, *more_pks):
    """Lock the target row and every active product-admin row in one ordered
    statement (consistent lock order, so concurrent status changes serialize
    instead of deadlocking). Must run inside ``transaction.atomic``.
    Returns the locked target or None when it does not exist."""
    rows = list(
        User.objects.select_for_update()
        .filter(Q(pk__in=[target_pk, *more_pks]) | Q(role=User.Role.ADMIN, is_active=True))
        .order_by("pk")
    )
    return next((row for row in rows if row.pk == target_pk), None)


SELF_DEMOTION = "SELF_DEMOTION"


def assert_status_change_allowed(target, actor, make_active, new_role=None):
    """Raise a 409 APIError if the change would deactivate the acting admin, demote
    the acting admin, or remove the last active product admin (by deactivation or
    by changing its role). ``target`` is the locked, currently stored row; call
    with rows locked by lock_status_scope."""
    demoting = new_role is not None and new_role != User.Role.ADMIN
    if not target.is_active:
        return
    if not make_active and actor is not None and target.pk == actor.pk:
        raise APIError(SELF_DEACTIVATION, "You cannot deactivate your own account.", status_code=409)
    if demoting and target.role == User.Role.ADMIN and actor is not None and target.pk == actor.pk:
        raise APIError(SELF_DEMOTION, "You cannot remove your own administrator role.", status_code=409)
    leaving = target.role == User.Role.ADMIN and (not make_active or demoting)
    if leaving:
        others = User.objects.filter(role=User.Role.ADMIN, is_active=True).exclude(pk=target.pk)
        if not others.exists():
            raise APIError(
                LAST_ADMIN, "The last active administrator cannot be deactivated or demoted.",
                status_code=409,
            )


SELF_DELETION = "SELF_DELETION"


def assert_deletion_allowed(pks, actor):
    """Refuse deleting the acting admin or every remaining active admin.
    Call inside ``transaction.atomic`` after ``lock_status_scope(*pks)``; the
    whole set is refused if any member violates the rule."""
    pks = set(pks)
    if actor is not None and actor.pk in pks:
        raise APIError(SELF_DELETION, "You cannot delete your own account.", status_code=409)
    active_admins = set(
        User.objects.filter(role=User.Role.ADMIN, is_active=True).values_list("pk", flat=True)
    )
    if active_admins & pks and not active_admins - pks:
        raise APIError(
            LAST_ADMIN, "The last active administrator cannot be deleted.", status_code=409
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
