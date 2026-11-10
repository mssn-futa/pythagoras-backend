"""Shared product permissions for domain apps (D0 roles).

Import from here or ``pythagoras.permissions``; both name the same classes.
All require an authenticated *active* account. ``IsAdmin`` checks
``role == 'admin'``; ``is_staff`` is Django-admin access only.
"""
from pythagoras.permissions import (
    IsActiveUser, IsAdmin, IsAdminOrReadOnly, IsOwnerOrAdmin, IsStudent, is_active_user,
)

# Descriptive aliases for new domain code.
IsActiveAdmin = IsAdmin
IsActiveStudent = IsStudent

__all__ = ['IsActiveAdmin', 'IsActiveStudent', 'IsActiveUser', 'IsAdmin', 'IsAdminOrReadOnly',
           'IsOwnerOrAdmin', 'IsStudent', 'is_active_user']
