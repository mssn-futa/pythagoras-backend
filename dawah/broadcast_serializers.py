"""Broadcast request/response shapes (D4 API contract, frozen for COMM-03).

Field errors are always lists of strings keyed by a top-level field name
(audience problems are reported under ``audience``), so the shared client can
show them next to the form controls.
"""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from pythagoras.serializers import UTCDateTimeField

from . import broadcast_content, broadcasts
from .models import AudienceType, BodyFormat, Broadcast, BroadcastState, Delivery, DeliveryState
from .serializers import StrictWriteMixin

LEVELS = ('100', '200', '300', '400', '500')
DEPARTMENT_MAX_LENGTH = 100
MAX_RETRY_IDS = 100
BROADCAST_ACTIONS = ['edit', 'delete', 'preview', 'queue', 'retry']


class StrictCharField(serializers.CharField):
    """JSON string only: rejects numbers/booleans DRF would coerce to text."""

    def to_internal_value(self, data):
        if type(data) is not str:
            self.fail('invalid')
        return super().to_internal_value(data)


class StrictIntegerField(serializers.IntegerField):
    """JSON integer only: rejects "1", 1.0 and booleans."""

    def to_internal_value(self, data):
        if type(data) is not int:
            self.fail('invalid')
        return super().to_internal_value(data)


AUDIENCE_SCHEMA = {
    'type': 'object',
    'required': ['type'],
    'additionalProperties': False,
    'properties': {
        'type': {'type': 'string', 'enum': AudienceType.values},
        'department': {'type': 'string', 'maxLength': DEPARTMENT_MAX_LENGTH,
                       'description': 'type=department only. Trimmed; matched case-insensitively.'},
        'level': {'type': 'string', 'enum': list(LEVELS), 'description': 'type=level only.'},
    },
    'description': '{"type":"all"} | {"type":"department","department":"..."} | {"type":"level","level":"300"}',
}


def audience_payload(broadcast):
    audience = {'type': broadcast.audience_type}
    if broadcast.audience_type == AudienceType.DEPARTMENT:
        audience['department'] = broadcast.audience_department
    elif broadcast.audience_type == AudienceType.LEVEL:
        audience['level'] = broadcast.audience_level
    return audience


@extend_schema_field(AUDIENCE_SCHEMA)
class AudienceField(serializers.Field):
    """``{"type": ..., "department"?: ..., "level"?: ...}`` ⇄ the three model columns."""
    INVALID = 'Enter an audience object such as {"type": "all"}.'

    def __init__(self, **kwargs):
        kwargs['source'] = '*'
        super().__init__(**kwargs)

    def to_representation(self, instance):
        return audience_payload(instance)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError(self.INVALID)
        unknown = sorted(set(data) - {'type', 'department', 'level'})
        if unknown:
            raise serializers.ValidationError(f'Unexpected audience keys: {", ".join(unknown)}.')
        kind = data.get('type')
        if type(kind) is not str or kind not in AudienceType.values:
            raise serializers.ValidationError(f'Choose an audience type: {", ".join(AudienceType.values)}.')
        department, level = data.get('department'), data.get('level')
        if kind != AudienceType.DEPARTMENT and 'department' in data:
            raise serializers.ValidationError('A department applies only to a department audience.')
        if kind != AudienceType.LEVEL and 'level' in data:
            raise serializers.ValidationError('A level applies only to a level audience.')
        values = {'audience_type': kind, 'audience_department': '', 'audience_level': ''}
        if kind == AudienceType.DEPARTMENT:
            if type(department) is not str or not department.strip():
                raise serializers.ValidationError('Enter a department.')
            department = department.strip()
            if len(department) > DEPARTMENT_MAX_LENGTH:
                raise serializers.ValidationError(
                    f'Department must be {DEPARTMENT_MAX_LENGTH} characters or fewer.')
            if broadcast_content.subject_error(department):
                raise serializers.ValidationError('Department must be plain single-line text.')
            values['audience_department'] = department
        elif kind == AudienceType.LEVEL:
            if type(level) is not str or level not in LEVELS:
                raise serializers.ValidationError(f'Choose a level: {", ".join(LEVELS)}.')
            values['audience_level'] = level
        return values


# ---------------------------------------------------------------- requests

class BroadcastWriteSerializer(StrictWriteMixin, serializers.ModelSerializer):
    """Create (subject, body, audience required) or PATCH (any subset plus
    ``version``). Plain text only until COMM-04 adds rich content."""
    subject = StrictCharField(max_length=broadcast_content.SUBJECT_MAX_LENGTH,
                              help_text='Single line, at most 200 characters.')
    body = StrictCharField(trim_whitespace=False, max_length=broadcast_content.BODY_MAX_LENGTH,
                           help_text='Plain message text (line breaks kept).')
    body_format = StrictCharField(required=False, help_text='"plain" (default). Rich content arrives with COMM-04.')
    audience = AudienceField()
    version = StrictIntegerField(required=False, write_only=True, min_value=1,
                                 help_text='PATCH only: the version you loaded (optimistic concurrency).')

    class Meta:
        model = Broadcast
        fields = ['subject', 'body', 'body_format', 'audience', 'version']

    def validate_subject(self, value):
        error = broadcast_content.subject_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate_body(self, value):
        value = broadcast_content.normalize_body(value)
        if not value.strip():
            raise serializers.ValidationError('Enter the message text.')
        error = broadcast_content.body_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate_body_format(self, value):
        if value == BodyFormat.RICH:
            raise serializers.ValidationError('Rich content is not available yet; send plain text.')
        if value != BodyFormat.PLAIN:
            raise serializers.ValidationError('Choose "plain".')
        return value

    def validate_version(self, value):
        if self.instance is None:
            raise serializers.ValidationError('A new draft has no version yet.')
        return value

    def validate(self, attrs):
        attrs.pop('version', None)  # checked under the row lock by broadcasts.update_draft
        return attrs


class VersionSerializer(StrictWriteMixin, serializers.Serializer):
    version = StrictIntegerField(min_value=1, help_text='The draft version you loaded.')


class RetryRequestSerializer(StrictWriteMixin, serializers.Serializer):
    delivery_ids = serializers.ListField(child=StrictIntegerField(min_value=1), min_length=1,
                                         max_length=MAX_RETRY_IDS,
                                         help_text='Failed deliveries of this broadcast to retry (all or none).')

    def validate_delivery_ids(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError('Each delivery may be listed once.')
        return value


def parse_version(data):
    if not isinstance(data, dict):
        raise serializers.ValidationError({'non_field_errors': ['Send a JSON object.']})
    serializer = VersionSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data['version']


def patch_version(data):
    """PATCH carries ``version`` beside the draft fields."""
    if not isinstance(data, dict):
        raise serializers.ValidationError({'non_field_errors': ['Send a JSON object.']})
    if 'version' not in data:
        raise serializers.ValidationError({'version': ['This field is required.']})
    field = StrictIntegerField(min_value=1)
    try:
        return field.run_validation(data['version'])
    except serializers.ValidationError as error:
        raise serializers.ValidationError({'version': error.detail}) from None


# ---------------------------------------------------------------- responses

COUNTS_SCHEMA = {
    'type': 'object',
    'required': list(broadcasts.COUNT_KEYS),
    'properties': {key: {'type': 'integer', 'minimum': 0} for key in broadcasts.COUNT_KEYS},
    'description': 'Delivery counts. `queued` covers every unresolved delivery (queued/claimed/sending); '
                   '`sent` means provider accepted, not inbox delivery. All zero for drafts.',
}


class BroadcastSerializer(serializers.ModelSerializer):
    """Admin broadcast payload. Never contains recipient addresses."""
    audience = AudienceField(read_only=True)
    creator_id = serializers.IntegerField(source='created_by_id', read_only=True, allow_null=True)
    state = serializers.ChoiceField(choices=BroadcastState.choices, read_only=True)
    body_format = serializers.ChoiceField(choices=BodyFormat.choices, read_only=True)
    body_html = serializers.CharField(read_only=True, allow_null=True,
                                      help_text='Canonical sanitized HTML (rich drafts, COMM-04); null for plain.')
    created_at = UTCDateTimeField(read_only=True)
    updated_at = UTCDateTimeField(read_only=True)
    queued_at = UTCDateTimeField(read_only=True, allow_null=True)
    recipient_count = serializers.IntegerField(read_only=True, allow_null=True,
                                               help_text='Frozen snapshot size; null while a draft.')
    counts = serializers.SerializerMethodField()
    eligible_actions = serializers.SerializerMethodField()

    class Meta:
        model = Broadcast
        fields = ['id', 'subject', 'body', 'body_format', 'body_html', 'audience', 'state', 'version',
                  'creator_id', 'created_at', 'updated_at', 'queued_at', 'recipient_count', 'counts',
                  'eligible_actions']
        read_only_fields = fields

    @extend_schema_field(COUNTS_SCHEMA)
    def get_counts(self, obj):
        return broadcasts.counts_of(obj)

    @extend_schema_field({'type': 'array', 'items': {'type': 'string', 'enum': BROADCAST_ACTIONS},
                          'description': 'Drafts: edit, delete, preview, queue. Queued: retry while any '
                                         'delivery is a known, retryable failure.'})
    def get_eligible_actions(self, obj):
        return broadcasts.eligible_actions(obj)


class PreviewSerializer(serializers.Serializer):
    subject = serializers.CharField()
    body = serializers.CharField()
    body_format = serializers.ChoiceField(choices=BodyFormat.choices)
    body_html = serializers.CharField(allow_null=True)
    rendered_text = serializers.CharField(help_text='Exact text alternative that queue would freeze.')
    rendered_html = serializers.CharField(help_text='Escaped HTML (plain drafts: paragraphs/line breaks only).')
    audience_count = serializers.IntegerField(help_text='Current matching active students (advisory until queue).')


def preview_payload(broadcast, rendered, audience_count):
    return {
        'subject': rendered.subject, 'body': broadcast.body, 'body_format': broadcast.body_format,
        'body_html': broadcast.body_html, 'rendered_text': rendered.text, 'rendered_html': rendered.html,
        'audience_count': audience_count,
    }


class DeliverySerializer(serializers.ModelSerializer):
    """Admin delivery detail: snapshot recipient identity for operations."""
    student_id = serializers.IntegerField(read_only=True)
    state = serializers.ChoiceField(choices=DeliveryState.choices, read_only=True)
    provider_email_id = serializers.SerializerMethodField()
    error_code = serializers.SerializerMethodField()
    sent_at = UTCDateTimeField(read_only=True, allow_null=True)
    created_at = UTCDateTimeField(read_only=True)
    updated_at = UTCDateTimeField(read_only=True)
    eligible_actions = serializers.SerializerMethodField()

    class Meta:
        model = Delivery
        fields = ['id', 'student_id', 'snapshot_email', 'state', 'attempt_count', 'retry_generation',
                  'provider_email_id', 'error_code', 'sent_at', 'created_at', 'updated_at', 'eligible_actions']
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True, help_text='Resend email ID once accepted.'))
    def get_provider_email_id(self, obj):
        return obj.provider_email_id or None

    @extend_schema_field(serializers.CharField(allow_null=True, help_text='Safe error code; never raw errors.'))
    def get_error_code(self, obj):
        return obj.error_code or None

    @extend_schema_field({'type': 'array', 'items': {'type': 'string', 'enum': ['retry']}})
    def get_eligible_actions(self, obj):
        return ['retry'] if obj.is_retry_eligible else []
