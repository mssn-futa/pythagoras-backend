from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from pythagoras.serializers import UTCDateTimeField

from . import uploads
from .media import MP3_CONTENT_TYPE, MP3_MAX_BYTES
from .models import Event, EventResource, PublicationStatus

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

class EventSummarySerializer(serializers.ModelSerializer):
    start_time = UTCDateTimeField(read_only=True)

    class Meta:
        model = Event
        fields = ['id', 'title', 'speakers', 'category', 'start_time', 'summary']
        read_only_fields = fields


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

    @extend_schema_field(serializers.URLField(
        allow_null=True, help_text='Admin readers only: short-lived signed preview of the private ready asset '
                                   '(R2_PREVIEW_TTL_SECONDS). Never persist or share it. Null otherwise.'))
    def get_preview_url(self, obj):
        if not is_admin_context(self):
            return None
        return uploads.preview_url(obj)

    @extend_schema_field({'type': 'object', 'nullable': True, 'additionalProperties': True,
                          'description': 'Typed playback metadata (video: DAWAH-05).'})
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
                   'media_url', 'playback', 'content_type', 'size_bytes', 'uploaded_at', 'preview_url',
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
    created_by = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = ['id', 'title', 'category', 'tags', 'description', 'summary', 'speakers', 'location',
                  'start_time', 'end_time', 'status', 'published_at', 'featured_image_url',
                  'created_at', 'updated_at', 'created_by']
        read_only_fields = fields

    @extend_schema_field(serializers.URLField(allow_null=True, help_text='Ready featured image (DAWAH-07).'))
    def get_featured_image_url(self, obj):
        return obj.featured_image_url

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

    class Meta:
        model = Event
        fields = ['title', 'category', 'tags', 'description', 'summary', 'speakers', 'location',
                  'start_time', 'end_time']

    def validate(self, attrs):
        start = attrs.get('start_time', getattr(self.instance, 'start_time', None))
        end = attrs.get('end_time', getattr(self.instance, 'end_time', None))
        if start and end and end <= start:
            raise serializers.ValidationError({'end_time': ['End time must be after start time.']})
        return attrs


class EventResourceWriteSerializer(StrictWriteMixin, serializers.ModelSerializer):
    """Metadata only. The URL's event owns the association; files are not accepted
    (durable uploads use DAWAH-02's intent/finalize protocol)."""

    class Meta:
        model = EventResource
        fields = ['title', 'type', 'duration']
        extra_kwargs = {'duration': {'min_value': 1}}

    def validate_type(self, value):
        instance = self.instance
        if instance is not None and value != instance.type:
            if instance.status == PublicationStatus.PUBLISHED:
                raise serializers.ValidationError('Unpublish this resource before changing its type.')
            if instance.storage_status == EventResource.StorageStatus.READY:
                raise serializers.ValidationError('A resource with a ready asset cannot change type.')
        return value



# ---------------------------------------------------------------- uploads

class AudioUploadIntentSerializer(serializers.Serializer):
    """Upload-intent metadata. Bytes go directly to storage, never to Django."""
    file_name = serializers.CharField(max_length=255)
    content_type = serializers.CharField(max_length=100)
    size_bytes = serializers.IntegerField(min_value=1)

    def to_internal_value(self, data):
        if hasattr(data, 'keys'):
            rejected = sorted(set(data.keys()) - set(self.fields))
            if rejected:
                raise serializers.ValidationError({name: [PROTECTED_FIELD] for name in rejected})
        return super().to_internal_value(data)

    def validate_file_name(self, value):
        if any(ord(ch) < 32 or ch in '/\\' for ch in value):
            raise serializers.ValidationError('Enter a plain file name without folders.')
        if not value.lower().endswith('.mp3') or len(value) < 5:
            raise serializers.ValidationError('Only MP3 audio files (.mp3) are accepted.')
        return value

    def validate_content_type(self, value):
        if value.strip().lower() != MP3_CONTENT_TYPE:
            raise serializers.ValidationError('Only MP3 audio (audio/mpeg) is accepted.')
        return MP3_CONTENT_TYPE

    def validate_size_bytes(self, value):
        if value > MP3_MAX_BYTES:
            raise serializers.ValidationError('The file exceeds the 100 MiB audio limit.')
        return value


class UploadIntentResponseSerializer(serializers.Serializer):
    upload_id = serializers.IntegerField()
    resource_id = serializers.IntegerField()
    method = serializers.ChoiceField(choices=['PUT'])
    upload_url = serializers.URLField(help_text='Short-lived signed S3 PUT URL on the R2 S3 API host. Send the '
                                                'bytes with exactly `headers`; no Authorization header or cookies.')
    headers = serializers.DictField(child=serializers.CharField())
    expires_at = UTCDateTimeField()
    max_size_bytes = serializers.IntegerField()


class UploadStateSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    resource_id = serializers.IntegerField()
    # Plain string (not a schema enum, to avoid colliding with the publication
    # ``status`` enum name): issued | validating | ready | rejected | failed | expired.
    status = serializers.CharField(help_text='Upload state: issued, validating, ready, rejected, failed or expired.')
    error_code = serializers.CharField(allow_null=True)
    message = serializers.CharField(allow_null=True)
    expires_at = UTCDateTimeField()
    finalized_at = UTCDateTimeField(allow_null=True)


class FinalizeResponseSerializer(serializers.Serializer):
    upload = UploadStateSerializer()
    resource = EventResourceSerializer()
    preview_url = serializers.URLField(allow_null=True)


def upload_state(intent):
    return {
        'id': intent.pk, 'resource_id': intent.resource_id, 'status': intent.status,
        'error_code': intent.error_code or None, 'message': intent.error_message or None,
        'expires_at': UTCDateTimeField().to_representation(intent.expires_at),
        'finalized_at': UTCDateTimeField().to_representation(intent.finalized_at) if intent.finalized_at else None,
    }
