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
