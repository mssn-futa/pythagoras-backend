from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from pythagoras.serializers import UTCDateTimeField

from . import uploads, youtube
from .models import Event, EventResource, PublicationStatus, UploadIntent

PROTECTED_FIELD = 'This field cannot be set or changed here.'


class AwareDateTimeField(UTCDateTimeField):
    """ISO-8601 input that must carry an explicit offset (``Z`` or ``+01:00``)."""
    default_error_messages = {'naive': 'Include a timezone offset, e.g. 2026-10-09T12:00:00Z.'}

    def to_internal_value(self, value):
        if isinstance(value, str):
            parsed = parse_datetime(value.strip())
            if parsed is not None and parsed.tzinfo is None:
                self.fail('naive')
        return super().to_internal_value(value)


class StrictWriteMixin:
    """Reject keys that are not writable here (IDs, parent, status, files...)."""

    def to_internal_value(self, data):
        if hasattr(data, 'keys'):
            writable = {name for name, field in self.fields.items() if not field.read_only}
            rejected = sorted(set(data.keys()) - writable)
            if rejected:
                raise serializers.ValidationError({name: [PROTECTED_FIELD] for name in rejected})
        return super().to_internal_value(data)


def is_admin_context(serializer):
    return bool(serializer.context.get('is_admin'))


# ---------------------------------------------------------------- read shapes

FEATURED_IMAGE_URL_HELP = ('Public URL of the validated featured image (custom media domain); '
                           'null unless the event is published and has one.')


class EventSummarySerializer(serializers.ModelSerializer):
    start_time = UTCDateTimeField(read_only=True)
    featured_image_url = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = ['id', 'title', 'speakers', 'category', 'start_time', 'summary', 'featured_image_url']
        read_only_fields = fields

    @extend_schema_field(serializers.URLField(allow_null=True, help_text=FEATURED_IMAGE_URL_HELP))
    def get_featured_image_url(self, obj):
        return obj.featured_image_url


class ResourceFieldsMixin(serializers.Serializer):
    event_id = serializers.IntegerField(read_only=True)
    media_url = serializers.SerializerMethodField()
    playback = serializers.SerializerMethodField()
    published_at = UTCDateTimeField(read_only=True, allow_null=True)
    created_at = UTCDateTimeField(read_only=True)
    updated_at = UTCDateTimeField(read_only=True)
    created_by = serializers.SerializerMethodField()
    legacy_file_present = serializers.SerializerMethodField()
    uploaded_at = UTCDateTimeField(read_only=True, allow_null=True)
    content_type = serializers.SerializerMethodField()
    size_bytes = serializers.SerializerMethodField()
    file_name = serializers.SerializerMethodField()
    preview_url = serializers.SerializerMethodField()

    @extend_schema_field(serializers.URLField(
        allow_null=True, help_text='Canonical public delivery URL (custom media domain); null unless published.'))
    def get_media_url(self, obj):
        return obj.media_url

    @extend_schema_field(serializers.CharField(allow_null=True, help_text='Validated asset MIME type.'))
    def get_content_type(self, obj):
        return obj.content_type if obj.has_asset else None

    @extend_schema_field(serializers.IntegerField(allow_null=True, help_text='Validated asset size in bytes.'))
    def get_size_bytes(self, obj):
        return obj.size_bytes if obj.has_asset else None

    @extend_schema_field(serializers.CharField(
        allow_null=True, help_text='Sanitized download file name of the validated asset (e.g. agenda.pdf).'))
    def get_file_name(self, obj):
        return (obj.file_name or None) if obj.has_asset else None

    @extend_schema_field(serializers.URLField(
        allow_null=True, help_text='Admin readers only: short-lived signed preview of the private ready asset '
                                   '(R2_PREVIEW_TTL_SECONDS). Never persist or share it. Null otherwise.'))
    def get_preview_url(self, obj):
        if not is_admin_context(self):
            return None
        return uploads.preview_url(obj)

    @extend_schema_field({'type': 'object', 'nullable': True, 'additionalProperties': True,
                          'description': 'Video with a validated YouTube ID: {provider:"youtube", video_id, '
                                         'watch_url, embed_url} (constructed server-side); null otherwise.'})
    def get_playback(self, obj):
        return obj.playback

    @extend_schema_field(serializers.IntegerField(allow_null=True, help_text='Admin readers only; null otherwise.'))
    def get_created_by(self, obj):
        return obj.created_by_id if is_admin_context(self) else None

    @extend_schema_field(serializers.BooleanField(
        allow_null=True, help_text='Admin readers only: a legacy local file exists (never served). Null otherwise.'))
    def get_legacy_file_present(self, obj):
        return bool(obj.file) if is_admin_context(self) else None


RESOURCE_FIELDS = ['id', 'event_id', 'title', 'type', 'status', 'storage_status', 'duration',
                   'media_url', 'playback', 'content_type', 'size_bytes', 'file_name', 'uploaded_at', 'preview_url',
                   'published_at', 'created_at', 'updated_at', 'created_by', 'legacy_file_present']


class EventResourceNestedSerializer(ResourceFieldsMixin, serializers.ModelSerializer):
    """Resource inside its event's detail payload."""

    class Meta:
        model = EventResource
        fields = RESOURCE_FIELDS
        read_only_fields = fields


class EventResourceSerializer(ResourceFieldsMixin, serializers.ModelSerializer):
    """Resource with parent metadata for stable detail links and the archive."""
    event = EventSummarySerializer(read_only=True)

    class Meta:
        model = EventResource
        fields = [*RESOURCE_FIELDS, 'event']
        read_only_fields = fields


class EventSerializer(serializers.ModelSerializer):
    start_time = UTCDateTimeField(read_only=True)
    end_time = UTCDateTimeField(read_only=True)
    published_at = UTCDateTimeField(read_only=True, allow_null=True)
    created_at = UTCDateTimeField(read_only=True)
    updated_at = UTCDateTimeField(read_only=True)
    featured_image_url = serializers.SerializerMethodField()
    featured_image_preview_url = serializers.SerializerMethodField()
    featured_image_uploaded_at = UTCDateTimeField(read_only=True, allow_null=True)
    created_by = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = ['id', 'title', 'category', 'tags', 'description', 'summary', 'speakers', 'location',
                  'start_time', 'end_time', 'status', 'published_at', 'featured_image_url',
                  'featured_image_preview_url', 'featured_image_storage_status', 'featured_image_uploaded_at',
                  'featured_image_alt', 'created_at', 'updated_at', 'created_by']
        read_only_fields = fields

    @extend_schema_field(serializers.URLField(allow_null=True, help_text=FEATURED_IMAGE_URL_HELP))
    def get_featured_image_url(self, obj):
        return obj.featured_image_url

    @extend_schema_field(serializers.URLField(
        allow_null=True, help_text='Admin readers only: short-lived signed preview of the private validated '
                                   'featured image (any event state). Never persist or share it. Null otherwise.'))
    def get_featured_image_preview_url(self, obj):
        if not is_admin_context(self):
            return None
        return uploads.image_preview_url(obj)

    @extend_schema_field(serializers.IntegerField(allow_null=True, help_text='Admin readers only; null otherwise.'))
    def get_created_by(self, obj):
        return obj.created_by_id if is_admin_context(self) else None


class EventDetailSerializer(EventSerializer):
    """Event with the resources the reader may see (all of them for admins)."""
    resources = serializers.SerializerMethodField()

    class Meta(EventSerializer.Meta):
        fields = [*EventSerializer.Meta.fields, 'resources']
        read_only_fields = fields

    @extend_schema_field(EventResourceNestedSerializer(many=True))
    def get_resources(self, obj):
        queryset = obj.resources.all()
        if not is_admin_context(self):
            queryset = queryset.visible()
        return EventResourceNestedSerializer(queryset.order_by('id'), many=True, context=self.context).data


# ---------------------------------------------------------------- write shapes

class EventWriteSerializer(StrictWriteMixin, serializers.ModelSerializer):
    start_time = AwareDateTimeField()
    end_time = AwareDateTimeField()
    tags = serializers.CharField(max_length=250, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True, trim_whitespace=False)
    summary = serializers.CharField(required=False, allow_blank=True, allow_null=True, trim_whitespace=False)

    featured_image_alt = serializers.CharField(
        max_length=250, required=False, allow_blank=True,
        help_text='Optional plain-text alternative text for the featured image (at most 250 characters).')

    class Meta:
        model = Event
        fields = ['title', 'category', 'tags', 'description', 'summary', 'speakers', 'location',
                  'start_time', 'end_time', 'featured_image_alt']

    def validate_featured_image_alt(self, value):
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or '<' in value or '>' in value:
            raise serializers.ValidationError('Enter plain text without markup or control characters.')
        return value

    def validate(self, attrs):
        start = attrs.get('start_time', getattr(self.instance, 'start_time', None))
        end = attrs.get('end_time', getattr(self.instance, 'end_time', None))
        if start and end and end <= start:
            raise serializers.ValidationError({'end_time': ['End time must be after start time.']})
        return attrs


class EventResourceWriteSerializer(StrictWriteMixin, serializers.ModelSerializer):
    """Metadata only. The URL's event owns the association; files are not accepted
    (durable uploads use DAWAH-02's intent/finalize protocol)."""

    youtube_url = serializers.CharField(
        required=False, write_only=True, trim_whitespace=False, max_length=youtube.MAX_URL_LENGTH,
        help_text='Video resources only: a YouTube watch/embed/shorts or youtu.be HTTPS link. Normalized to '
                  'the video ID; the submitted text is not stored.')

    class Meta:
        model = EventResource
        fields = ['title', 'type', 'duration', 'youtube_url']
        extra_kwargs = {'duration': {'min_value': 1}}

    def validate_youtube_url(self, value):
        try:
            return youtube.parse_youtube_url(value)
        except youtube.InvalidYouTubeUrl as error:
            raise serializers.ValidationError(str(error)) from None

    def validate(self, attrs):
        if 'youtube_url' in attrs:
            resource_type = attrs.get('type', getattr(self.instance, 'type', None))
            if resource_type != EventResource.Type.VIDEO:
                raise serializers.ValidationError({'youtube_url': ['Only video resources have a YouTube link.']})
            attrs['youtube_video_id'] = attrs.pop('youtube_url')
        return attrs

    def validate_type(self, value):
        instance = self.instance
        if instance is not None and value != instance.type:
            if instance.status == PublicationStatus.PUBLISHED:
                raise serializers.ValidationError('Unpublish this resource before changing its type.')
            if instance.storage_status == EventResource.StorageStatus.READY:
                raise serializers.ValidationError('A resource with a ready asset cannot change type.')
        return value



# ---------------------------------------------------------------- uploads

class UploadIntentSerializer(serializers.Serializer):
    """Upload-intent metadata. Bytes go directly to storage, never to Django.
    Subclasses name the upload kind; its rules come from ``uploads.SPECS``."""
    kind = None
    file_name = serializers.CharField(max_length=255)
    content_type = serializers.CharField(max_length=100)
    size_bytes = serializers.IntegerField(min_value=1)

    @property
    def spec(self):
        return uploads.SPECS[self.kind]

    def to_internal_value(self, data):
        if hasattr(data, 'keys'):
            rejected = sorted(set(data.keys()) - set(self.fields))
            if rejected:
                raise serializers.ValidationError({name: [PROTECTED_FIELD] for name in rejected})
        return super().to_internal_value(data)

    def types_label(self):
        return ', '.join(self.spec.types)

    def validate_file_name(self, value):
        if any(ord(ch) < 32 or ch in '/\\' for ch in value):
            raise serializers.ValidationError('Enter a plain file name without folders.')
        lowered = value.lower()
        extensions = [ext for exts in (v[1] for v in self.spec.types.values()) for ext in exts]
        if not any(lowered.endswith(ext) and len(value) > len(ext) for ext in extensions):
            raise serializers.ValidationError(f'Only {" / ".join(extensions)} files are accepted.')
        return value

    def validate_content_type(self, value):
        normalized = value.strip().lower()
        if normalized not in self.spec.types:
            raise serializers.ValidationError(f'Only {self.types_label()} is accepted.')
        return normalized

    def validate_size_bytes(self, value):
        if value > self.spec.max_bytes:
            raise serializers.ValidationError(f'The file exceeds the {self.spec.limit_label} limit.')
        return value

    def validate(self, attrs):
        content_type, name = attrs['content_type'], attrs['file_name'].lower()
        if not any(name.endswith(ext) for ext in self.spec.accepted_extensions(content_type)):
            raise serializers.ValidationError(
                {'file_name': ['The file extension does not match the declared content type.']})
        return attrs


class AudioUploadIntentSerializer(UploadIntentSerializer):
    kind = UploadIntent.Kind.AUDIO

    def validate_file_name(self, value):
        if any(ord(ch) < 32 or ch in '/\\' for ch in value):
            raise serializers.ValidationError('Enter a plain file name without folders.')
        if not value.lower().endswith('.mp3') or len(value) < 5:
            raise serializers.ValidationError('Only MP3 audio files (.mp3) are accepted.')
        return value

    def validate_content_type(self, value):
        if value.strip().lower() not in self.spec.types:
            raise serializers.ValidationError('Only MP3 audio (audio/mpeg) is accepted.')
        return value.strip().lower()


class DocumentUploadIntentSerializer(UploadIntentSerializer):
    kind = UploadIntent.Kind.DOCUMENT


class ImageUploadIntentSerializer(UploadIntentSerializer):
    kind = UploadIntent.Kind.IMAGE


class UploadIntentResponseSerializer(serializers.Serializer):
    upload_id = serializers.IntegerField()
    resource_id = serializers.IntegerField(allow_null=True, help_text='Null for a featured-image upload.')
    event_id = serializers.IntegerField()
    method = serializers.ChoiceField(choices=['PUT'])
    upload_url = serializers.URLField(help_text='Short-lived signed S3 PUT URL on the R2 S3 API host. Send the '
                                                'bytes with exactly `headers`; no Authorization header or cookies.')
    headers = serializers.DictField(child=serializers.CharField())
    expires_at = UTCDateTimeField()
    max_size_bytes = serializers.IntegerField()


class UploadStateSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    kind = serializers.CharField(help_text='audio, document or image.')
    resource_id = serializers.IntegerField(allow_null=True, help_text='Null for a featured-image upload.')
    event_id = serializers.IntegerField()
    # Plain string (not a schema enum, to avoid colliding with the publication
    # ``status`` enum name): issued | validating | ready | rejected | failed | expired.
    status = serializers.CharField(help_text='Upload state: issued, validating, ready, rejected, failed or expired.')
    error_code = serializers.CharField(allow_null=True)
    message = serializers.CharField(allow_null=True)
    expires_at = UTCDateTimeField()
    finalized_at = UTCDateTimeField(allow_null=True)


class FinalizeResponseSerializer(serializers.Serializer):
    upload = UploadStateSerializer()
    resource = EventResourceSerializer(allow_null=True, help_text='Audio/document uploads; null for an image.')
    event = EventSerializer(allow_null=True, help_text='Featured-image uploads; null otherwise.')
    preview_url = serializers.URLField(allow_null=True)


def upload_event_id(intent):
    return intent.event_id if intent.event_id else intent.resource.event_id


def upload_state(intent):
    return {
        'id': intent.pk, 'kind': intent.kind, 'resource_id': intent.resource_id,
        'event_id': upload_event_id(intent), 'status': intent.status,
        'error_code': intent.error_code or None, 'message': intent.error_message or None,
        'expires_at': UTCDateTimeField().to_representation(intent.expires_at),
        'finalized_at': UTCDateTimeField().to_representation(intent.finalized_at) if intent.finalized_at else None,
    }
