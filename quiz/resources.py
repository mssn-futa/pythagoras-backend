"""Adapter to the Da'wah published-resource interface (D2) used by learning modules.

The only place quiz reads dawah resource visibility/payloads. It delegates to
``dawah.public`` (DAWAH-07), the stable cross-domain read interface: only
currently visible resources, in exactly the anonymous ``GET /dawah/resources/{id}/``
shape. Module/enrollment authorization stays with the caller.
"""
from dawah import public as dawah_public
from dawah.serializers import EventResourceSerializer

# Schema component for module ``resources[]`` (the public resource payload).
PublicResourceSerializer = EventResourceSerializer


def visible_resource_ids(ids):
    """The subset of ``ids`` that any reader may currently discover."""
    return dawah_public.visible_resource_ids(ids)


def public_resource_payloads(resources):
    """Exactly the anonymous ``GET /dawah/resources/{id}/`` data for each resource
    (callers pass visible resources only, with ``event`` selected)."""
    return dawah_public.public_resource_payloads(resources)
