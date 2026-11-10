"""Da'wah events and their resources (D2).

Publication is explicit: every event/resource starts as ``draft`` and only the
publish/unpublish actions change ``status``. Public discovery is defined in one
place (``EventQuerySet.visible`` / ``EventResourceQuerySet.visible``) so later
packages extend it additively:

* DAWAH-02 adds R2 object metadata/UploadIntent, the finalize path that sets
  ``storage_status=ready`` and the publish promotion that records the public
  ``media_url`` for audio (see ``dawah/uploads.py``).
* DAWAH-05 adds ``youtube_video_id`` and tightens the video visibility rule.
* DAWAH-07 adds document/featured-image object metadata.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q


class PublicationStatus(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    PUBLISHED = 'published', 'Published'
    UNPUBLISHED = 'unpublished', 'Unpublished'


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
    # DAWAH-07 adds validated R2 featured-image metadata alongside it.
    featured_image = models.ImageField(upload_to='events/', blank=True, null=True)

    status = models.CharField(max_length=12, choices=PublicationStatus.choices,
                              default=PublicationStatus.DRAFT)
    published_at = models.DateTimeField(blank=True, null=True,
                                        help_text='Time of the most recent publication.')
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
    def featured_image_url(self):
        """Public URL of a ready featured image. DAWAH-07 supplies durable images."""
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
        # public bucket. Video is never deliverable until DAWAH-05 adds the
        # validated reference condition
        # (``| Q(type=VIDEO, youtube_video_id__isnull=False)``).
        return self.filter(
            Q(type__in=EventResource.UPLOADED_TYPES,
              storage_status=EventResource.StorageStatus.READY)
            & ~Q(private_object_key='') & ~Q(public_media_url=''),
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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventResourceQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=['event', 'status'], name='dawah_resource_event_status')]

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
        """Typed playback metadata. DAWAH-05 supplies it for validated video."""
        return None

    def media_ready(self):
        """Whether the resource has a deliverable asset/reference.

        Audio/documents need a validated ready upload (DAWAH-02/07). Video needs
        a validated YouTube reference, which DAWAH-05 adds; until then no video
        resource can be published.
        """
        if self.type == self.Type.VIDEO:
            return False
        return self.has_asset


class UploadIntent(models.Model):
    """Bounded file-upload control record (D2): issued → validating → terminal.

    The browser may only PUT to ``staging_key`` (signed, private, temporary).
    Finalize copies the staged object at its observed ETag to the server-only
    ``validation_key``, validates that immutable copy and attaches it.
    """

    class Kind(models.TextChoices):
        AUDIO = 'audio', 'Audio'

    class Status(models.TextChoices):
        ISSUED = 'issued', 'Issued'
        VALIDATING = 'validating', 'Validating'
        READY = 'ready', 'Ready'
        REJECTED = 'rejected', 'Rejected'
        FAILED = 'failed', 'Failed'
        EXPIRED = 'expired', 'Expired'

    TERMINAL = (Status.READY, Status.REJECTED, Status.FAILED, Status.EXPIRED)
    OPEN = (Status.ISSUED, Status.VALIDATING)

    resource = models.ForeignKey(EventResource, on_delete=models.CASCADE, related_name='upload_intents')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
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

    bucket = models.CharField(max_length=8, choices=Bucket.choices)
    key = models.CharField(max_length=512)
    reason = models.CharField(max_length=40)
    resource_id = models.BigIntegerField(blank=True, null=True)
    upload_id = models.BigIntegerField(blank=True, null=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=40, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'id'], name='dawah_cleanup_status')]

    def __str__(self):
        return f'Cleanup {self.pk} ({self.bucket}, {self.status})'
