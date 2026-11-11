"""Da'wah events and their resources (D2).

Publication is explicit: every event/resource starts as ``draft`` and only the
publish/unpublish actions change ``status``. Public discovery is defined in one
place (``EventQuerySet.visible`` / ``EventResourceQuerySet.visible``) so later
packages extend it additively:

* DAWAH-02 adds R2 object metadata/UploadIntent, the finalize path that sets
  ``storage_status=ready`` and the publish promotion that records the public
  ``media_url`` for audio (see ``dawah/uploads.py``).
* DAWAH-05 adds ``youtube_video_id`` and tightens the video visibility rule.
* DAWAH-07 adds document uploads (same resource fields) and validated R2
  featured-image object metadata on the event (``featured_image_*``).
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from . import youtube


class PublicationStatus(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    PUBLISHED = 'published', 'Published'
    UNPUBLISHED = 'unpublished', 'Unpublished'


class ImageStorageStatus(models.TextChoices):
    NONE = 'none', 'No image'
    PENDING = 'pending', 'Upload pending'
    READY = 'ready', 'Ready'
    FAILED = 'failed', 'Upload failed'


class EventQuerySet(models.QuerySet):
    def visible(self):
        """Events any reader may discover (A03)."""
        return self.filter(status=PublicationStatus.PUBLISHED)


class Event(models.Model):
    title = models.CharField(max_length=250)
    category = models.CharField(max_length=250)
    tags = models.CharField(max_length=250, blank=True, default='')
    description = models.TextField(blank=True, null=True)
    summary = models.TextField(blank=True, null=True)

    speakers = models.CharField(max_length=256)
    location = models.CharField(max_length=256)

    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    # Legacy local upload; never exposed or published as a durable asset.
    featured_image = models.ImageField(upload_to='events/', blank=True, null=True)

    # Validated R2 featured image (DAWAH-07). Keys/ETags are server-only and
    # never serialized; the public copy/URL exists only while the event is
    # published (promotion before the pointer changes, as for resources).
    featured_image_storage_status = models.CharField(max_length=16, choices=ImageStorageStatus.choices,
                                                     default=ImageStorageStatus.NONE)
    featured_image_object_key = models.CharField(max_length=512, blank=True, default='',
                                                 help_text='Server-only validated private copy.')
    featured_image_etag = models.CharField(max_length=128, blank=True, default='')
    featured_image_content_type = models.CharField(max_length=100, blank=True, default='')
    featured_image_size_bytes = models.BigIntegerField(blank=True, null=True)
    featured_image_file_name = models.CharField(max_length=120, blank=True, default='')
    featured_image_uploaded_at = models.DateTimeField(blank=True, null=True)
    featured_image_public_key = models.CharField(max_length=512, blank=True, default='')
    featured_image_public_url = models.CharField(max_length=1024, blank=True, default='')
    featured_image_alt = models.CharField(max_length=250, blank=True, default='',
                                          help_text='Optional authored plain-text alternative text.')

    status = models.CharField(max_length=12, choices=PublicationStatus.choices,
                              default=PublicationStatus.DRAFT)
    published_at = models.DateTimeField(blank=True, null=True,
                                        help_text='Time of the most recent publication.')
    # COMM-06: incremented whenever start_time/end_time change (``services.update_event``);
    # reminder occurrence keys include it, so a reschedule obsoletes pending reminders.
    schedule_revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   blank=True, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=['status', 'start_time'], name='dawah_event_status_start')]

    def __str__(self):
        return self.title

    @property
    def has_featured_image(self):
        return (bool(self.featured_image_object_key)
                and self.featured_image_storage_status == ImageStorageStatus.READY)

    @property
    def featured_image_url(self):
        """Public URL of the promoted featured image, only while published."""
        if self.status == PublicationStatus.PUBLISHED and self.has_featured_image and self.featured_image_public_url:
            return self.featured_image_public_url
        return None

    def publication_errors(self):
        """Field errors that prevent publication of the stored record."""
        errors = {}
        for name in ('title', 'category', 'speakers', 'location'):
            if not (getattr(self, name) or '').strip():
                errors[name] = ['This field is required for publication.']
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            errors['end_time'] = ['End time must be after start time.']
        return errors


class EventResourceQuerySet(models.QuerySet):
    def visible(self):
        """Resources any reader may discover: published, with a published parent
        and a deliverable asset/reference. Unpublishing the parent hides them
        without rewriting their own status."""
        # Uploaded types need a ready validated asset that was promoted to the
        # public bucket. Video needs a validated YouTube video ID (DAWAH-05).
        uploaded = (Q(type__in=EventResource.UPLOADED_TYPES, storage_status=EventResource.StorageStatus.READY)
                    & ~Q(private_object_key='') & ~Q(public_media_url=''))
        video = Q(type=EventResource.Type.VIDEO) & ~Q(youtube_video_id='')
        return self.filter(
            uploaded | video,
            status=PublicationStatus.PUBLISHED,
            event__status=PublicationStatus.PUBLISHED,
        )


class EventResource(models.Model):
    class Type(models.TextChoices):
        DOC = 'doc', 'Document'
        VIDEO = 'vid', 'Video'
        AUDIO = 'aud', 'Audio'

    class StorageStatus(models.TextChoices):
        NONE = 'none', 'No asset'
        PENDING = 'pending', 'Upload pending'
        READY = 'ready', 'Ready'
        FAILED = 'failed', 'Upload failed'
        NOT_APPLICABLE = 'not_applicable', 'Not applicable'

    UPLOADED_TYPES = (Type.AUDIO, Type.DOC)

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='resources')

    title = models.CharField(max_length=250)
    type = models.CharField(max_length=3, choices=Type)
    # Legacy local upload. Not validated, not served, never makes a resource ready.
    file = models.FileField(upload_to='event_resources/', blank=True, null=True)
    duration = models.PositiveSmallIntegerField(blank=True, null=True, help_text="Duration in seconds")

    status = models.CharField(max_length=12, choices=PublicationStatus.choices,
                              default=PublicationStatus.DRAFT)
    storage_status = models.CharField(max_length=16, choices=StorageStatus.choices,
                                      default=StorageStatus.NONE)
    published_at = models.DateTimeField(blank=True, null=True,
                                        help_text='Time of the most recent publication.')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   blank=True, null=True, related_name='+')

    # Validated asset (DAWAH-02). Private keys/ETags are server-only and never
    # serialized; ``public_media_url`` is set only by publish promotion.
    private_object_key = models.CharField(max_length=512, blank=True, default='',
                                          help_text='Server-only validated private copy.')
    etag = models.CharField(max_length=128, blank=True, default='')
    content_type = models.CharField(max_length=100, blank=True, default='')
    size_bytes = models.BigIntegerField(blank=True, null=True)
    file_name = models.CharField(max_length=120, blank=True, default='',
                                 help_text='Sanitized original file name.')
    uploaded_at = models.DateTimeField(blank=True, null=True,
                                       help_text='When the current validated asset was attached.')
    public_object_key = models.CharField(max_length=512, blank=True, default='')
    public_media_url = models.CharField(max_length=1024, blank=True, default='',
                                        help_text='Canonical public delivery URL of the promoted copy.')

    # External video reference (DAWAH-05): only the validated ID is stored; the
    # watch/embed URLs are constructed from it.
    youtube_video_id = models.CharField(max_length=11, blank=True, default='',
                                        help_text='Validated YouTube video ID (video resources only).')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventResourceQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=['event', 'status'], name='dawah_resource_event_status')]
        constraints = [models.CheckConstraint(
            condition=Q(youtube_video_id='') | Q(youtube_video_id__regex=r'^[A-Za-z0-9_-]{11}$'),
            name='dawah_resource_youtube_id_format')]

    def __str__(self):
        return self.title

    @staticmethod
    def initial_storage_status(resource_type):
        if resource_type == EventResource.Type.VIDEO:
            return EventResource.StorageStatus.NOT_APPLICABLE
        return EventResource.StorageStatus.NONE

    @property
    def media_url(self):
        """Canonical public delivery URL, only while published after promotion."""
        if self.status == PublicationStatus.PUBLISHED and self.public_media_url:
            return self.public_media_url
        return None

    @property
    def has_asset(self):
        return bool(self.private_object_key) and self.storage_status == self.StorageStatus.READY

    @property
    def playback(self):
        """Typed playback metadata for a video with a validated YouTube ID, else None."""
        if self.type != self.Type.VIDEO:
            return None
        return youtube.playback_for(self.youtube_video_id)

    def media_ready(self):
        """Whether the resource has a deliverable asset/reference.

        Audio/documents need a validated ready upload (DAWAH-02/07). Video needs
        a validated YouTube video ID.
        """
        if self.type == self.Type.VIDEO:
            return self.playback is not None
        return self.has_asset


class UploadIntent(models.Model):
    """Bounded file-upload control record (D2): issued → validating → terminal.

    The browser may only PUT to ``staging_key`` (signed, private, temporary).
    Finalize copies the staged object at its observed ETag to the server-only
    ``validation_key``, validates that immutable copy and attaches it.
    Audio/document intents target a ``resource``; image intents target the
    ``event`` (its featured image). Exactly one of the two is set.
    """

    class Kind(models.TextChoices):
        AUDIO = 'audio', 'Audio'
        DOCUMENT = 'document', 'Document'
        IMAGE = 'image', 'Featured image'

    class Status(models.TextChoices):
        ISSUED = 'issued', 'Issued'
        VALIDATING = 'validating', 'Validating'
        READY = 'ready', 'Ready'
        REJECTED = 'rejected', 'Rejected'
        FAILED = 'failed', 'Failed'
        EXPIRED = 'expired', 'Expired'

    TERMINAL = (Status.READY, Status.REJECTED, Status.FAILED, Status.EXPIRED)
    OPEN = (Status.ISSUED, Status.VALIDATING)

    resource = models.ForeignKey(EventResource, on_delete=models.CASCADE, related_name='upload_intents',
                                 blank=True, null=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='image_upload_intents',
                              blank=True, null=True, help_text='Featured-image intents only.')
    # SET_NULL: deleting an admin must not cascade-delete intents (their keys
    # would be orphaned unrecorded). Unowned intents can no longer be finalized
    # (404) and are expired/cleaned by the cleanup command.
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   related_name='+')
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.AUDIO)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ISSUED)

    staging_key = models.CharField(max_length=512, unique=True)
    validation_key = models.CharField(max_length=512, unique=True)
    file_name = models.CharField(max_length=120)
    content_type = models.CharField(max_length=100)
    size_bytes = models.BigIntegerField()
    expires_at = models.DateTimeField()

    lease_token = models.CharField(max_length=64, blank=True, default='')
    lease_expires_at = models.DateTimeField(blank=True, null=True)
    attempts = models.PositiveIntegerField(default=0)

    observed_etag = models.CharField(max_length=128, blank=True, default='',
                                     help_text='Staged ETag copied to the validation key.')
    validation_etag = models.CharField(max_length=128, blank=True, default='')
    observed_size = models.BigIntegerField(blank=True, null=True)
    duration = models.PositiveIntegerField(blank=True, null=True)

    error_code = models.CharField(max_length=40, blank=True, default='')
    error_message = models.CharField(max_length=200, blank=True, default='')
    finalized_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'expires_at'], name='dawah_intent_status_exp')]
        constraints = [models.CheckConstraint(
            condition=(Q(kind='image', event__isnull=False, resource__isnull=True)
                       | (~Q(kind='image') & Q(event__isnull=True, resource__isnull=False))),
            name='dawah_intent_single_target')]

    def __str__(self):
        return f'Upload {self.pk} ({self.status})'


class StorageCleanup(models.Model):
    """Recorded provider cleanup (orphans, replaced/unpublished copies).

    Rows survive resource deletion (no FK). Processing never deletes a key a
    resource still references (``manage.py dawah_storage_cleanup``).
    """

    class Bucket(models.TextChoices):
        PRIVATE = 'private', 'Private'
        PUBLIC = 'public', 'Public'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        DONE = 'done', 'Done'
        SKIPPED = 'skipped', 'Skipped (still referenced)'
        CANCELLED = 'cancelled', 'Cancelled (object was attached)'

    bucket = models.CharField(max_length=8, choices=Bucket.choices)
    key = models.CharField(max_length=512)
    reason = models.CharField(max_length=40)
    resource_id = models.BigIntegerField(blank=True, null=True)
    event_id = models.BigIntegerField(blank=True, null=True, help_text='Featured-image objects.')
    upload_id = models.BigIntegerField(blank=True, null=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    public_url = models.CharField(max_length=1024, blank=True, default='',
                                  help_text='Public delivery URL to purge from the CDN after deletion.')
    not_before = models.DateTimeField(blank=True, null=True,
                                      help_text='Do not process before this time (in-flight promotion grace).')
    attempts = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=40, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'id'], name='dawah_cleanup_status')]

    def __str__(self):
        return f'Cleanup {self.pk} ({self.bucket}, {self.status})'


# ---------------------------------------------------------------- communication (D4)
#
# COMM-01 owns drafts, audiences, the queue transaction and the frozen recipient
# snapshot. The outbox worker/provider fields on ``Delivery`` and the
# ``DeliveryAttempt`` audit rows exist so COMM-02 (worker/Resend/retries) and
# COMM-04 (rich content) extend this schema additively. Lock order for every
# broadcast write: broadcast row, then its deliveries (ascending id).

class BroadcastState(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    QUEUED = 'queued', 'Queued'
    PROCESSING = 'processing', 'Processing'
    COMPLETED = 'completed', 'Completed'
    COMPLETED_WITH_ERRORS = 'completed_with_errors', 'Completed with errors'
    UNCERTAIN = 'uncertain', 'Uncertain'


class BodyFormat(models.TextChoices):
    PLAIN = 'plain', 'Plain text'
    RICH = 'rich', 'Rich text'


class AudienceType(models.TextChoices):
    ALL = 'all', 'All active students'
    DEPARTMENT = 'department', 'One department'
    LEVEL = 'level', 'One level'


class DeliveryState(models.TextChoices):
    QUEUED = 'queued', 'Queued'
    CLAIMED = 'claimed', 'Claimed'
    SENDING = 'sending', 'Sending'
    SENT = 'sent', 'Sent (provider accepted)'
    FAILED = 'failed', 'Failed'
    UNCERTAIN = 'uncertain', 'Uncertain'
    SKIPPED = 'skipped', 'Skipped'
    CANCELLED = 'cancelled', 'Cancelled'


# Deliveries not yet resolved; reported under ``counts.queued``.
OUTSTANDING_DELIVERY_STATES = (DeliveryState.QUEUED, DeliveryState.CLAIMED, DeliveryState.SENDING)


class Broadcast(models.Model):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True,
                                   related_name='+')
    subject = models.CharField(max_length=200)
    body = models.TextField(help_text='Plain text (rich drafts: the server-generated text fallback).')
    body_format = models.CharField(max_length=8, choices=BodyFormat.choices, default=BodyFormat.PLAIN)
    body_html = models.TextField(blank=True, null=True,
                                 help_text='Canonical sanitized HTML of rich drafts (COMM-04); null for plain.')

    audience_type = models.CharField(max_length=12, choices=AudienceType.choices)
    audience_department = models.CharField(max_length=100, blank=True, default='',
                                           help_text='Trimmed display value; matched case-insensitively.')
    audience_level = models.CharField(max_length=3, blank=True, default='')

    state = models.CharField(max_length=24, choices=BroadcastState.choices, default=BroadcastState.DRAFT)
    version = models.PositiveIntegerField(default=1)

    # Frozen at queue time and never changed afterwards.
    queued_at = models.DateTimeField(blank=True, null=True)
    queued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True,
                                  related_name='+')
    recipient_count = models.PositiveIntegerField(blank=True, null=True)
    snapshot_subject = models.CharField(max_length=200, blank=True, default='')
    snapshot_text = models.TextField(blank=True, default='')
    snapshot_html = models.TextField(blank=True, default='')
    snapshot_template_version = models.CharField(max_length=32, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['state', 'updated_at'], name='dawah_broadcast_state_updated')]
        constraints = [
            models.CheckConstraint(
                condition=(Q(audience_type=AudienceType.ALL, audience_department='', audience_level='')
                           | (Q(audience_type=AudienceType.DEPARTMENT, audience_level='')
                              & ~Q(audience_department=''))
                           | Q(audience_type=AudienceType.LEVEL, audience_department='',
                               audience_level__in=['100', '200', '300', '400', '500'])),
                name='dawah_broadcast_audience_selector'),
            models.CheckConstraint(
                condition=Q(state=BroadcastState.DRAFT) | (Q(queued_at__isnull=False)
                                                           & Q(recipient_count__isnull=False)),
                name='dawah_broadcast_queued_snapshot'),
            # COMM-04: plain rows never carry HTML; rich rows always carry canonical HTML.
            models.CheckConstraint(
                condition=(Q(body_format=BodyFormat.PLAIN, body_html__isnull=True)
                           | (Q(body_format=BodyFormat.RICH, body_html__isnull=False) & ~Q(body_html=''))),
                name='dawah_broadcast_body_format_html'),
        ]

    def __str__(self):
        return self.subject

    @property
    def is_draft(self):
        return self.state == BroadcastState.DRAFT


class Delivery(models.Model):
    """One logical recipient of a queued broadcast, across retries."""
    broadcast = models.ForeignKey(Broadcast, on_delete=models.PROTECT, related_name='deliveries')
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    snapshot_email = models.EmailField(max_length=254, help_text='Address frozen at queue time.')
    state = models.CharField(max_length=12, choices=DeliveryState.choices, default=DeliveryState.QUEUED)

    # Outbox bookkeeping (COMM-02 worker).
    attempt_count = models.PositiveSmallIntegerField(default=0, help_text='Attempts in the current generation.')
    retry_generation = models.PositiveSmallIntegerField(default=0, help_text='0 = original send; +1 per manual retry.')
    next_attempt_at = models.DateTimeField(blank=True, null=True)
    lease_token = models.UUIDField(blank=True, null=True)
    lease_expires_at = models.DateTimeField(blank=True, null=True)
    error_code = models.CharField(max_length=64, blank=True, default='', help_text='Safe code, never raw errors.')
    sent_at = models.DateTimeField(blank=True, null=True)
    nonacceptance_known = models.BooleanField(
        default=False,
        help_text='Set only by the worker on positive evidence the provider did not accept this generation '
                  '(definitive rejection, or failure before any provider handoff). Required for manual retry.')

    # Provider request identity for the current generation (stable across replays).
    provider_email_id = models.CharField(max_length=128, blank=True, default='')
    idempotency_key = models.CharField(max_length=256, blank=True, default='')
    request_snapshot = models.JSONField(blank=True, null=True)
    request_hash = models.CharField(max_length=64, blank=True, default='')
    first_dispatch_at = models.DateTimeField(blank=True, null=True)
    idempotency_deadline_at = models.DateTimeField(blank=True, null=True)

    retry_requested_at = models.DateTimeField(blank=True, null=True)
    retry_requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True,
                                           null=True, related_name='+')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'deliveries'
        constraints = [models.UniqueConstraint(fields=['broadcast', 'student'], name='dawah_delivery_unique_recipient')]
        indexes = [
            models.Index(fields=['broadcast', 'state'], name='dawah_delivery_broadcast_state'),
            models.Index(fields=['state', 'next_attempt_at'], name='dawah_delivery_due'),
        ]

    def __str__(self):
        return f'Delivery {self.pk} of broadcast {self.broadcast_id}'

    @property
    def is_retry_eligible(self):
        """Positive evidence of nonacceptance only (docs/communication-outbox.md):
        a failure without ``nonacceptance_known`` (payload conflict, expired
        safety deadline, ambiguity) is never retryable."""
        return (self.state == DeliveryState.FAILED and self.nonacceptance_known
                and not self.provider_email_id and self.sent_at is None)


class DeliveryGeneration(models.Model):
    """Append-only evidence of a closed retry generation: the provider request
    identity and outcome, copied before manual retry resets the live fields."""
    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name='generations')
    generation = models.PositiveSmallIntegerField()
    state = models.CharField(max_length=12, choices=DeliveryState.choices)
    attempt_count = models.PositiveSmallIntegerField()
    error_code = models.CharField(max_length=64, blank=True, default='')
    nonacceptance_known = models.BooleanField()
    provider_email_id = models.CharField(max_length=128, blank=True, default='')
    idempotency_key = models.CharField(max_length=256, blank=True, default='')
    request_snapshot = models.JSONField(blank=True, null=True)
    request_hash = models.CharField(max_length=64, blank=True, default='')
    first_dispatch_at = models.DateTimeField(blank=True, null=True)
    idempotency_deadline_at = models.DateTimeField(blank=True, null=True)
    requested_at = models.DateTimeField(blank=True, null=True,
                                                help_text='When this generation was requested (null: original).')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True,
                                             null=True, related_name='+')
    closed_at = models.DateTimeField(help_text='When a manual retry closed this generation.')
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True,
                                  related_name='+', help_text='Admin whose retry closed this generation.')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['delivery', 'generation'],
                                               name='dawah_delivery_generation_unique')]


class DeliveryAttempt(models.Model):
    """Append-only audit of one provider handoff attempt (written by COMM-02)."""

    class Outcome(models.TextChoices):
        ACCEPTED = 'accepted', 'Accepted'
        RETRYABLE = 'retryable', 'Retryable failure'
        FAILED = 'failed', 'Definitive failure'
        UNCERTAIN = 'uncertain', 'Uncertain'
        SKIPPED = 'skipped', 'Skipped'
        LEASE_LOST = 'lease_lost', 'Lease lost'

    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name='attempts')
    generation = models.PositiveSmallIntegerField()
    attempt = models.PositiveSmallIntegerField()
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(blank=True, null=True)
    outcome = models.CharField(max_length=12, choices=Outcome.choices, blank=True, default='')
    error_code = models.CharField(max_length=64, blank=True, default='')
    provider_email_id = models.CharField(max_length=128, blank=True, default='',
                                         help_text='Provider email ID returned to this attempt, if any.')
    # Evidence from a worker that finished this attempt after losing its lease.
    late_outcome = models.CharField(max_length=12, choices=Outcome.choices, blank=True, default='',
                                    help_text='Transport outcome reported after the lease was lost.')
    late_error_code = models.CharField(max_length=64, blank=True, default='')
    late_recorded_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['delivery', 'generation', 'attempt'],
                                               name='dawah_delivery_attempt_unique')]


# ---------------------------------------------------------------- automation (D4, COMM-06)
#
# Rules start disabled. The outbox scheduler tick (``automations.tick``) turns
# each due occurrence of an enabled rule into exactly one ``AutomationRun``
# (unique per rule/occurrence_key) and, when there is content and an audience,
# one ordinary queued ``Broadcast`` with its frozen recipient snapshot. Lock
# order: rule, then event, then broadcast, then deliveries.

AUTOMATION_CATEGORIES = ('dawah_night', 'usroh', 'special_lecture', 'jumuah')


class AutomationKind(models.TextChoices):
    WEEKLY_DIGEST = 'weekly_digest', 'Weekly summary'
    EVENT_REMINDER = 'event_reminder', 'Event reminder'


class AutomationRule(models.Model):
    kind = models.CharField(max_length=16, choices=AutomationKind.choices)
    enabled = models.BooleanField(default=False)
    active_from = models.DateTimeField(
        blank=True, null=True,
        help_text='Occurrences scheduled before this (last enable or schedule change) are never materialized.')

    audience_type = models.CharField(max_length=12, choices=AudienceType.choices, default=AudienceType.ALL)
    audience_department = models.CharField(max_length=100, blank=True, default='')
    audience_level = models.CharField(max_length=3, blank=True, default='')
    categories = models.JSONField(default=list, blank=True,
                                  help_text='Lower-case category tokens; empty (weekly only) means all four.')

    timezone = models.CharField(max_length=64, default='Africa/Lagos')
    weekday = models.PositiveSmallIntegerField(blank=True, null=True, help_text='Weekly: 0 = Monday.')
    local_time = models.TimeField(blank=True, null=True, help_text='Weekly: local time of the occurrence.')
    offset_minutes = models.PositiveIntegerField(blank=True, null=True,
                                                 help_text='Reminder: minutes before the event start.')

    subject = models.CharField(max_length=200)
    body_format = models.CharField(max_length=8, choices=BodyFormat.choices, default=BodyFormat.PLAIN)
    body = models.TextField(help_text='Plain template (rich: the generated text fallback).')
    body_html = models.TextField(blank=True, null=True, help_text='Rich templates: canonical sanitized HTML.')

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True,
                              related_name='+', help_text='Must be an active admin for new runs.')
    version = models.PositiveIntegerField(default=1)
    # Bounded materialization retries: consecutive failures of one occurrence.
    failure_key = models.CharField(max_length=200, blank=True, default='')
    failure_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['enabled', 'kind'], name='dawah_automation_enabled')]
        constraints = [
            models.CheckConstraint(
                condition=(Q(kind=AutomationKind.WEEKLY_DIGEST, weekday__isnull=False, weekday__lte=6,
                             local_time__isnull=False, offset_minutes__isnull=True)
                           | Q(kind=AutomationKind.EVENT_REMINDER, weekday__isnull=True, local_time__isnull=True,
                               offset_minutes__gte=1)),
                name='dawah_automation_schedule_shape'),
            models.CheckConstraint(
                condition=(Q(audience_type=AudienceType.ALL, audience_department='', audience_level='')
                           | (Q(audience_type=AudienceType.DEPARTMENT, audience_level='')
                              & ~Q(audience_department=''))
                           | Q(audience_type=AudienceType.LEVEL, audience_department='',
                               audience_level__in=['100', '200', '300', '400', '500'])),
                name='dawah_automation_audience_selector'),
            models.CheckConstraint(
                condition=(Q(body_format=BodyFormat.PLAIN, body_html__isnull=True)
                           | (Q(body_format=BodyFormat.RICH, body_html__isnull=False) & ~Q(body_html=''))),
                name='dawah_automation_body_format_html'),
        ]

    def __str__(self):
        return f'{self.kind} rule {self.pk}'


class AutomationRunState(models.TextChoices):
    PENDING = 'pending', 'Pending'
    QUEUED = 'queued', 'Queued (broadcast materialized)'
    SKIPPED = 'skipped', 'Skipped'
    MISSED = 'missed', 'Missed'
    CANCELLED = 'cancelled', 'Cancelled'
    BLOCKED = 'blocked', 'Blocked'
    FAILED = 'failed', 'Failed'


class AutomationRun(models.Model):
    """One occurrence of a rule. ``state`` is stored for unmaterialized
    outcomes; once a broadcast exists the API derives queued/processing/
    completed/... from it (see ``automations.run_state``)."""
    rule = models.ForeignKey(AutomationRule, on_delete=models.PROTECT, related_name='runs')
    occurrence_key = models.CharField(max_length=200)
    scheduled_at = models.DateTimeField()
    state = models.CharField(max_length=12, choices=AutomationRunState.choices, default=AutomationRunState.PENDING)
    reason_code = models.CharField(max_length=40, blank=True, default='')
    broadcast = models.OneToOneField(Broadcast, on_delete=models.PROTECT, blank=True, null=True,
                                     related_name='automation_run')
    window_start = models.DateTimeField(blank=True, null=True)
    window_end = models.DateTimeField(blank=True, null=True)
    # Plain ids (no FK): run history must not block deleting an unpublished event.
    event_id = models.BigIntegerField(blank=True, null=True)
    event_revision = models.PositiveIntegerField(blank=True, null=True)
    source = models.JSONField(default=dict, blank=True,
                              help_text='Provenance snapshot: event ids/titles/categories/start times.')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['rule', 'occurrence_key'],
                                               name='dawah_automation_run_unique_occurrence')]
        indexes = [models.Index(fields=['rule', 'scheduled_at'], name='dawah_run_rule_scheduled'),
                   models.Index(fields=['event_id', 'state'], name='dawah_run_event_state')]

    def __str__(self):
        return f'Run {self.occurrence_key} ({self.state})'
