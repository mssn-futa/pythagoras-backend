"""Adapter to the Da'wah published-resource interface (D2) used by learning modules.

The only place quiz reads dawah resource visibility/payloads. Today it uses
``EventResource.objects.visible()`` and the public ``EventResourceSerializer``
(rendered for an anonymous reader). When ``dawah.public`` (DAWAH-07) lands,
these functions delegate to its ``visible_resource_ids`` /
``public_resource_payloads`` without any change to callers.
"""
from dawah.models import EventResource
from dawah.serializers import EventResourceSerializer

# Schema component for module ``resources[]`` (the public resource payload).
PublicResourceSerializer = EventResourceSerializer


def visible_resource_ids(ids):
    """The subset of ``ids`` that any reader may currently discover."""
    return set(EventResource.objects.visible().filter(pk__in=list(ids))
               .values_list('pk', flat=True))


def public_resource_payloads(resources):
    """Exactly the anonymous ``GET /dawah/resources/{id}/`` data for each resource
    (callers pass visible resources only, with ``event`` selected)."""
    return EventResourceSerializer(list(resources), many=True, context={'is_admin': False}).data
