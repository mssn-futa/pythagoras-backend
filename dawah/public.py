"""Stable read interface for other domains (e.g. D3 Arabic modules, ASSESS-07).

Consumers get exactly what an anonymous reader of ``GET /dawah/resources/{id}/``
gets: only currently visible resources (published, parent event published,
deliverable asset/reference) in the public payload shape, with no admin-only
fields, private keys or signed URLs. Module/enrollment authorization stays with
the caller; it can never widen resource visibility.
"""
from .models import EventResource
from .serializers import EventResourceSerializer


def visible_resources(resource_ids=None):
    """Visible resources (optionally limited to ``resource_ids``), ordered by id."""
    queryset = EventResource.objects.visible().select_related('event')
    if resource_ids is not None:
        queryset = queryset.filter(pk__in=list(resource_ids))
    return queryset.order_by('id')


def visible_resource_ids(resource_ids):
    """The subset of ``resource_ids`` that is currently visible."""
    return set(visible_resources(resource_ids).values_list('pk', flat=True))


def public_resource_payloads(resources):
    """Public payloads (as ``GET /dawah/resources/{id}/`` returns them) for visible resources."""
    return EventResourceSerializer(list(resources), many=True, context={'is_admin': False}).data
