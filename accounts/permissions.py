from rest_framework.permissions import BasePermission


class IsAdmin(BasePermission):
    """
    Permission class that only allows admin users.
    """

    message = "You must be an admin to perform this action."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "admin"
        )


class IsStudent(BasePermission):
    """
    Permission class that only allows student users.
    """

    message = "You must be a student to perform this action."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "student"
        )


class IsAdminOrReadOnly(BasePermission):
    """
    Permission class that allows read-only access to all authenticated users,
    but only allows write access to admin users.
    """

    message = "You must be an admin to modify this resource."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True

        return request.user.role == "admin"


class IsOwnerOrAdmin(BasePermission):
    """
    Permission class that allows access to the owner of an object or admin users.
    Requires the view to have a `get_object` method that returns an object
    with a `user` attribute or is the user itself.
    """

    message = "You can only access your own resources unless you are an admin."

    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False

        if request.user.role == "admin":
            return True

        if obj == request.user:
            return True

        if hasattr(obj, "user"):
            return obj.user == request.user

        return False
