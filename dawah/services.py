"""Publication state transitions and safe deletion (D2).

Transitions lock rows with ``select_for_update`` inside a transaction. Lock
order is always event, then resource.
"""
from django.db import transaction
from django.db.models import ProtectedError, RestrictedError
from django.shortcuts import get_object_or_404
from django.utils import timezone

from pythagoras.utils import APIError

from .models import Event, EventResource, PublicationStatus

PUBLISHED = PublicationStatus.PUBLISHED
UNPUBLISHED = PublicationStatus.UNPUBLISHED
REFERENCE_ID_LIMIT = 20


def invalid_transition(kind, current, target):
    return APIError('INVALID_STATE_TRANSITION',
                    f'A {current} {kind} cannot be {target}.', status_code=409)


def unpublish_required(kind):
    return APIError('UNPUBLISH_REQUIRED',
                    f'Unpublish this {kind} before deleting it.', status_code=409)


def _lock_event(event_id):
    return get_object_or_404(Event.objects.select_for_update(), pk=event_id)


def publish_event(event_id):
    with transaction.atomic():
        event = _lock_event(event_id)
        if event.status == PUBLISHED:
            raise invalid_transition('event', event.status, 'published')
        errors = event.publication_errors()
        if errors:
            raise APIError('EVENT_NOT_PUBLISHABLE',
                           'Correct the event details before publishing.',
                           status_code=409, fields=errors)
        event.status = PUBLISHED
        event.published_at = timezone.now()
        event.save(update_fields=['status', 'published_at', 'updated_at'])
    return event


def unpublish_event(event_id):
    """Hide the event and, through visibility rules, all of its resources.

    Resource states are intentionally not rewritten (D2)."""
    with transaction.atomic():
        event = _lock_event(event_id)
        if event.status != PUBLISHED:
            raise invalid_transition('event', event.status, 'unpublished')
        event.status = UNPUBLISHED
        event.save(update_fields=['status', 'updated_at'])
    return event


def _lock_resource(event_id, resource_id):
    event = _lock_event(event_id)
    resource = get_object_or_404(EventResource.objects.select_for_update(), pk=resource_id, event_id=event.pk)
    return event, resource


def publish_resource(event_id, resource_id):
    with transaction.atomic():
        event, resource = _lock_resource(event_id, resource_id)
        if resource.status == PUBLISHED:
            raise invalid_transition('resource', resource.status, 'published')
        if event.status != PUBLISHED:
            raise APIError('EVENT_NOT_PUBLISHED',
                           'Publish the parent event before publishing its resources.',
                           status_code=409)
        if not resource.title.strip():
            raise APIError('RESOURCE_NOT_PUBLISHABLE', 'Correct the resource details before publishing.',
                           status_code=409, fields={'title': ['This field is required for publication.']})
        if not resource.media_ready():
            if resource.type == EventResource.Type.VIDEO:
                message = 'A validated video reference is required before publishing.'
            else:
                message = 'A ready uploaded asset is required before publishing.'
            raise APIError('MEDIA_NOT_READY', message, status_code=409)
        resource.status = PUBLISHED
        resource.published_at = timezone.now()
        resource.save(update_fields=['status', 'published_at', 'updated_at'])
    resource.event = event
    return resource


def unpublish_resource(event_id, resource_id):
    with transaction.atomic():
        event, resource = _lock_resource(event_id, resource_id)
        if resource.status != PUBLISHED:
            raise invalid_transition('resource', resource.status, 'unpublished')
        resource.status = UNPUBLISHED
        resource.save(update_fields=['status', 'updated_at'])
    resource.event = event
    return resource


def apply_update(instance, validated_data, extra_fields=()):
    """Write only the validated fields, so state columns are never rewritten
    from a stale copy (an overlapping publish/unpublish keeps its result)."""
    for name, value in validated_data.items():
        setattr(instance, name, value)
    instance.save(update_fields=[*validated_data, *extra_fields, 'updated_at'])
    return instance


def update_event(event_id, serializer_factory):
    """Validate against and update the locked current row."""
    with transaction.atomic():
        event = _lock_event(event_id)
        serializer = serializer_factory(event)
        serializer.is_valid(raise_exception=True)
        apply_update(event, serializer.validated_data)
    return event


def update_resource(event_id, resource_id, serializer_factory):
    with transaction.atomic():
        event, resource = _lock_resource(event_id, resource_id)
        serializer = serializer_factory(resource)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        extra = []
        if 'type' in data and data['type'] != resource.type:
            resource.storage_status = EventResource.initial_storage_status(data['type'])
            extra.append('storage_status')
        apply_update(resource, data, extra)
    resource.event = event
    return resource


def create_resource(event_id, serializer, created_by):
    """Create under the locked parent, so a racing event delete cannot leave a
    dangling insert (it waits, then this request sees 404)."""
    with transaction.atomic():
        event = _lock_event(event_id)
        data = serializer.validated_data
        return EventResource.objects.create(
            event=event, created_by=created_by,
            storage_status=EventResource.initial_storage_status(data['type']), **data)


def event_references(event):
    """Rows in other domains (e.g. quizzes) that point at this event.

    Parent-owned resources are not references. Checked generically so the
    guard does not import or edit quiz models; ASSESS-02 adds DB protection.
    """
    references = {}
    for relation in Event._meta.related_objects:
        if relation.related_model is EventResource:
            continue
        model = relation.related_model
        ids = list(model._base_manager.filter(**{relation.field.name: event})
                   .order_by('pk').values_list('pk', flat=True)[:REFERENCE_ID_LIMIT])
        if ids:
            references[model._meta.label_lower] = ids
    return references


def referenced_error(references):
    return APIError('RESOURCE_REFERENCED',
                    'This event is referenced by other records (for example quizzes). '
                    'Remove those references before deleting it.',
                    status_code=409, fields={'references': references})


def delete_event(event_id):
    with transaction.atomic():
        event = _lock_event(event_id)
        if event.status == PUBLISHED:
            raise unpublish_required('event')
        if event.resources.filter(status=PUBLISHED).exists():
            raise APIError('UNPUBLISH_REQUIRED',
                           'Unpublish this event\'s published resources before deleting it.',
                           status_code=409)
        references = event_references(event)
        if references:
            raise referenced_error(references)
        try:
            # Legacy local files are left on disk; Django does not delete them.
            event.delete()
        except (ProtectedError, RestrictedError):
            raise referenced_error(event_references(event)) from None


def delete_resource(event_id, resource_id):
    with transaction.atomic():
        _, resource = _lock_resource(event_id, resource_id)
        if resource.status == PUBLISHED:
            raise unpublish_required('resource')
        # DAWAH-02 records provider cleanup for uploaded assets here.
        resource.delete()
