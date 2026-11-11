"""Durable audio/document/featured-image upload, finalize, promotion and cleanup (D2).

Protocol: intent → direct browser PUT (signed, private staging key) → finalize
(server-observed size/type/ETag, conditional copy to a server-only validation
key, bounded content validation of that immutable copy) → ready private asset →
publish (copy at ETag to a unique public key, then record the public URL).

Kinds (:data:`SPECS`): ``audio`` (MP3, DAWAH-02) and ``document`` (PDF/DOCX,
DAWAH-07) attach to an event resource; ``image`` (JPEG/PNG, DAWAH-07) is the
event's featured image. A featured image may be replaced while its event is
published: the new validated copy is promoted to a new public key *before* the
event's pointer is swapped, and the old copies are recorded for cleanup.

Database locks are short and never held across provider I/O. Lock order is
always event → resource → upload intent (DAWAH-01 order, extended). A DB lease
on the intent serializes validation; replay of a terminal outcome is read-only.
Provider and DB are not one transaction: every key that might be orphaned is
recorded in :class:`StorageCleanup` before or when it can become unreferenced.
Logs carry only IDs, statuses and fixed codes - never keys or signed URLs.
"""
from __future__ import annotations

import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pythagoras.utils import APIError

from . import documents, images, media
from .models import Event, EventResource, ImageStorageStatus, PublicationStatus, StorageCleanup, UploadIntent
from .storage import PRIVATE, PUBLIC, ObjectNotFound, PreconditionFailed, StorageError, get_storage
from .storage.purge import PurgeError, get_purger
from .storage.s3 import content_disposition

logger = logging.getLogger('dawah.uploads')

Status = UploadIntent.Status
Kind = UploadIntent.Kind
Storage = EventResource.StorageStatus
LEASE_SECONDS = 300
ABANDONED_AFTER = timedelta(days=1)
PROMOTION_GRACE = timedelta(hours=1)  # an in-flight public copy is not deleted before this
PUBLIC_CACHE_CONTROL = 'public, max-age=31536000, immutable'
DURATION_MAX = 32767  # EventResource.duration is a PositiveSmallIntegerField
MAX_INTENT_TTL = 900   # D2: 15-minute signed PUT; settings may shorten, never lengthen
MAX_PREVIEW_TTL = 900  # admin preview stays short-lived


def intent_ttl():
    return min(settings.R2_UPLOAD_INTENT_TTL_SECONDS, MAX_INTENT_TTL)


def preview_ttl():
    return min(settings.R2_PREVIEW_TTL_SECONDS, MAX_PREVIEW_TTL)


def media_upload_failed():
    """Sanitized provider failure. D2 names it MEDIA_UPLOAD_FAILED, but the shared
    renderer publishes every 5xx as the fixed ``503 PROVIDER_UNAVAILABLE``."""
    return APIError('PROVIDER_UNAVAILABLE', status_code=503)


def _token():
    return secrets.token_urlsafe(24)


def safe_file_name(name, extension='.mp3', accepted=None, fallback='audio'):
    """ASCII-safe display/download name derived from the client's file name.

    Any accepted extension is removed and the canonical ``extension`` appended."""
    base = re.split(r'[\\/]', name)[-1]
    stem = base
    for ext in accepted or (extension,):
        if base.lower().endswith(ext):
            stem = base[:-len(ext)]
            break
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', stem).strip('.-_')[:80] or fallback
    return f'{stem}{extension}'


@dataclass(frozen=True)
class KindSpec:
    """Per-kind upload rules. ``types`` maps each accepted MIME type to its
    canonical extension and the file-name extensions accepted for it."""
    kind: str
    types: dict
    max_bytes: int
    limit_label: str
    prefix: str  # private/public key prefix
    disposition: str  # Content-Disposition type of public/preview delivery
    resource_type: str | None
    fallback_name: str
    noun: str

    def extension(self, content_type):
        return self.types[content_type][0]

    def accepted_extensions(self, content_type):
        return self.types[content_type][1]

    def file_name(self, name, content_type):
        return safe_file_name(name, self.extension(content_type), self.accepted_extensions(content_type),
                              self.fallback_name)


SPECS = {
    Kind.AUDIO: KindSpec(
        kind=Kind.AUDIO, types={media.MP3_CONTENT_TYPE: ('.mp3', ('.mp3',))}, max_bytes=media.MP3_MAX_BYTES,
        limit_label='100 MiB audio', prefix='audio', disposition='inline', resource_type=EventResource.Type.AUDIO,
        fallback_name='audio', noun='audio'),
    Kind.DOCUMENT: KindSpec(
        kind=Kind.DOCUMENT,
        types={documents.PDF_CONTENT_TYPE: ('.pdf', ('.pdf',)),
               documents.DOCX_CONTENT_TYPE: ('.docx', ('.docx',))},
        max_bytes=documents.DOCUMENT_MAX_BYTES, limit_label='20 MiB document', prefix='documents',
        # Documents are always downloaded, never rendered inline from the media domain.
        disposition='attachment', resource_type=EventResource.Type.DOC, fallback_name='document',
        noun='document'),
    Kind.IMAGE: KindSpec(
        kind=Kind.IMAGE,
        types={images.JPEG_CONTENT_TYPE: ('.jpg', ('.jpg', '.jpeg')), images.PNG_CONTENT_TYPE: ('.png', ('.png',))},
        max_bytes=images.IMAGE_MAX_BYTES, limit_label='10 MiB image', prefix='images', disposition='inline',
        resource_type=None, fallback_name='image', noun='featured image'),
}
RESOURCE_KINDS = {spec.resource_type: spec for spec in SPECS.values() if spec.resource_type}


def spec_for_resource(resource):
    return RESOURCE_KINDS.get(resource.type, SPECS[Kind.AUDIO])


# ---------------------------------------------------------------- cleanup records

def record_cleanup(bucket, key, reason, *, resource_id=None, event_id=None, upload_id=None, public_url='',
                   not_before=None):
    if not key:
        return None
    return StorageCleanup.objects.create(bucket=bucket, key=key, reason=reason, resource_id=resource_id,
                                         event_id=event_id, upload_id=upload_id, public_url=public_url,
                                         not_before=not_before)


def _owner(intent):
    """Cleanup-row owner fields of an intent's target."""
    return {'resource_id': intent.resource_id, 'event_id': intent.event_id}


def retire_public_copy(resource, reason):
    """On a locked resource: record its public copy (and URL to purge) for
    deletion and clear the pointer. Caller saves the fields. Returns row id."""
    if not resource.public_object_key:
        return None
    row = record_cleanup(PUBLIC, resource.public_object_key, reason, resource_id=resource.pk,
                         public_url=resource.public_media_url)
    resource.public_object_key = resource.public_media_url = ''
    return row.pk


def retire_event_image(event, reason):
    """On a locked event: record its featured image's public copy (and URL to
    purge) for deletion and clear the pointer. Caller saves. Returns row id."""
    if not event.featured_image_public_key:
        return None
    row = record_cleanup(PUBLIC, event.featured_image_public_key, reason, event_id=event.pk,
                         public_url=event.featured_image_public_url)
    event.featured_image_public_key = event.featured_image_public_url = ''
    return row.pk


IMAGE_POINTER_FIELDS = ['featured_image_public_key', 'featured_image_public_url']


def record_event_image_cleanup(event, reason):
    """Record every featured-image object a deleted event leaves behind."""
    ids = []
    for bucket, key, url in ((PRIVATE, event.featured_image_object_key, ''),
                             (PUBLIC, event.featured_image_public_key, event.featured_image_public_url)):
        row = record_cleanup(bucket, key, reason, event_id=event.pk, public_url=url)
        if row:
            ids.append(row.pk)
    for intent in event.image_upload_intents.all():
        for key in (intent.staging_key, intent.validation_key):
            if key != event.featured_image_object_key:
                ids.append(record_cleanup(PRIVATE, key, reason, event_id=event.pk, upload_id=intent.pk).pk)
    return ids


def record_resource_cleanup(resource, reason):
    """Record every provider object a deleted resource leaves behind."""
    ids = []
    for bucket, key, url in ((PRIVATE, resource.private_object_key, ''),
                             (PUBLIC, resource.public_object_key, resource.public_media_url)):
        row = record_cleanup(bucket, key, reason, resource_id=resource.pk, public_url=url)
        if row:
            ids.append(row.pk)
    for intent in resource.upload_intents.all():
        for key in (intent.staging_key, intent.validation_key):
            if key != resource.private_object_key:
                ids.append(record_cleanup(PRIVATE, key, reason, resource_id=resource.pk,
                                          upload_id=intent.pk).pk)
    return ids


def _referenced(row):
    if row.bucket == StorageCleanup.Bucket.PUBLIC:
        return (EventResource.objects.filter(public_object_key=row.key).exists()
                or Event.objects.filter(featured_image_public_key=row.key).exists())
    # An unfinished intent's validation copy may be retried, whatever its lease.
    return (EventResource.objects.filter(private_object_key=row.key).exists()
            or Event.objects.filter(featured_image_object_key=row.key).exists()
            or UploadIntent.objects.filter(validation_key=row.key, status__in=UploadIntent.OPEN).exists())


def process_cleanup(*, ids=None, limit=100):
    """Delete recorded orphans, then purge their public URLs from the CDN.

    Returns counts; never deletes referenced keys or rows still in their grace period.
    """
    storage = get_storage()
    counts = {'done': 0, 'skipped': 0, 'failed': 0}
    rows = StorageCleanup.objects.filter(Q(not_before__isnull=True) | Q(not_before__lte=timezone.now()),
                                         status=StorageCleanup.Status.PENDING)
    if ids is not None:
        rows = rows.filter(pk__in=ids)
    for row in rows.order_by('id')[:limit]:
        now = timezone.now()
        if _referenced(row):
            StorageCleanup.objects.filter(pk=row.pk).update(status=StorageCleanup.Status.SKIPPED, processed_at=now)
            counts['skipped'] += 1
            continue
        try:
            storage.delete(row.key, bucket=row.bucket)
        except StorageError as exc:
            StorageCleanup.objects.filter(pk=row.pk).update(attempts=row.attempts + 1, last_error_code=exc.code)
            logger.warning('storage cleanup failed', extra={'cleanup_id': row.pk, 'error_code': exc.code})
            counts['failed'] += 1
            continue
        if row.bucket == StorageCleanup.Bucket.PUBLIC and row.public_url:
            try:
                get_purger().purge([row.public_url])
            except PurgeError as exc:
                # Deleted but maybe still cached: keep pending so the purge is retried.
                StorageCleanup.objects.filter(pk=row.pk).update(attempts=row.attempts + 1,
                                                               last_error_code=exc.code)
                logger.warning('cache purge failed', extra={'cleanup_id': row.pk, 'error_code': exc.code})
                counts['failed'] += 1
                continue
        StorageCleanup.objects.filter(pk=row.pk).update(status=StorageCleanup.Status.DONE, processed_at=now,
                                                       attempts=row.attempts + 1, last_error_code='')
        counts['done'] += 1
    return counts


def cleanup_after_commit(ids):
    """Best-effort immediate processing; failures stay recorded for the command."""
    ids = [i for i in ids if i]
    if not ids:
        return

    def run():
        try:
            process_cleanup(ids=ids)
        except Exception:  # noqa: BLE001 - must never fail the committed request
            logger.warning('immediate storage cleanup deferred', extra={'cleanup_ids': ids})

    transaction.on_commit(run)


# ---------------------------------------------------------------- storage status

def refresh_storage_status(resource, save=True):
    """Derive the resource's upload status from its asset and intents."""
    if resource.type not in EventResource.UPLOADED_TYPES:
        return
    if resource.private_object_key:
        status = Storage.READY
    else:
        latest = resource.upload_intents.order_by('-id').first()
        if latest is None:
            status = Storage.NONE
        elif latest.status in UploadIntent.OPEN:
            status = Storage.PENDING
        else:
            status = Storage.FAILED
    if resource.storage_status != status:
        resource.storage_status = status
        if save:
            resource.save(update_fields=['storage_status', 'updated_at'])


def refresh_image_status(event, save=True):
    """Derive the event's featured-image status from its asset and image intents."""
    if event.featured_image_object_key:
        status = ImageStorageStatus.READY
    else:
        latest = event.image_upload_intents.order_by('-id').first()
        if latest is None:
            status = ImageStorageStatus.NONE
        elif latest.status in UploadIntent.OPEN:
            status = ImageStorageStatus.PENDING
        else:
            status = ImageStorageStatus.FAILED
    if event.featured_image_storage_status != status:
        event.featured_image_storage_status = status
        if save:
            event.save(update_fields=['featured_image_storage_status', 'updated_at'])


def _refresh_target(intent, event, resource):
    if intent.kind == Kind.IMAGE:
        refresh_image_status(event)
    else:
        refresh_storage_status(resource)


def supersede_open_intents(resource, reason='SUPERSEDED'):
    """Fail every unfinished intent of the (locked) resource; returns cleanup ids."""
    return _supersede(UploadIntent.objects.filter(resource=resource), reason)


def supersede_open_image_intents(event, reason='SUPERSEDED'):
    """Fail every unfinished featured-image intent of the (locked) event."""
    return _supersede(UploadIntent.objects.filter(event=event, kind=Kind.IMAGE), reason)


def _supersede(queryset, reason):
    ids = []
    for intent in queryset.select_for_update().filter(status__in=UploadIntent.OPEN):
        intent.status = Status.FAILED
        intent.error_code = reason
        intent.error_message = 'A newer upload replaced this one.'
        intent.lease_token = ''
        intent.lease_expires_at = None
        intent.finalized_at = timezone.now()
        intent.save()
        for key in (intent.staging_key, intent.validation_key):
            ids.append(record_cleanup(PRIVATE, key, 'superseded', upload_id=intent.pk, **_owner(intent)).pk)
    return ids


# ---------------------------------------------------------------- intent

def _sign(spec, content_type, size_bytes, log_extra):
    """Unpredictable keys and a signed staging PUT (local computation, before any row)."""
    storage = get_storage()
    ttl = intent_ttl()
    expires_at = timezone.now() + timedelta(seconds=ttl)
    staging_key = f'staging/{spec.prefix}/{_token()}'
    validation_key = f'media/{spec.prefix}/{_token()}{spec.extension(content_type)}'
    try:
        upload_url = storage.presign_put(staging_key, content_type=content_type, content_length=size_bytes,
                                         expires_in=ttl)
    except StorageError as exc:
        logger.warning('upload intent signing failed', extra={**log_extra, 'error_code': exc.code})
        raise media_upload_failed() from None
    return staging_key, validation_key, expires_at, upload_url


def create_resource_intent(event_id, resource_id, data, user, kind):
    """Intent for a resource's audio/document asset (validated ``data``)."""
    from .services import _lock_resource  # shared lock helper (event → resource)

    spec = SPECS[kind]
    content_type = data['content_type']
    staging_key, validation_key, expires_at, upload_url = _sign(spec, content_type, data['size_bytes'],
                                                                {'resource_id': resource_id})
    with transaction.atomic():
        _, resource = _lock_resource(event_id, resource_id)
        if resource.type != spec.resource_type:
            article = 'an' if spec.noun[0] in 'aeiou' else 'a'
            raise APIError('RESOURCE_TYPE_MISMATCH',
                           f'{spec.noun.capitalize()} files can only be uploaded to {article} {spec.noun} resource.',
                           status_code=409)
        if resource.status == PublicationStatus.PUBLISHED:
            raise APIError('UNPUBLISH_REQUIRED', f'Unpublish this resource before replacing its {spec.noun}.',
                           status_code=409)
        cleanup_ids = supersede_open_intents(resource)
        intent = UploadIntent.objects.create(
            resource=resource, created_by=user, kind=kind,
            staging_key=staging_key, validation_key=validation_key,
            file_name=spec.file_name(data['file_name'], content_type), content_type=content_type,
            size_bytes=data['size_bytes'], expires_at=expires_at)
        refresh_storage_status(resource)
        cleanup_after_commit(cleanup_ids)
    logger.info('upload intent issued', extra={'upload_id': intent.pk, 'resource_id': resource.pk,
                                               'upload_kind': kind})
    return intent, upload_url


def create_audio_intent(event_id, resource_id, data, user):
    return create_resource_intent(event_id, resource_id, {**data, 'content_type': media.MP3_CONTENT_TYPE},
                                  user, Kind.AUDIO)


def create_document_intent(event_id, resource_id, data, user):
    return create_resource_intent(event_id, resource_id, data, user, Kind.DOCUMENT)


def create_image_intent(event_id, data, user):
    """Intent for the event's featured image. Allowed in any event state: a
    published event keeps its current image until the new one is promoted."""
    from .services import _lock_event

    spec = SPECS[Kind.IMAGE]
    content_type = data['content_type']
    staging_key, validation_key, expires_at, upload_url = _sign(spec, content_type, data['size_bytes'],
                                                                {'event_id': event_id})
    with transaction.atomic():
        event = _lock_event(event_id)
        cleanup_ids = supersede_open_image_intents(event)
        intent = UploadIntent.objects.create(
            event=event, created_by=user, kind=Kind.IMAGE,
            staging_key=staging_key, validation_key=validation_key,
            file_name=spec.file_name(data['file_name'], content_type), content_type=content_type,
            size_bytes=data['size_bytes'], expires_at=expires_at)
        refresh_image_status(event)
        cleanup_after_commit(cleanup_ids)
    logger.info('upload intent issued', extra={'upload_id': intent.pk, 'event_id': event.pk,
                                               'upload_kind': Kind.IMAGE})
    return intent, upload_url


# ---------------------------------------------------------------- finalize

@dataclass
class FinalizeResult:
    intent: UploadIntent
    resource: EventResource | None
    event: Event | None = None
    replay: bool = False


def _locked_chain(upload_id, user):
    """Lock event → resource (resource kinds) → intent for an intent owned by ``user``."""
    row = (UploadIntent.objects.filter(pk=upload_id, created_by=user)
           .values('resource_id', 'event_id', 'resource__event_id').first())
    if row is None:
        return None, None, None
    event_id = row['event_id'] or row['resource__event_id']
    event = Event.objects.select_for_update().filter(pk=event_id).first()
    resource = None
    if row['resource_id']:
        resource = (EventResource.objects.select_for_update()
                    .filter(pk=row['resource_id'], event_id=event_id).first())
    intent = UploadIntent.objects.select_for_update().filter(pk=upload_id, created_by=user).first()
    return event, resource, intent


def _target_missing(event, resource, intent):
    if intent is None or event is None:
        return True
    return intent.kind != Kind.IMAGE and resource is None


def _not_found():
    return APIError('NOT_FOUND', 'Upload not found.', status_code=404)


def _release_lease(upload_id, token):
    """Give up the lease. Without an immutable copy the intent is ``issued``
    again (so normal expiry applies); with one it stays ``validating`` with no
    lease, retryable at once and abandoned by the cleanup command if left."""
    rows = UploadIntent.objects.filter(pk=upload_id, lease_token=token, status=Status.VALIDATING)
    now = timezone.now()
    rows.filter(validation_etag='').update(status=Status.ISSUED, lease_token='', lease_expires_at=None,
                                           updated_at=now)
    rows.update(lease_token='', lease_expires_at=None, updated_at=now)


def _terminal(intent, status, code='', message=''):
    intent.status = status
    intent.error_code = code
    intent.error_message = message[:200]
    intent.lease_token = ''
    intent.lease_expires_at = None
    intent.finalized_at = timezone.now()
    intent.save()


def finalize_upload(upload_id, user):
    # Phase 1: ownership/state checks and lease acquisition (short transaction).
    with transaction.atomic():
        event, resource, intent = _locked_chain(upload_id, user)
        if _target_missing(event, resource, intent):
            raise _not_found()
        if intent.status in UploadIntent.TERMINAL:
            return FinalizeResult(intent, resource, event, replay=True)
        now = timezone.now()
        if intent.status == Status.VALIDATING and intent.lease_expires_at and intent.lease_expires_at > now:
            raise APIError('UPLOAD_IN_PROGRESS', 'This upload is already being validated. Retry shortly.',
                           status_code=409)
        if not intent.validation_etag and now >= intent.expires_at:
            _terminal(intent, Status.EXPIRED, 'UPLOAD_EXPIRED',
                      'The upload link expired before the upload was finalized. Request a new upload.')
            cleanup_ids = [record_cleanup(PRIVATE, intent.staging_key, 'expired', upload_id=intent.pk,
                                          **_owner(intent)).pk]
            _refresh_target(intent, event, resource)
            cleanup_after_commit(cleanup_ids)
            logger.info('upload expired', extra={'upload_id': intent.pk, **_owner(intent)})
            return FinalizeResult(intent, resource, event)
        token = uuid.uuid4().hex
        intent.status = Status.VALIDATING
        intent.lease_token = token
        intent.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        intent.attempts += 1
        intent.save()
        snapshot = UploadIntent.objects.get(pk=intent.pk)

    # Phase 2: provider I/O and validation, no row locks held.
    outcome = None  # ('ready', info) or ('rejected'|'failed', code, message)
    derivative = None  # featured image: sanitized server-generated copy (key, ObjectInfo, cleanup row id)
    promotion = None  # featured image of a published event: (public key, url, cleanup row id)
    storage = get_storage()
    try:
        try:
            outcome = _observe_and_validate(storage, snapshot, token)
            if outcome[0] == 'ready' and snapshot.kind == Kind.IMAGE:
                # The raw upload is never attached or published: only the re-encoded,
                # metadata-free derivative is.
                derivative = _store_derivative(storage, snapshot, outcome[1].sanitized)
                if Event.objects.filter(pk=snapshot.event_id, status=PublicationStatus.PUBLISHED).exists():
                    # Promote before the pointer can change, so the visible image never
                    # points at a missing/unvalidated object.
                    try:
                        promotion = _copy_to_public(derivative[0], derivative[1].etag,
                                                    spec=SPECS[Kind.IMAGE], file_name=snapshot.file_name,
                                                    content_type=snapshot.content_type, event_id=snapshot.event_id)
                    except (ObjectNotFound, PreconditionFailed):
                        outcome = ('failed', 'VALIDATION_COPY_MISSING',
                                   'The validated copy is unavailable. Upload the file again.')
        except _Retryable as exc:
            _release_lease(snapshot.pk, token)
            raise exc.error from None
        except StorageError as exc:
            _release_lease(snapshot.pk, token)
            logger.warning('upload finalize provider failure',
                           extra={'upload_id': snapshot.pk, 'error_code': exc.code})
            raise media_upload_failed() from None
        except Exception:  # noqa: BLE001 - e.g. temp disk full; never a 500 with details or a stuck lease
            _release_lease(snapshot.pk, token)
            logger.error('upload finalize internal failure',
                         extra={'upload_id': snapshot.pk, 'error_code': 'FINALIZE_INTERNAL_ERROR'})
            raise media_upload_failed() from None

        # Phase 3: attach or reject atomically under the lock chain.
        return _complete(upload_id, user, token, snapshot, outcome, promotion, derivative)
    finally:
        # Server-made copies not attached above become eligible for cleanup (no-op once attached).
        if derivative:
            promotion_abandoned([derivative[2]], 'derivative_unused')
        if promotion:
            promotion_abandoned([promotion[2]], 'promotion_unused')


def _store_derivative(storage, intent, data):
    """Write the sanitized image under a new server-only private key. Its cleanup
    row is recorded first (with a grace period) and cancelled when attached."""
    spec = SPECS[Kind.IMAGE]
    key = f'media/{spec.prefix}/{_token()}{spec.extension(intent.content_type)}'
    row = record_cleanup(PRIVATE, key, 'derivative_pending', event_id=intent.event_id, upload_id=intent.pk,
                         not_before=timezone.now() + PROMOTION_GRACE)
    try:
        info = storage.put_object(key, data, content_type=intent.content_type)
    except StorageError:
        promotion_abandoned([row.pk], 'derivative_failed')
        raise
    return key, info, row.pk


class _Retryable(Exception):
    def __init__(self, error, back_to_issued=False):
        super().__init__(error.code)
        self.error = error


def _content_type_matches(observed, expected):
    return observed.split(';', 1)[0].strip().lower() == expected


DOCUMENT_MAGIC = {documents.PDF_CONTENT_TYPE: b'%PDF-', documents.DOCX_CONTENT_TYPE: b'PK\x03\x04'}


def validate_content(kind, spool, size, content_type):
    """Kind-specific bounded validation of the spooled immutable copy."""
    if kind == Kind.AUDIO:
        return media.validate_mp3(spool, size)
    if kind == Kind.IMAGE:
        return images.validate_image(spool, size, content_type)
    spool.seek(0)
    head = spool.read(8)
    actual = next((ct for ct, magic in DOCUMENT_MAGIC.items() if head.startswith(magic)), None)
    if actual is not None and actual != content_type:
        raise media.MediaRejected('CONTENT_TYPE_MISMATCH', 'The document content does not match the declared type.')
    if content_type == documents.PDF_CONTENT_TYPE:
        documents.validate_pdf(spool, size)
    else:
        documents.validate_docx(spool, size)
    return None


def _observe_and_validate(storage, intent, token):
    spec = SPECS[intent.kind]
    if not intent.validation_etag:
        staged = storage.head(intent.staging_key)
        if staged is None:
            raise _Retryable(APIError('UPLOAD_INCOMPLETE',
                                      'The file has not arrived in storage. Finish the upload, then retry.',
                                      status_code=409), back_to_issued=True)
        if staged.size != intent.size_bytes:
            return ('rejected', 'SIZE_MISMATCH', 'The uploaded file size does not match the declared size.')
        if staged.size > spec.max_bytes:
            return ('rejected', 'FILE_TOO_LARGE', f'The file exceeds the {spec.limit_label} limit.')
        if not _content_type_matches(staged.content_type, intent.content_type):
            return ('rejected', 'CONTENT_TYPE_MISMATCH', 'The uploaded file type does not match the declared type.')
        try:
            copied = storage.copy_if_match(intent.staging_key, intent.validation_key, etag=staged.etag,
                                           content_type=intent.content_type)
        except PreconditionFailed:
            raise _Retryable(APIError('UPLOAD_CHANGED', 'The file changed while it was being finalized. Retry.',
                                      status_code=409), back_to_issued=True) from None
        except ObjectNotFound:
            raise _Retryable(APIError('UPLOAD_INCOMPLETE',
                                      'The file has not arrived in storage. Finish the upload, then retry.',
                                      status_code=409), back_to_issued=True) from None
        if copied.size != staged.size:
            return ('rejected', 'SIZE_MISMATCH', 'The uploaded file size does not match the declared size.')
        updated = UploadIntent.objects.filter(pk=intent.pk, lease_token=token, status=Status.VALIDATING).update(
            observed_etag=staged.etag, observed_size=staged.size, validation_etag=copied.etag,
            updated_at=timezone.now())
        if not updated:
            # Superseded/deleted meanwhile: the copy is an orphan.
            record_cleanup(PRIVATE, intent.validation_key, 'superseded', upload_id=intent.pk, **_owner(intent))
            raise _Retryable(APIError('UPLOAD_IN_PROGRESS', 'This upload changed state. Retry shortly.',
                                      status_code=409))
        intent.observed_etag, intent.observed_size, intent.validation_etag = staged.etag, staged.size, copied.etag

    # Validate the immutable server-only copy, never the client-writable staging key.
    copy = storage.head(intent.validation_key)
    if copy is None or copy.etag != intent.validation_etag or copy.size != intent.observed_size:
        return ('failed', 'VALIDATION_COPY_MISSING', 'The validated copy is unavailable. Upload the file again.')
    try:
        stream = storage.open_stream(intent.validation_key, if_match=intent.validation_etag)
    except (ObjectNotFound, PreconditionFailed):
        return ('failed', 'VALIDATION_COPY_MISSING', 'The validated copy is unavailable. Upload the file again.')
    try:
        spool = media.spool_stream(stream, expected_size=copy.size, max_bytes=spec.max_bytes)
        try:
            info = validate_content(intent.kind, spool, copy.size, intent.content_type)
        finally:
            spool.close()
    except media.MediaRejected as exc:
        return ('rejected', exc.code, exc.message)
    return ('ready', info)


def _complete(upload_id, user, token, snapshot, outcome, promotion=None, derivative=None):
    """Attach or reject under the lock chain, only while our lease is live."""
    cleanup_ids = []
    after_commit_error = None
    result = None
    with transaction.atomic():
        event, resource, intent = _locked_chain(upload_id, user)
        if _target_missing(event, resource, intent):
            after_commit_error = 'gone'
        elif intent.lease_token != token or intent.status != Status.VALIDATING:
            # Superseded or taken over: report the current state, do not attach.
            if intent.status != Status.READY:
                cleanup_ids.append(record_cleanup(PRIVATE, snapshot.validation_key, 'superseded',
                                                  upload_id=intent.pk, **_owner(intent)).pk)
            if intent.status in UploadIntent.TERMINAL:
                result = FinalizeResult(intent, resource, event)
            else:
                after_commit_error = 'in_progress'
        elif intent.lease_expires_at is None or intent.lease_expires_at < timezone.now():
            # Our lease lapsed (validation outlived it): another finalize may start.
            # Attach nothing; drop the lease so a retry starts cleanly.
            intent.lease_token = ''
            intent.lease_expires_at = None
            if not intent.validation_etag:
                intent.status = Status.ISSUED
            intent.save()
            after_commit_error = 'lease_expired'
        elif (outcome[0] == 'ready' and intent.kind == Kind.IMAGE
              and event.status == PublicationStatus.PUBLISHED and promotion is None):
            # The event was published while this image was validated: it must be
            # promoted first. Keep the validated copy; a retry re-validates and promotes.
            intent.lease_token = ''
            intent.lease_expires_at = None
            intent.save()
            after_commit_error = 'event_changed'
        else:
            kind = outcome[0]
            if kind == 'ready':
                conflict = _attach_conflict(intent, resource)
                if conflict:
                    _terminal(intent, Status.FAILED, *conflict)
                    kind = 'failed'
                elif intent.kind == Kind.IMAGE:
                    cleanup_ids += _attach_image(event, intent, promotion, derivative)
                else:
                    cleanup_ids += _attach(resource, intent, outcome[1])
            else:
                _terminal(intent, Status.REJECTED if kind == 'rejected' else Status.FAILED, outcome[1], outcome[2])
            if kind != 'ready':
                for key in (intent.staging_key, intent.validation_key):
                    cleanup_ids.append(record_cleanup(PRIVATE, key, f'upload_{kind}', upload_id=intent.pk,
                                                      **_owner(intent)).pk)
                _refresh_target(intent, event, resource)
            result = FinalizeResult(intent, resource, event)
        cleanup_after_commit(cleanup_ids)
    if after_commit_error == 'gone':
        # Target (and its intent) deleted during validation: record the orphan copy.
        record_cleanup(PRIVATE, snapshot.validation_key, 'resource_deleted', upload_id=snapshot.pk,
                       **_owner(snapshot))
        raise _not_found()
    if after_commit_error == 'in_progress':
        raise APIError('UPLOAD_IN_PROGRESS', 'This upload is already being validated. Retry shortly.',
                       status_code=409)
    if after_commit_error == 'lease_expired':
        raise APIError('UPLOAD_LEASE_EXPIRED', 'Validation took too long. Retry finalize.', status_code=409)
    if after_commit_error == 'event_changed':
        raise APIError('MEDIA_CHANGED', 'The event was published while the image was validated. Retry finalize.',
                       status_code=409)
    logger.info('upload finalized', extra={'upload_id': result.intent.pk, **_owner(result.intent),
                                           'upload_kind': result.intent.kind,
                                           'upload_status': result.intent.status,
                                           'error_code': result.intent.error_code})
    return result


def _attach_conflict(intent, resource):
    if intent.kind == Kind.IMAGE:
        return None  # a featured image may replace the current one in any event state
    spec = SPECS[intent.kind]
    if resource.type != spec.resource_type:
        return ('RESOURCE_TYPE_MISMATCH', f'The resource is no longer a {spec.noun} resource.')
    if resource.status == PublicationStatus.PUBLISHED:
        return ('UNPUBLISH_REQUIRED',
                f'The resource was published meanwhile; unpublish it to replace its {spec.noun}.')
    return None


def _attach(resource, intent, info):
    """Make the validated copy the resource's ready asset (locked rows)."""
    cleanup_ids = []
    if resource.private_object_key and resource.private_object_key != intent.validation_key:
        cleanup_ids.append(record_cleanup(PRIVATE, resource.private_object_key, 'replaced',
                                          resource_id=resource.pk).pk)
    cleanup_ids.append(retire_public_copy(resource, 'replaced'))
    now = timezone.now()
    resource.private_object_key = intent.validation_key
    resource.etag = intent.validation_etag
    resource.content_type = intent.content_type
    resource.size_bytes = intent.observed_size
    resource.file_name = intent.file_name
    resource.uploaded_at = now
    resource.storage_status = Storage.READY
    fields = ['private_object_key', 'etag', 'content_type', 'size_bytes', 'file_name', 'uploaded_at',
              'public_object_key', 'public_media_url', 'storage_status', 'updated_at']
    if intent.kind == Kind.AUDIO:
        # The new asset's duration replaces the old one; unknown/out of range clears it.
        in_range = bool(info.duration_seconds) and info.duration_seconds <= DURATION_MAX
        resource.duration = info.duration_seconds if in_range else None
        fields.append('duration')
        intent.duration = info.duration_seconds
    resource.save(update_fields=fields)
    _terminal(intent, Status.READY)
    cleanup_ids.append(record_cleanup(PRIVATE, intent.staging_key, 'staging_consumed', resource_id=resource.pk,
                                      upload_id=intent.pk).pk)
    return cleanup_ids


def _attach_image(event, intent, promotion, derivative):
    """Make the sanitized derivative the event's featured image (locked rows).

    For a published event ``promotion`` is the already-copied public object
    (of the derivative): the pointer swaps to it and the previous public copy
    is retired (deleted and purged after commit). Otherwise only the private
    asset changes. The raw validation copy is recorded for cleanup."""
    key, info, derivative_row = derivative
    cleanup_ids = []
    if event.featured_image_object_key and event.featured_image_object_key != key:
        cleanup_ids.append(record_cleanup(PRIVATE, event.featured_image_object_key, 'replaced',
                                          event_id=event.pk).pk)
    cleanup_ids.append(retire_event_image(event, 'replaced'))
    event.featured_image_object_key = key
    event.featured_image_etag = info.etag
    event.featured_image_content_type = intent.content_type
    event.featured_image_size_bytes = info.size
    promotion_attached([derivative_row])
    event.featured_image_file_name = intent.file_name
    event.featured_image_uploaded_at = timezone.now()
    event.featured_image_storage_status = ImageStorageStatus.READY
    if event.status == PublicationStatus.PUBLISHED and promotion:
        event.featured_image_public_key, event.featured_image_public_url = promotion[0], promotion[1]
        promotion_attached([promotion[2]])
    event.save(update_fields=[*IMAGE_ASSET_FIELDS, *IMAGE_POINTER_FIELDS, 'updated_at'])
    _terminal(intent, Status.READY)
    cleanup_ids.append(record_cleanup(PRIVATE, intent.staging_key, 'staging_consumed', event_id=event.pk,
                                      upload_id=intent.pk).pk)
    cleanup_ids.append(record_cleanup(PRIVATE, intent.validation_key, 'image_sanitized', event_id=event.pk,
                                      upload_id=intent.pk).pk)
    return cleanup_ids


IMAGE_ASSET_FIELDS = ['featured_image_object_key', 'featured_image_etag', 'featured_image_content_type',
                      'featured_image_size_bytes', 'featured_image_file_name', 'featured_image_uploaded_at',
                      'featured_image_storage_status']


# ---------------------------------------------------------------- preview / promotion

def _presign_preview(key, spec, content_type, file_name):
    try:
        return get_storage().presign_get(key, expires_in=preview_ttl(), content_type=content_type,
                                         filename=file_name, disposition=spec.disposition)
    except StorageError:
        return None


def preview_url(resource):
    """Admin-only short-lived signed GET of the private ready asset, or None."""
    if not resource.has_asset:
        return None
    spec = spec_for_resource(resource)
    content_type = resource.content_type or next(iter(spec.types))
    return _presign_preview(resource.private_object_key, spec, content_type,
                            resource.file_name or f'{spec.fallback_name}{spec.extension(content_type)}')


def image_preview_url(event):
    """Admin-only short-lived signed GET of the event's private featured image, or None."""
    if not event.has_featured_image:
        return None
    spec = SPECS[Kind.IMAGE]
    content_type = event.featured_image_content_type or images.JPEG_CONTENT_TYPE
    return _presign_preview(event.featured_image_object_key, spec, content_type,
                            event.featured_image_file_name or f'image{spec.extension(content_type)}')


def _copy_to_public(private_key, etag, *, spec, file_name, content_type, resource_id=None, event_id=None):
    """Copy validated private bytes (at their ETag) to a new unique public key.

    The cleanup row for the new key is recorded *before* the copy, with a grace
    period, so a crash at any point leaves no unrecorded public object. The
    caller marks it attached (:func:`promotion_attached`) on success or
    :func:`promotion_abandoned` otherwise. Returns ``(public_key, url, row_id)``;
    raises :class:`StorageError` (the row already made eligible)."""
    storage = get_storage()
    public_key = f'{spec.prefix}/{_token()}/{file_name}'
    url = storage.public_url(public_key)
    row = record_cleanup(PUBLIC, public_key, 'promotion_pending', resource_id=resource_id, event_id=event_id,
                         public_url=url, not_before=timezone.now() + PROMOTION_GRACE)
    try:
        storage.copy_if_match(private_key, public_key, etag=etag, src_bucket=PRIVATE, dst_bucket=PUBLIC,
                              content_type=content_type,
                              content_disposition=content_disposition(spec.disposition, file_name),
                              cache_control=PUBLIC_CACHE_CONTROL)
    except StorageError as exc:
        promotion_abandoned([row.pk], 'promotion_failed')
        logger.warning('publication promotion failed', extra={'resource_id': resource_id, 'event_id': event_id,
                                                              'error_code': exc.code})
        raise
    return public_key, url, row.pk


def _promotion_error(exc, noun):
    if isinstance(exc, (ObjectNotFound, PreconditionFailed)):
        return APIError('MEDIA_NOT_READY', f'The validated {noun} is unavailable. Upload it again.',
                        status_code=409)
    return media_upload_failed()


def promote_to_public(resource):
    """Promote a resource's validated asset; returns ``(public_key, url, row_id)``."""
    spec = spec_for_resource(resource)
    content_type = resource.content_type or next(iter(spec.types))
    file_name = resource.file_name or f'{spec.fallback_name}{spec.extension(content_type)}'
    try:
        return _copy_to_public(resource.private_object_key, resource.etag, spec=spec, file_name=file_name,
                               content_type=content_type, resource_id=resource.pk)
    except StorageError as exc:
        raise _promotion_error(exc, spec.noun) from None


def promote_event_image(event):
    """Promote the event's validated featured image; returns ``(public_key, url, row_id)``."""
    spec = SPECS[Kind.IMAGE]
    content_type = event.featured_image_content_type or images.JPEG_CONTENT_TYPE
    file_name = event.featured_image_file_name or f'image{spec.extension(content_type)}'
    try:
        return _copy_to_public(event.featured_image_object_key, event.featured_image_etag, spec=spec,
                               file_name=file_name, content_type=content_type, event_id=event.pk)
    except StorageError as exc:
        raise _promotion_error(exc, spec.noun) from None


def promotion_attached(row_ids):
    """The promoted copy is now referenced: its pending cleanup is cancelled."""
    StorageCleanup.objects.filter(pk__in=[i for i in row_ids if i], status=StorageCleanup.Status.PENDING).update(
        status=StorageCleanup.Status.CANCELLED, processed_at=timezone.now())


def promotion_abandoned(row_ids, reason):
    """The promoted copy will not be attached: make its cleanup eligible now."""
    StorageCleanup.objects.filter(pk__in=[i for i in row_ids if i], status=StorageCleanup.Status.PENDING).update(
        reason=reason, not_before=timezone.now())


# ---------------------------------------------------------------- maintenance

def expire_stale_intents(now=None):
    """Expire unfinished intents past expiry and abandon stuck validations."""
    now = now or timezone.now()
    expired = abandoned = 0
    stale = UploadIntent.objects.filter(
        Q(status=Status.ISSUED, expires_at__lte=now)
        | (Q(status=Status.VALIDATING, validation_etag='', expires_at__lte=now)
           & (Q(lease_expires_at__lte=now) | Q(lease_expires_at__isnull=True)))
        | (Q(status=Status.VALIDATING, expires_at__lte=now - ABANDONED_AFTER)
           & (Q(lease_expires_at__lte=now) | Q(lease_expires_at__isnull=True)))
    ).values_list('pk', 'resource_id', 'event_id', 'resource__event_id')
    for upload_id, resource_id, image_event_id, resource_event_id in stale:
        with transaction.atomic():
            event = Event.objects.select_for_update().filter(pk=image_event_id or resource_event_id).first()
            resource = None
            if resource_id:
                resource = EventResource.objects.select_for_update().filter(pk=resource_id).first()
            intent = UploadIntent.objects.select_for_update().filter(pk=upload_id).first()
            if _target_missing(event, resource, intent) or intent.status not in UploadIntent.OPEN:
                continue
            if intent.status == Status.VALIDATING and intent.lease_expires_at and intent.lease_expires_at > now:
                continue
            if intent.validation_etag:
                _terminal(intent, Status.FAILED, 'VALIDATION_ABANDONED', 'Validation did not complete.')
                abandoned += 1
            else:
                _terminal(intent, Status.EXPIRED, 'UPLOAD_EXPIRED', 'The upload link expired.')
                expired += 1
            for key in (intent.staging_key, intent.validation_key):
                record_cleanup(PRIVATE, key, 'expired', upload_id=intent.pk, **_owner(intent))
            _refresh_target(intent, event, resource)
    return {'expired': expired, 'abandoned': abandoned}
