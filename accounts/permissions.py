"""Compatibility imports; shared product permission definitions live in pythagoras."""
from pythagoras.permissions import IsAdmin, IsAdminOrReadOnly, IsOwnerOrAdmin, IsStudent

__all__ = ['IsAdmin', 'IsAdminOrReadOnly', 'IsOwnerOrAdmin', 'IsStudent']
