"""Publication state transitions and safe deletion (D2).

Transitions lock rows with ``select_for_update`` inside a transaction. Lock
order is always event, then resource (then upload intent, see ``uploads``).
Provider I/O (publication promotion, cleanup) never runs while rows are locked.
"""
from django.db import transaction
from django.db.models import ProtectedError, RestrictedError
from django.shortcuts import get_object_or_404
from django.utils import timezone

from pythagoras.utils import APIError

from . import uploads
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


def _check_event_publishable(event):
    if event.status == PUBLISHED:
        raise invalid_transition('event', event.status, 'published')
    errors = event.publication_errors()
    if errors:
        raise APIError('EVENT_NOT_PUBLISHABLE',
                       'Correct the event details before publishing.',
                       status_code=409, fields=errors)


def _needs_promotion(resource):
    return (resource.status == PUBLISHED and resource.type in EventResource.UPLOADED_TYPES
            and resource.has_asset and not resource.public_object_key)


def publish_event(event_id):
    """Publish the event. Its published uploaded resources lost their public
    copies at event unpublication, so they are re-promoted to *new* public keys
    (no locks held during the copies) and attached atomically with the event's
    status. All or nothing: a failed promotion leaves the event unpublished."""
    with transaction.atomic():
        event = _lock_event(event_id)
        _check_event_publishable(event)
        pending = [r for r in event.resources.select_for_update().order_by('id') if _needs_promotion(r)]
    promoted = {}  # resource id -> (asset, key, url, cleanup row id)
    try:
        for resource in pending:
            key, url, row_id = uploads.promote_to_public(resource)
            promoted[resource.pk] = ((resource.private_object_key, resource.etag), key, url, row_id)
        with transaction.atomic():
            event = _lock_event(event_id)
            _check_event_publishable(event)
            attached = []
            for resource in event.resources.select_for_update().order_by('id'):
                entry = promoted.get(resource.pk)
                if entry is None:
                    if _needs_promotion(resource):
                        raise APIError('MEDIA_CHANGED', 'Event content changed while publishing. Try again.',
                                       status_code=409)
                    continue
                asset, key, url, row_id = entry
                if not _needs_promotion(resource) or (resource.private_object_key, resource.etag) != asset:
                    continue  # changed meanwhile: its unused copy stays recorded and is abandoned below
                resource.public_object_key, resource.public_media_url = key, url
                resource.save(update_fields=['public_object_key', 'public_media_url', 'updated_at'])
                attached.append(row_id)
            event.status = PUBLISHED
            event.published_at = timezone.now()
            event.save(update_fields=['status', 'published_at', 'updated_at'])
            uploads.promotion_attached(attached)
    finally:
        # Every copy not cancelled as attached above becomes eligible for cleanup.
        uploads.promotion_abandoned([entry[3] for entry in promoted.values()], 'publish_aborted')
    return event


def unpublish_event(event_id):
    """Hide the event and, through visibility rules, all of its resources.

    Resource publication states are not rewritten (D2), but their public
    copies are recorded for deletion/purge and their media_url cleared;
    re-publishing the event re-promotes them."""
    with transaction.atomic():
        event = _lock_event(event_id)
        if event.status != PUBLISHED:
            raise invalid_transition('event', event.status, 'unpublished')
        cleanup_ids = []
        for resource in event.resources.select_for_update().order_by('id'):
            row_id = uploads.retire_public_copy(resource, 'event_unpublished')
            if row_id:
                resource.save(update_fields=['public_object_key', 'public_media_url', 'updated_at'])
                cleanup_ids.append(row_id)
        event.status = UNPUBLISHED
        event.save(update_fields=['status', 'updated_at'])
        uploads.cleanup_after_commit(cleanup_ids)
    return event


def _lock_resource(event_id, resource_id):
    event = _lock_event(event_id)
    resource = get_object_or_404(EventResource.objects.select_for_update(), pk=resource_id, event_id=event.pk)
    return event, resource


def _check_resource_publishable(event, resource):
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


def publish_resource(event_id, resource_id):
    """Check, promote the validated bytes to a new public key (no locks held),
    then re-check and record visibility atomically. The new key's cleanup row
    is recorded before the copy; it is cancelled on success and made eligible
    on any failure, which leaves the resource unpublished."""
    with transaction.atomic():
        event, resource = _lock_resource(event_id, resource_id)
        _check_resource_publishable(event, resource)
        uploaded = resource.type in EventResource.UPLOADED_TYPES
        asset = (resource.private_object_key, resource.etag)

    public_key = public_url = ''
    row_id = None
    if uploaded:
        public_key, public_url, row_id = uploads.promote_to_public(resource)

    try:
        with transaction.atomic():
            event, resource = _lock_resource(event_id, resource_id)
            _check_resource_publishable(event, resource)
            if uploaded and (resource.private_object_key, resource.etag) != asset:
                raise APIError('MEDIA_CHANGED', 'The audio changed while publishing. Try again.', status_code=409)
            fields = ['status', 'published_at', 'updated_at']
            if uploaded:
                stale = uploads.retire_public_copy(resource, 'replaced')
                resource.public_object_key, resource.public_media_url = public_key, public_url
                fields += ['public_object_key', 'public_media_url']
                uploads.cleanup_after_commit([stale])
            resource.status = PUBLISHED
            resource.published_at = timezone.now()
            resource.save(update_fields=fields)
            uploads.promotion_attached([row_id])
    finally:
        uploads.promotion_abandoned([row_id], 'publish_aborted')  # no-op once cancelled as attached
    resource.event = event
    return resource


def unpublish_resource(event_id, resource_id):
    """Hide immediately; the public copy is recorded for deletion (already
    shared/cached bytes cannot be recalled)."""
    with transaction.atomic():
        event, resource = _lock_resource(event_id, resource_id)
        if resource.status != PUBLISHED:
            raise invalid_transition('resource', resource.status, 'unpublished')
        resource.status = UNPUBLISHED
        fields = ['status', 'updated_at']
        row_id = uploads.retire_public_copy(resource, 'unpublished')
        if row_id:
            fields += ['public_object_key', 'public_media_url']
            uploads.cleanup_after_commit([row_id])
        resource.save(update_fields=fields)
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
        if 'youtube_video_id' in data and resource.status == PUBLISHED:
            raise APIError('UNPUBLISH_REQUIRED', 'Unpublish this resource before changing its video link.',
                           status_code=409)
        if 'type' in data and data['type'] != resource.type:
            uploads.cleanup_after_commit(uploads.supersede_open_intents(resource, reason='RESOURCE_TYPE_CHANGED'))
            resource.storage_status = EventResource.initial_storage_status(data['type'])
            extra.append('storage_status')
            if data['type'] != EventResource.Type.VIDEO and resource.youtube_video_id:
                data.setdefault('youtube_video_id', '')  # a link belongs to video resources only
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
        cleanup_ids = []
        for resource in event.resources.select_for_update().order_by('id'):
            cleanup_ids += uploads.record_resource_cleanup(resource, 'resource_deleted')
        try:
            # Legacy local files are left on disk; Django does not delete them.
            event.delete()
        except (ProtectedError, RestrictedError):
            raise referenced_error(event_references(event)) from None
        uploads.cleanup_after_commit(cleanup_ids)


def delete_resource(event_id, resource_id):
    with transaction.atomic():
        _, resource = _lock_resource(event_id, resource_id)
        if resource.status == PUBLISHED:
            raise unpublish_required('resource')
        cleanup_ids = uploads.record_resource_cleanup(resource, 'resource_deleted')
        resource.delete()
        uploads.cleanup_after_commit(cleanup_ids)
