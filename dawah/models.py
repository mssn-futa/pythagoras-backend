"""Da'wah events and their resources (D2).

Publication is explicit: every event/resource starts as ``draft`` and only the
publish/unpublish actions change ``status``. Public discovery is defined in one
place (``EventQuerySet.visible`` / ``EventResourceQuerySet.visible``) so later
packages extend it additively:

* DAWAH-02 adds R2 object metadata/UploadIntent and the path that sets
  ``storage_status=ready`` and derives ``media_url`` for audio.
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
        # Uploaded types need a ready asset. Video is never deliverable until
        # DAWAH-05 adds the validated reference condition
        # (``| Q(type=VIDEO, youtube_video_id__isnull=False)``).
        return self.filter(
            Q(type__in=[EventResource.Type.AUDIO, EventResource.Type.DOC],
              storage_status=EventResource.StorageStatus.READY),
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
        READY = 'ready', 'Ready'
        NOT_APPLICABLE = 'not_applicable', 'Not applicable'

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
        """Canonical public delivery URL. DAWAH-02/07 derive it after promotion."""
        return None

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
        return self.storage_status == self.StorageStatus.READY
