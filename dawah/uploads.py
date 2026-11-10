"""Durable audio upload, finalize, publication promotion and cleanup (D2).

Protocol: intent → direct browser PUT (signed, private staging key) → finalize
(server-observed size/type/ETag, conditional copy to a server-only validation
key, bounded MP3 validation of that immutable copy) → ready private asset →
publish (copy at ETag to a unique public key, then record ``public_media_url``).

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

from . import media
from .models import Event, EventResource, PublicationStatus, StorageCleanup, UploadIntent
from .storage import PRIVATE, PUBLIC, ObjectNotFound, PreconditionFailed, StorageError, get_storage
from .storage.s3 import inline_disposition

logger = logging.getLogger('dawah.uploads')

Status = UploadIntent.Status
Storage = EventResource.StorageStatus
LEASE_SECONDS = 300
ABANDONED_AFTER = timedelta(days=1)
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


def safe_file_name(name, extension='.mp3'):
    """ASCII-safe display/download name derived from the client's file name."""
    base = re.split(r'[\\/]', name)[-1]
    stem = base[:-len(extension)] if base.lower().endswith(extension) else base
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', stem).strip('.-_')[:80] or 'audio'
    return f'{stem}{extension}'


# ---------------------------------------------------------------- cleanup records

def record_cleanup(bucket, key, reason, *, resource_id=None, upload_id=None):
    if not key:
        return None
    return StorageCleanup.objects.create(bucket=bucket, key=key, reason=reason, resource_id=resource_id,
                                         upload_id=upload_id)


def record_resource_cleanup(resource, reason):
    """Record every provider object a deleted resource leaves behind."""
    ids = []
    for bucket, key in ((PRIVATE, resource.private_object_key), (PUBLIC, resource.public_object_key)):
        row = record_cleanup(bucket, key, reason, resource_id=resource.pk)
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
        return EventResource.objects.filter(public_object_key=row.key).exists()
    return (EventResource.objects.filter(private_object_key=row.key).exists()
            or UploadIntent.objects.filter(validation_key=row.key, status=Status.VALIDATING,
                                           lease_expires_at__gt=timezone.now()).exists())


def process_cleanup(*, ids=None, limit=100):
    """Delete recorded orphans. Returns counts; never deletes referenced keys."""
    storage = get_storage()
    counts = {'done': 0, 'skipped': 0, 'failed': 0}
    rows = StorageCleanup.objects.filter(status=StorageCleanup.Status.PENDING)
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


def supersede_open_intents(resource, reason='SUPERSEDED'):
    """Fail every unfinished intent of the (locked) resource; returns cleanup ids."""
    ids = []
    for intent in UploadIntent.objects.select_for_update().filter(resource=resource, status__in=UploadIntent.OPEN):
        intent.status = Status.FAILED
        intent.error_code = reason
        intent.error_message = 'A newer upload replaced this one.'
        intent.lease_token = ''
        intent.lease_expires_at = None
        intent.finalized_at = timezone.now()
        intent.save()
        for key in (intent.staging_key, intent.validation_key):
            ids.append(record_cleanup(PRIVATE, key, 'superseded', resource_id=resource.pk, upload_id=intent.pk).pk)
    return ids


# ---------------------------------------------------------------- intent

def create_audio_intent(event_id, resource_id, data, user):
    from .services import _lock_resource  # shared lock helper (event → resource)

    storage = get_storage()
    ttl = intent_ttl()
    expires_at = timezone.now() + timedelta(seconds=ttl)
    staging_key = f'staging/audio/{_token()}'
    validation_key = f'media/audio/{_token()}.mp3'
    try:
        # Signing is local computation; done before any row exists.
        upload_url = storage.presign_put(staging_key, content_type=media.MP3_CONTENT_TYPE, expires_in=ttl)
    except StorageError as exc:
        logger.warning('upload intent signing failed', extra={'resource_id': resource_id, 'error_code': exc.code})
        raise media_upload_failed() from None

    with transaction.atomic():
        _, resource = _lock_resource(event_id, resource_id)
        if resource.type != EventResource.Type.AUDIO:
            raise APIError('RESOURCE_TYPE_MISMATCH', 'Audio can only be uploaded to an audio resource.',
                           status_code=409)
        if resource.status == PublicationStatus.PUBLISHED:
            raise APIError('UNPUBLISH_REQUIRED', 'Unpublish this resource before replacing its audio.',
                           status_code=409)
        cleanup_ids = supersede_open_intents(resource)
        intent = UploadIntent.objects.create(
            resource=resource, created_by=user, kind=UploadIntent.Kind.AUDIO,
            staging_key=staging_key, validation_key=validation_key,
            file_name=safe_file_name(data['file_name']), content_type=media.MP3_CONTENT_TYPE,
            size_bytes=data['size_bytes'], expires_at=expires_at)
        refresh_storage_status(resource)
        cleanup_after_commit(cleanup_ids)
    logger.info('upload intent issued', extra={'upload_id': intent.pk, 'resource_id': resource.pk})
    return intent, upload_url


# ---------------------------------------------------------------- finalize

@dataclass
class FinalizeResult:
    intent: UploadIntent
    resource: EventResource | None
    replay: bool = False


def _locked_chain(upload_id, user):
    """Lock event → resource → intent for an intent owned by ``user``."""
    row = (UploadIntent.objects.filter(pk=upload_id, created_by=user)
           .values('resource_id', 'resource__event_id').first())
    if row is None:
        return None, None, None
    event = Event.objects.select_for_update().filter(pk=row['resource__event_id']).first()
    resource = (EventResource.objects.select_for_update()
                .filter(pk=row['resource_id'], event_id=row['resource__event_id']).first())
    intent = UploadIntent.objects.select_for_update().filter(pk=upload_id, created_by=user).first()
    return event, resource, intent


def _not_found():
    return APIError('NOT_FOUND', 'Upload not found.', status_code=404)


def _release_lease(upload_id, token, *, back_to_issued=False):
    updates = {'lease_token': '', 'lease_expires_at': None, 'updated_at': timezone.now()}
    if back_to_issued:
        updates['status'] = Status.ISSUED
    UploadIntent.objects.filter(pk=upload_id, lease_token=token, status=Status.VALIDATING).update(**updates)


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
        if intent is None or resource is None:
            raise _not_found()
        if intent.status in UploadIntent.TERMINAL:
            return FinalizeResult(intent, resource, replay=True)
        now = timezone.now()
        if intent.status == Status.VALIDATING and intent.lease_expires_at and intent.lease_expires_at > now:
            raise APIError('UPLOAD_IN_PROGRESS', 'This upload is already being validated. Retry shortly.',
                           status_code=409)
        if not intent.validation_etag and now >= intent.expires_at:
            _terminal(intent, Status.EXPIRED, 'UPLOAD_EXPIRED',
                      'The upload link expired before the upload was finalized. Request a new upload.')
            cleanup_ids = [record_cleanup(PRIVATE, intent.staging_key, 'expired', resource_id=resource.pk,
                                          upload_id=intent.pk).pk]
            refresh_storage_status(resource)
            cleanup_after_commit(cleanup_ids)
            logger.info('upload expired', extra={'upload_id': intent.pk, 'resource_id': resource.pk})
            return FinalizeResult(intent, resource)
        token = uuid.uuid4().hex
        intent.status = Status.VALIDATING
        intent.lease_token = token
        intent.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        intent.attempts += 1
        intent.save()
        snapshot = UploadIntent.objects.get(pk=intent.pk)

    # Phase 2: provider I/O and validation, no row locks held.
    outcome = None  # ('ready', AudioInfo) or ('rejected', code, message)
    storage = get_storage()
    try:
        outcome = _observe_and_validate(storage, snapshot, token)
    except _Retryable as exc:
        _release_lease(snapshot.pk, token, back_to_issued=exc.back_to_issued)
        raise exc.error from None
    except StorageError as exc:
        _release_lease(snapshot.pk, token)
        logger.warning('upload finalize provider failure',
                       extra={'upload_id': snapshot.pk, 'error_code': exc.code})
        raise media_upload_failed() from None

    # Phase 3: attach or reject atomically under the lock chain.
    return _complete(upload_id, user, token, snapshot, outcome)


class _Retryable(Exception):
    def __init__(self, error, back_to_issued=False):
        super().__init__(error.code)
        self.error = error
        self.back_to_issued = back_to_issued


def _content_type_matches(observed, expected):
    return observed.split(';', 1)[0].strip().lower() == expected


def _observe_and_validate(storage, intent, token):
    if not intent.validation_etag:
        staged = storage.head(intent.staging_key)
        if staged is None:
            raise _Retryable(APIError('UPLOAD_INCOMPLETE',
                                      'The file has not arrived in storage. Finish the upload, then retry.',
                                      status_code=409), back_to_issued=True)
        if staged.size != intent.size_bytes:
            return ('rejected', 'SIZE_MISMATCH', 'The uploaded file size does not match the declared size.')
        if staged.size > media.MP3_MAX_BYTES:
            return ('rejected', 'FILE_TOO_LARGE', 'The file exceeds the 100 MiB audio limit.')
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
            record_cleanup(PRIVATE, intent.validation_key, 'superseded', resource_id=intent.resource_id,
                           upload_id=intent.pk)
            raise _Retryable(APIError('UPLOAD_IN_PROGRESS', 'This upload changed state. Retry shortly.',
                                      status_code=409))
        intent.observed_etag, intent.observed_size, intent.validation_etag = staged.etag, staged.size, copied.etag

    # Validate the immutable server-only copy, never the client-writable staging key.
    copy = storage.head(intent.validation_key)
    if copy is None or copy.etag != intent.validation_etag or copy.size != intent.observed_size:
        return ('failed', 'VALIDATION_COPY_MISSING', 'The validated copy is unavailable. Upload the file again.')
    try:
        stream = storage.open_stream(intent.validation_key)
    except ObjectNotFound:
        return ('failed', 'VALIDATION_COPY_MISSING', 'The validated copy is unavailable. Upload the file again.')
    try:
        spool = media.spool_stream(stream, expected_size=copy.size)
        try:
            info = media.validate_mp3(spool, copy.size)
        finally:
            spool.close()
    except media.MediaRejected as exc:
        return ('rejected', exc.code, exc.message)
    return ('ready', info)


def _complete(upload_id, user, token, snapshot, outcome):
    cleanup_ids = []
    with transaction.atomic():
        event, resource, intent = _locked_chain(upload_id, user)
        if intent is None or resource is None:
            gone = True
        else:
            gone = False
            if intent.lease_token != token or intent.status != Status.VALIDATING:
                # Superseded or taken over: report the current state, do not attach.
                if intent.status != Status.READY:
                    cleanup_ids.append(record_cleanup(PRIVATE, snapshot.validation_key, 'superseded',
                                                      resource_id=resource.pk, upload_id=intent.pk).pk)
                cleanup_after_commit(cleanup_ids)
                if intent.status in UploadIntent.TERMINAL:
                    return FinalizeResult(intent, resource)
                raise APIError('UPLOAD_IN_PROGRESS', 'This upload is already being validated. Retry shortly.',
                               status_code=409)
            kind = outcome[0]
            if kind == 'ready':
                conflict = _attach_conflict(resource)
                if conflict:
                    _terminal(intent, Status.FAILED, *conflict)
                    kind = 'failed'
                else:
                    cleanup_ids += _attach(resource, intent, outcome[1])
            else:
                _terminal(intent, Status.REJECTED if kind == 'rejected' else Status.FAILED, outcome[1], outcome[2])
            if kind != 'ready':
                for key in (intent.staging_key, intent.validation_key):
                    cleanup_ids.append(record_cleanup(PRIVATE, key, f'upload_{kind}', resource_id=resource.pk,
                                                      upload_id=intent.pk).pk)
                refresh_storage_status(resource)
            cleanup_after_commit(cleanup_ids)
    if gone:
        # Resource (and its intent) deleted during validation: record the orphan copy.
        record_cleanup(PRIVATE, snapshot.validation_key, 'resource_deleted', resource_id=snapshot.resource_id,
                       upload_id=snapshot.pk)
        raise _not_found()
    logger.info('upload finalized', extra={'upload_id': intent.pk, 'resource_id': resource.pk,
                                           'upload_status': intent.status, 'error_code': intent.error_code})
    return FinalizeResult(intent, resource)


def _attach_conflict(resource):
    if resource.type != EventResource.Type.AUDIO:
        return ('RESOURCE_TYPE_MISMATCH', 'The resource is no longer an audio resource.')
    if resource.status == PublicationStatus.PUBLISHED:
        return ('UNPUBLISH_REQUIRED', 'The resource was published meanwhile; unpublish it to replace its audio.')
    return None


def _attach(resource, intent, info):
    """Make the validated copy the resource's ready asset (locked rows)."""
    cleanup_ids = []
    if resource.private_object_key and resource.private_object_key != intent.validation_key:
        cleanup_ids.append(record_cleanup(PRIVATE, resource.private_object_key, 'replaced',
                                          resource_id=resource.pk).pk)
    if resource.public_object_key:
        cleanup_ids.append(record_cleanup(PUBLIC, resource.public_object_key, 'replaced',
                                          resource_id=resource.pk).pk)
    now = timezone.now()
    resource.private_object_key = intent.validation_key
    resource.etag = intent.validation_etag
    resource.content_type = intent.content_type
    resource.size_bytes = intent.observed_size
    resource.file_name = intent.file_name
    resource.uploaded_at = now
    resource.public_object_key = ''
    resource.public_media_url = ''
    resource.storage_status = Storage.READY
    fields = ['private_object_key', 'etag', 'content_type', 'size_bytes', 'file_name', 'uploaded_at',
              'public_object_key', 'public_media_url', 'storage_status', 'updated_at']
    if info.duration_seconds and info.duration_seconds <= DURATION_MAX:
        resource.duration = info.duration_seconds
        fields.append('duration')
    resource.save(update_fields=fields)
    intent.duration = info.duration_seconds
    _terminal(intent, Status.READY)
    cleanup_ids.append(record_cleanup(PRIVATE, intent.staging_key, 'staging_consumed', resource_id=resource.pk,
                                      upload_id=intent.pk).pk)
    return cleanup_ids


# ---------------------------------------------------------------- preview / promotion

def preview_url(resource):
    """Admin-only short-lived signed GET of the private ready asset, or None."""
    if not resource.has_asset:
        return None
    try:
        return get_storage().presign_get(resource.private_object_key, expires_in=preview_ttl(),
                                         content_type=resource.content_type or media.MP3_CONTENT_TYPE,
                                         filename=resource.file_name or 'audio.mp3')
    except StorageError:
        return None


def promote_to_public(resource):
    """Copy the validated private bytes (at their ETag) to a new unique public key.

    Returns ``(public_key, public_url)``. Records cleanup of the new key on failure.
    """
    storage = get_storage()
    file_name = resource.file_name or 'audio.mp3'
    public_key = f'audio/{_token()}/{file_name}'
    try:
        storage.copy_if_match(resource.private_object_key, public_key, etag=resource.etag, src_bucket=PRIVATE,
                              dst_bucket=PUBLIC, content_type=resource.content_type or media.MP3_CONTENT_TYPE,
                              content_disposition=inline_disposition(file_name), cache_control=PUBLIC_CACHE_CONTROL)
        url = storage.public_url(public_key)
    except StorageError as exc:
        record_cleanup(PUBLIC, public_key, 'promotion_failed', resource_id=resource.pk)
        logger.warning('publication promotion failed', extra={'resource_id': resource.pk, 'error_code': exc.code})
        if isinstance(exc, (ObjectNotFound, PreconditionFailed)):
            raise APIError('MEDIA_NOT_READY', 'The validated asset is unavailable. Upload the audio again.',
                           status_code=409) from None
        raise media_upload_failed() from None
    return public_key, url


# ---------------------------------------------------------------- maintenance

def expire_stale_intents(now=None):
    """Expire unfinished intents past expiry and abandon stuck validations."""
    now = now or timezone.now()
    expired = abandoned = 0
    stale = UploadIntent.objects.filter(
        Q(status=Status.ISSUED, expires_at__lte=now)
        | Q(status=Status.VALIDATING, validation_etag='', expires_at__lte=now, lease_expires_at__lte=now)
        | Q(status=Status.VALIDATING, lease_expires_at__lte=now, expires_at__lte=now - ABANDONED_AFTER)
    ).values_list('pk', 'resource_id', 'resource__event_id')
    for upload_id, resource_id, event_id in stale:
        with transaction.atomic():
            Event.objects.select_for_update().filter(pk=event_id).first()
            resource = EventResource.objects.select_for_update().filter(pk=resource_id).first()
            intent = UploadIntent.objects.select_for_update().filter(pk=upload_id).first()
            if resource is None or intent is None or intent.status not in UploadIntent.OPEN:
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
                record_cleanup(PRIVATE, key, 'expired', resource_id=resource.pk, upload_id=intent.pk)
            refresh_storage_status(resource)
    return {'expired': expired, 'abandoned': abandoned}
