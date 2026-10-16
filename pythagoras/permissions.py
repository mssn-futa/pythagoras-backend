"""Product API permissions. is_staff is reserved for Django admin."""
from rest_framework.permissions import BasePermission, SAFE_METHODS


def is_active_user(user):
    return bool(user and user.is_authenticated and user.is_active)


class IsAdmin(BasePermission):
    message = 'You must be an admin to perform this action.'

    def has_permission(self, request, view):
        return is_active_user(request.user) and request.user.role == 'admin'


class IsStudent(BasePermission):
    message = 'You must be a student to perform this action.'

    def has_permission(self, request, view):
        return is_active_user(request.user) and request.user.role == 'student'


class IsAdminOrReadOnly(BasePermission):
    message = 'You must be an admin to modify this resource.'

    def has_permission(self, request, view):
        return is_active_user(request.user) and (
            request.method in SAFE_METHODS or request.user.role == 'admin'
        )


class IsOwnerOrAdmin(BasePermission):
    message = 'You can only access your own resources unless you are an admin.'

    def has_permission(self, request, view):
        return is_active_user(request.user)

    def has_object_permission(self, request, view, obj):
        return self.has_permission(request, view) and (
            request.user.role == 'admin' or obj == request.user
            or getattr(obj, 'user_id', None) == request.user.pk
        )
