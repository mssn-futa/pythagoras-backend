"""Broadcast request/response shapes (D4 API contract, frozen for COMM-03).

Field errors are always lists of strings keyed by a top-level field name
(audience problems are reported under ``audience``), so the shared client can
show them next to the form controls.
"""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail

from pythagoras.serializers import UTCDateTimeField

from . import broadcast_content, broadcasts
from .models import (
    AudienceType, BodyFormat, Broadcast, BroadcastState, Delivery, DeliveryAttempt, DeliveryGeneration,
    DeliveryState,
)
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
            if department is None:
                raise serializers.ValidationError('Enter a department.')
            # Shared CharField rules: string only, trimmed, length, no NUL/surrogates.
            field = StrictCharField(max_length=DEPARTMENT_MAX_LENGTH, error_messages={
                'blank': 'Enter a department.', 'invalid': 'Enter the department as text.',
                'max_length': f'Department must be {DEPARTMENT_MAX_LENGTH} characters or fewer.'})
            try:
                department = field.run_validation(department)
            except serializers.ValidationError as error:
                raise serializers.ValidationError(list(error.detail)) from None
            if broadcast_content.subject_error(department):
                raise serializers.ValidationError('Department must be plain single-line text.')
            values['audience_department'] = department
        elif kind == AudienceType.LEVEL:
            if type(level) is not str or level not in LEVELS:
                raise serializers.ValidationError(f'Choose a level: {", ".join(LEVELS)}.')
            values['audience_level'] = level
        return values


# ---------------------------------------------------------------- requests

def presanitize(raw):
    """Author ``body_html`` → ``SanitizedRich``; raises ``ContentError``."""
    broadcast_content.check_rich_input(raw)
    return broadcast_content.sanitize_rich_html(raw)


def sanitize_context(data):
    """Serializer context with ``body_html`` already sanitized, so the
    (CPU-bound) sanitizer runs before ``update_draft`` takes the row lock.
    Invalid input is left for the serializer to report."""
    raw = data.get('body_html') if hasattr(data, 'get') else None
    if type(raw) is not str:
        return {}
    try:
        return {'sanitized_body_html': (raw, presanitize(raw))}
    except broadcast_content.ContentError:
        return {}


@extend_schema_field({'type': 'string', 'pattern': '^(plain|rich)$'})  # pattern: avoids an enum-name collision
class BodyFormatField(StrictCharField):
    pass


RICH_BODY_SENT = 'Rich drafts do not accept body; the server generates the text fallback from body_html.'
HTML_FOR_PLAIN = 'body_html applies only to rich drafts; send body_format "rich" with it.'


class BroadcastWriteSerializer(StrictWriteMixin, serializers.ModelSerializer):
    """Create or PATCH (any subset plus ``version``) a draft (D4, frozen for COMM-03/COMM-05).

    * Plain: ``{subject, body, audience}``; ``body_format: "plain"`` optional
      (sent when converting a rich draft back to plain; ``body`` may then be
      omitted to keep the current text fallback). ``body_html`` becomes null.
    * Rich: ``{subject, body_format: "rich", body_html, audience}`` and never
      ``body``. ``body_html`` is sanitized to the D4 allowlist and stored in
      canonical form; ``body`` is the generated text fallback. A PATCH of a
      rich draft may omit ``body_format``.
    """
    subject = StrictCharField(max_length=broadcast_content.SUBJECT_MAX_LENGTH,
                              help_text='Single line, at most 200 characters.')
    body = StrictCharField(required=False, trim_whitespace=False, max_length=broadcast_content.BODY_MAX_LENGTH,
                           help_text='Plain drafts only: message text (line breaks kept). Required to create a '
                                     'plain draft; never sent for rich drafts (server-generated fallback).')
    body_format = BodyFormatField(required=False, help_text='"plain" (default) or "rich".')
    body_html = StrictCharField(
        required=False, trim_whitespace=False, max_length=broadcast_content.RICH_HTML_MAX_LENGTH,
        error_messages={'max_length': broadcast_content.TOO_LONG_MESSAGE, 'blank': 'Enter the message.'},
        help_text='Rich drafts only: HTML using p, br, strong, em, h2, h3, ul, ol, li, blockquote and '
                  'a[href=http(s)://...]. Anything else is removed; empty results are rejected. At most '
                  f'{broadcast_content.RICH_HTML_MAX_LENGTH:,} characters.')
    audience = AudienceField()
    version = StrictIntegerField(required=False, write_only=True, min_value=1,
                                 help_text='PATCH only: the version you loaded (optimistic concurrency).')

    class Meta:
        model = Broadcast
        fields = ['subject', 'body', 'body_format', 'body_html', 'audience', 'version']

    def to_internal_value(self, data):
        try:
            return super().to_internal_value(data)
        except serializers.ValidationError as error:
            # Report the missing message field with the other field errors on create
            # (cross-field checks in validate() only run once fields are valid).
            if self.instance is None and isinstance(error.detail, dict) and hasattr(data, 'keys'):
                rich = data.get('body_format') == BodyFormat.RICH
                name = 'body_html' if rich else 'body'
                if name not in data:
                    error.detail.setdefault(name, [ErrorDetail(
                        'Enter the message.' if rich else 'This field is required.', code='required')])
            raise

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
        if value not in BodyFormat.values:
            raise serializers.ValidationError('Choose "plain" or "rich".')
        return value

    def validate_version(self, value):
        if self.instance is None:
            raise serializers.ValidationError('A new draft has no version yet.')
        return value

    def validate(self, attrs):
        attrs.pop('version', None)  # checked under the row lock by broadcasts.update_draft
        current = self.instance.body_format if self.instance is not None else None
        body_format = attrs.get('body_format') or current or BodyFormat.PLAIN
        if body_format == BodyFormat.RICH:
            self._validate_rich(attrs, current)
        else:
            self._validate_plain(attrs, current)
        return attrs

    def _validate_rich(self, attrs, current):
        if 'body' in self.initial_data:
            raise serializers.ValidationError({'body': [RICH_BODY_SENT]})
        if 'body_html' not in attrs:
            if current != BodyFormat.RICH:
                raise serializers.ValidationError({'body_html': ['Enter the message.']})
            return  # subject/audience edit of a rich draft
        sanitized = self.context.get('sanitized_body_html')  # pre-sanitized outside the row lock (PATCH)
        if sanitized is None or sanitized[0] != attrs['body_html']:
            try:
                sanitized = (attrs['body_html'], presanitize(attrs['body_html']))
            except broadcast_content.ContentError as error:
                raise serializers.ValidationError({'body_html': [str(error)]}) from None
        sanitized = sanitized[1]
        if len(sanitized.text) > broadcast_content.BODY_MAX_LENGTH:
            raise serializers.ValidationError({'body_html': [
                f'The message text must be {broadcast_content.BODY_MAX_LENGTH:,} characters or fewer.']})
        attrs.update(body_format=BodyFormat.RICH, body_html=sanitized.html, body=sanitized.text)

    def _validate_plain(self, attrs, current):
        if 'body_html' in self.initial_data:
            raise serializers.ValidationError({'body_html': [HTML_FOR_PLAIN]})
        if 'body' not in attrs and current is None:
            raise serializers.ValidationError({'body': ['This field is required.']})
        attrs.update(body_format=BodyFormat.PLAIN, body_html=None)  # rich -> plain keeps the fallback as body


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
    body = serializers.CharField(read_only=True, help_text='Plain text; for rich drafts the server-generated '
                                                           'text fallback (what the text part of the email shows).')
    body_html = serializers.CharField(read_only=True, allow_null=True,
                                      help_text='Canonical sanitized HTML of rich drafts; null for plain.')
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
    rendered_html = serializers.CharField(
        help_text='The complete HTML email document that queue would freeze and the worker sends: the shared '
                  'template around sanitized rich content or escaped plain paragraphs. Render it only in a '
                  'sandboxed frame.')
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


class DeliveryAttemptSerializer(serializers.ModelSerializer):
    """One provider handoff (or pre-handoff decision). Safe codes only."""
    outcome = serializers.ChoiceField(choices=DeliveryAttempt.Outcome.choices, read_only=True, allow_blank=True,
                                      help_text='Blank while the attempt is in flight.')
    started_at = UTCDateTimeField(read_only=True)
    finished_at = UTCDateTimeField(read_only=True, allow_null=True)
    late_outcome = serializers.ChoiceField(choices=DeliveryAttempt.Outcome.choices, read_only=True,
                                           allow_blank=True,
                                           help_text='Result reported after this attempt lost its lease.')
    late_recorded_at = UTCDateTimeField(read_only=True, allow_null=True)

    class Meta:
        model = DeliveryAttempt
        fields = ['generation', 'attempt', 'started_at', 'finished_at', 'outcome', 'error_code',
                  'provider_email_id', 'late_outcome', 'late_error_code', 'late_recorded_at']
        read_only_fields = fields


class DeliveryGenerationSerializer(serializers.ModelSerializer):
    """A closed retry generation's provider request identity and outcome."""
    state = serializers.ChoiceField(choices=DeliveryState.choices, read_only=True)
    first_dispatch_at = UTCDateTimeField(read_only=True, allow_null=True)
    idempotency_deadline_at = UTCDateTimeField(read_only=True, allow_null=True)
    requested_at = UTCDateTimeField(read_only=True, allow_null=True)
    requested_by_id = serializers.IntegerField(read_only=True, allow_null=True)
    closed_at = UTCDateTimeField(read_only=True)
    closed_by_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = DeliveryGeneration
        fields = ['generation', 'state', 'attempt_count', 'error_code', 'nonacceptance_known', 'provider_email_id',
                  'idempotency_key', 'request_hash', 'first_dispatch_at', 'idempotency_deadline_at',
                  'requested_at', 'requested_by_id', 'closed_at', 'closed_by_id']
        read_only_fields = fields


class DeliveryDetailSerializer(DeliverySerializer):
    """Admin delivery detail: current request identity plus closed generations
    and every attempt. Never the request snapshot or message content."""
    next_attempt_at = UTCDateTimeField(read_only=True, allow_null=True)
    first_dispatch_at = UTCDateTimeField(read_only=True, allow_null=True)
    idempotency_deadline_at = UTCDateTimeField(read_only=True, allow_null=True)
    retry_requested_at = UTCDateTimeField(read_only=True, allow_null=True)
    retry_requested_by_id = serializers.IntegerField(read_only=True, allow_null=True)
    generations = serializers.SerializerMethodField()
    attempts = serializers.SerializerMethodField()

    class Meta(DeliverySerializer.Meta):
        fields = DeliverySerializer.Meta.fields + [
            'broadcast_id', 'nonacceptance_known', 'next_attempt_at', 'idempotency_key', 'request_hash',
            'first_dispatch_at', 'idempotency_deadline_at', 'retry_requested_at', 'retry_requested_by_id',
            'generations', 'attempts']
        read_only_fields = fields

    @extend_schema_field(DeliveryGenerationSerializer(many=True))
    def get_generations(self, obj):
        rows = DeliveryGeneration.objects.filter(delivery=obj).order_by('generation')
        return DeliveryGenerationSerializer(rows, many=True).data

    @extend_schema_field(DeliveryAttemptSerializer(many=True))
    def get_attempts(self, obj):
        rows = DeliveryAttempt.objects.filter(delivery=obj).order_by('generation', 'attempt')
        return DeliveryAttemptSerializer(rows, many=True).data
