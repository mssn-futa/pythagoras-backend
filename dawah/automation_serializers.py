"""Automation rule/run request and response shapes (D4, frozen COMM-07 contract).

Field errors are flat lists of strings keyed by field name; template problems
use ``template`` (placeholders/code) or ``template.<field>`` (COMM-07 strips the
prefix and shows them next to the template controls).
"""
import re
from datetime import time

from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from pythagoras.serializers import UTCDateTimeField

from . import automations, broadcast_content
from .broadcast_serializers import (
    AUDIENCE_SCHEMA, COUNTS_SCHEMA, AudienceField, StrictCharField, StrictIntegerField, audience_payload, presanitize,
)
from .models import AUTOMATION_CATEGORIES, AutomationKind, AutomationRule, BodyFormat

LOCAL_TIME_PATTERN = r'^([01]\d|2[0-3]):[0-5]\d$'
MAX_OFFSET_MINUTES = 60 * 24 * 30  # 30 days
COMMON_FIELDS = ('audience', 'template', 'categories')
KIND_FIELDS = {
    AutomationKind.WEEKLY_DIGEST: (*COMMON_FIELDS, 'timezone', 'weekday', 'local_time'),
    AutomationKind.EVENT_REMINDER: (*COMMON_FIELDS, 'offset_minutes'),
}
NOT_FOR_KIND = 'Not allowed for this rule type.'


def _flatten(detail, prefix=''):
    """DRF error detail → ``{"field" | "template.field": [str, ...]}``."""
    if isinstance(detail, dict):
        flat = {}
        for key, value in detail.items():
            name = f'{prefix}{key}' if key != 'non_field_errors' or not prefix else prefix.rstrip('.')
            for sub, messages in _flatten(value, f'{name}.').items():
                flat.setdefault(sub, []).extend(messages)
        return flat
    if isinstance(detail, list) and detail and all(isinstance(item, dict) for item in detail):
        return _flatten(detail[0], prefix)
    messages = detail if isinstance(detail, list) else [detail]
    return {prefix.rstrip('.'): [str(message) for message in messages]}


TEMPLATE_SCHEMA = {
    'type': 'object',
    'required': ['subject', 'body_format'],
    'additionalProperties': False,
    'properties': {
        'subject': {'type': 'string', 'maxLength': 200,
                    'description': 'One line; documented placeholders such as {{event_title}} allowed.'},
        'body_format': {'type': 'string', 'enum': BodyFormat.values},
        'body': {'type': 'string', 'description': 'Plain templates only (rich: never sent).'},
        'body_html': {'type': 'string', 'description': 'Rich templates only; sanitized to the broadcast allowlist.'},
    },
    'description': 'Plain: {subject, body_format:"plain", body}. Rich: {subject, body_format:"rich", body_html}.',
}

TEMPLATE_OUT_SCHEMA = {
    'type': 'object',
    'required': ['subject', 'body_format', 'body', 'body_html'],
    'properties': {
        'subject': {'type': 'string'},
        'body_format': {'type': 'string', 'enum': BodyFormat.values},
        'body': {'type': 'string', 'description': 'Plain template, or the generated text fallback of a rich one.'},
        'body_html': {'type': 'string', 'nullable': True, 'description': 'Canonical sanitized HTML; null for plain.'},
    },
}


@extend_schema_field(TEMPLATE_SCHEMA)
class TemplateField(serializers.Field):
    """``template`` → ``subject/body_format/body/body_html`` model columns."""

    def __init__(self, **kwargs):
        kwargs['source'] = '*'
        super().__init__(**kwargs)

    def to_representation(self, rule):
        return {'subject': rule.subject, 'body_format': rule.body_format, 'body': rule.body,
                'body_html': rule.body_html}

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError('Enter a template object.')
        errors = {}
        unknown = sorted(set(data) - {'subject', 'body_format', 'body', 'body_html'})
        if unknown:
            errors['non_field_errors'] = [f'Unexpected template keys: {", ".join(unknown)}.']
        body_format = data.get('body_format', BodyFormat.PLAIN)
        if body_format not in BodyFormat.values:
            errors['body_format'] = ['Choose "plain" or "rich".']
        subject = data.get('subject')
        if type(subject) is not str or not subject.strip():
            errors['subject'] = ['Enter a subject.']
        else:
            subject = subject.strip()
            if len(subject) > broadcast_content.SUBJECT_MAX_LENGTH:
                errors['subject'] = ['Subject must be 200 characters or fewer.']
            elif broadcast_content.subject_error(subject):
                errors['subject'] = [broadcast_content.subject_error(subject)]
        values = {'subject': subject}
        if body_format == BodyFormat.RICH:
            if 'body' in data:
                errors['body'] = ['Rich templates do not accept body; the server generates the text.']
            raw = data.get('body_html')
            try:
                sanitized = presanitize(raw)
            except broadcast_content.ContentError as error:
                errors['body_html'] = [str(error)]
            else:
                values.update(body_format=BodyFormat.RICH, body_html=sanitized.html, body=sanitized.text)
        elif body_format == BodyFormat.PLAIN:
            if 'body_html' in data:
                errors['body_html'] = ['body_html applies only to rich templates.']
            body = data.get('body')
            if type(body) is not str or not broadcast_content.normalize_body(body).strip():
                errors['body'] = ['Enter the message template.']
            else:
                body = broadcast_content.normalize_body(body)
                problem = broadcast_content.body_error(body)
                if problem:
                    errors['body'] = [problem]
                values.update(body_format=BodyFormat.PLAIN, body_html=None, body=body)
        if errors:
            raise serializers.ValidationError(errors)
        return values


class RuleWriteSerializer(serializers.Serializer):
    """Create (``kind`` + kind fields, ``enabled`` false or omitted) or PATCH
    (kind fields and/or ``owner_id``, plus ``version``)."""
    kind = serializers.ChoiceField(choices=AutomationKind.choices, required=False)
    enabled = serializers.BooleanField(required=False, help_text='Create only; must be false (rules start disabled).')
    audience = AudienceField(required=False)
    categories = serializers.ListField(child=StrictCharField(), required=False, max_length=len(AUTOMATION_CATEGORIES),
                                       help_text=f'Tokens: {", ".join(AUTOMATION_CATEGORIES)}. Reminders need at '
                                                 'least one; weekly may be empty (all four).')
    timezone = StrictCharField(required=False, help_text='Weekly only. Supported: Africa/Lagos.')
    weekday = StrictIntegerField(required=False, min_value=0, max_value=6, help_text='Weekly only; 0 = Monday.')
    local_time = StrictCharField(required=False, help_text='Weekly only; "HH:MM" 24-hour local time.')
    offset_minutes = StrictIntegerField(required=False, min_value=1, max_value=MAX_OFFSET_MINUTES,
                                        help_text='Reminder only; minutes before the event start.')
    template = TemplateField(required=False)
    owner_id = StrictIntegerField(required=False, min_value=1,
                                  help_text='PATCH only, sent alone with version: reassign to an active admin.')
    version = StrictIntegerField(required=False, min_value=1, help_text='PATCH only: the version you loaded.')

    def __init__(self, *args, rule=None, **kwargs):
        self.rule = rule
        super().__init__(*args, **kwargs)

    def run_validation(self, data=serializers.empty):
        try:
            return super().run_validation(data)
        except serializers.ValidationError as error:
            raise serializers.ValidationError(_flatten(error.detail)) from None

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError({'non_field_errors': ['Send a JSON object.']})
        kind = self.rule.kind if self.rule else data.get('kind')
        errors = {}
        allowed = {'version', 'owner_id'} if self.rule else {'kind', 'enabled'}
        allowed |= set(KIND_FIELDS[kind] if kind in KIND_FIELDS else self.fields)
        for name in sorted(set(data) - allowed):
            errors[name] = [NOT_FOR_KIND if name in self.fields else 'Unknown field.']
            if self.rule and name in ('kind', 'enabled'):
                errors[name] = ['Use the enable/disable actions; kind cannot change.' if name == 'enabled'
                                else 'The rule type cannot change.']
        if self.rule and 'owner_id' in data and set(data) - {'owner_id', 'version'}:
            errors['owner_id'] = ['Send owner_id on its own (with version).']
        if errors:
            raise serializers.ValidationError(errors)
        return super().to_internal_value(data)

    def validate_enabled(self, value):
        if value is not False:
            raise serializers.ValidationError('Rules start disabled: send enabled false and use the enable action.')
        return value

    def validate_categories(self, value):
        value = [item.strip().lower() for item in value]
        unknown = sorted(set(value) - set(AUTOMATION_CATEGORIES))
        if unknown:
            raise serializers.ValidationError(f'Unknown category: {", ".join(unknown)}.')
        if len(set(value)) != len(value):
            raise serializers.ValidationError('Each category may be listed once.')
        return value

    def validate_timezone(self, value):
        if value not in automations.SUPPORTED_TIMEZONES:
            raise serializers.ValidationError('Only Africa/Lagos is supported.')
        return value

    def validate_local_time(self, value):
        if not re.fullmatch(LOCAL_TIME_PATTERN, value):
            raise serializers.ValidationError('Time must be HH:MM (24-hour), for example 08:00.')
        hour, minute = map(int, value.split(':'))
        return time(hour, minute)

    def validate_owner_id(self, value):
        owner = get_user_model().objects.filter(pk=value).first()
        if not automations.is_owner_candidate(owner):
            raise serializers.ValidationError('Choose an active admin.')
        return owner

    def validate(self, attrs):
        version = attrs.pop('version', None)
        if self.rule is not None and version is None:
            raise serializers.ValidationError({'version': ['This field is required.']})
        self.version = version
        if 'owner_id' in attrs:
            attrs['owner'] = attrs.pop('owner_id')
        if self.rule is None:
            attrs.pop('enabled', None)
            kind = attrs.get('kind')
            if kind is None:
                raise serializers.ValidationError({'kind': ['This field is required.']})
            attrs.setdefault('audience_type', 'all')
            if 'template' not in self.initial_data:
                default = automations.DEFAULT_TEMPLATES[kind]
                attrs.update(subject=default['subject'], body=default['body'], body_format=BodyFormat.PLAIN,
                             body_html=None)
            if kind == AutomationKind.WEEKLY_DIGEST:
                attrs.setdefault('timezone', automations.DEFAULT_TIMEZONE)
                attrs.setdefault('weekday', 0)
                attrs.setdefault('local_time', time(8, 0))
            else:
                attrs.setdefault('offset_minutes', 24 * 60)
                attrs.setdefault('timezone', automations.DEFAULT_TIMEZONE)
        kind = self.rule.kind if self.rule else attrs['kind']
        errors = {}
        if kind == AutomationKind.EVENT_REMINDER and 'categories' in attrs and not attrs['categories']:
            errors['categories'] = ['Choose at least one event category.']
        if kind == AutomationKind.EVENT_REMINDER and self.rule is None and 'categories' not in attrs:
            errors['categories'] = ['Choose at least one event category.']
        if 'subject' in attrs:
            problems = automations.template_errors(kind, attrs['subject'], attrs.get('body'), attrs.get('body_html'))
            if problems:
                errors['template'] = problems
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class RuleSerializer(serializers.ModelSerializer):
    """Admin rule payload (COMM-07)."""
    kind = serializers.ChoiceField(choices=AutomationKind.choices)
    audience = serializers.SerializerMethodField()
    categories = serializers.ListField(child=serializers.CharField())
    weekday = serializers.IntegerField(allow_null=True, help_text='Weekly: 0 = Monday; null for reminders.')
    local_time = serializers.SerializerMethodField()
    offset_minutes = serializers.IntegerField(allow_null=True, help_text='Reminder; null for weekly.')
    template = serializers.SerializerMethodField()
    owner_id = serializers.IntegerField(allow_null=True)
    owner_active = serializers.SerializerMethodField()
    blocked_reason = serializers.SerializerMethodField()
    created_at = UTCDateTimeField()
    updated_at = UTCDateTimeField()

    class Meta:
        model = AutomationRule
        fields = ['id', 'kind', 'enabled', 'audience', 'categories', 'timezone', 'weekday', 'local_time',
                  'offset_minutes', 'template', 'owner_id', 'owner_active', 'blocked_reason', 'version',
                  'created_at', 'updated_at']
        read_only_fields = fields

    @extend_schema_field(AUDIENCE_SCHEMA)
    def get_audience(self, obj):
        return audience_payload(obj)

    @extend_schema_field(serializers.CharField(allow_null=True, help_text='Weekly: "HH:MM"; null for reminders.'))
    def get_local_time(self, obj):
        return obj.local_time.strftime('%H:%M') if obj.local_time else None

    @extend_schema_field(TEMPLATE_OUT_SCHEMA)
    def get_template(self, obj):
        return {'subject': obj.subject, 'body_format': obj.body_format, 'body': obj.body, 'body_html': obj.body_html}

    @extend_schema_field(serializers.BooleanField())
    def get_owner_active(self, obj):
        return automations.owner_blocked_reason(obj) is None

    @extend_schema_field(serializers.CharField(allow_null=True, help_text='"owner_inactive" or null.'))
    def get_blocked_reason(self, obj):
        return automations.owner_blocked_reason(obj)


EVENT_SUMMARY_SCHEMA = {
    'type': 'object',
    'properties': {'id': {'type': 'integer'}, 'title': {'type': 'string'}, 'category': {'type': 'string'},
                   'start_time': {'type': 'string', 'format': 'date-time'}},
}
SOURCE_SCHEMA = {
    'type': 'object',
    'nullable': True,
    'properties': {
        'window_start': {'type': 'string', 'format': 'date-time', 'nullable': True},
        'window_end': {'type': 'string', 'format': 'date-time', 'nullable': True},
        'events': {'type': 'array', 'items': EVENT_SUMMARY_SCHEMA},
        'event': {**EVENT_SUMMARY_SCHEMA, 'nullable': True},
        'event_revision': {'type': 'integer', 'nullable': True},
        'scheduled_at': {'type': 'string', 'format': 'date-time'},
    },
    'description': 'Provenance: weekly window and collated events; reminder event and schedule revision.',
}


@extend_schema_field({**COUNTS_SCHEMA, 'nullable': True})
class CountsField(serializers.JSONField):
    pass


@extend_schema_field(SOURCE_SCHEMA)
class SourceField(serializers.JSONField):
    pass


class RunSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    occurrence_key = serializers.CharField()
    scheduled_at = UTCDateTimeField()
    state = serializers.ChoiceField(choices=automations.RUN_STATES)
    reason_code = serializers.CharField(allow_null=True, help_text='Safe reason; unknown codes may be added.')
    broadcast_id = serializers.IntegerField(allow_null=True)
    recipient_count = serializers.IntegerField(allow_null=True)
    counts = CountsField(allow_null=True, help_text='Delivery counts once materialized, else null.')
    source = SourceField()
    created_at = UTCDateTimeField()
    updated_at = UTCDateTimeField()


def run_payload(run, materialized=None):
    """``materialized``: {broadcast_id: (broadcast, counts)} (``automations.broadcast_counts``)."""
    broadcast = counts = None
    if run.broadcast_id:
        broadcast, counts = (materialized or automations.broadcast_counts([run.broadcast_id]))[run.broadcast_id]
    state, reason = automations.run_state(run, broadcast, counts)
    source = dict(run.source or {})
    source.setdefault('window_start', run.window_start and run.window_start.strftime('%Y-%m-%dT%H:%M:%SZ'))
    source.setdefault('window_end', run.window_end and run.window_end.strftime('%Y-%m-%dT%H:%M:%SZ'))
    source.setdefault('events', [])
    if run.event_id is not None:
        source.setdefault('event', {'id': run.event_id})
        source['event_revision'] = run.event_revision
    return {
        'id': run.pk, 'occurrence_key': run.occurrence_key, 'scheduled_at': run.scheduled_at,
        'state': state, 'reason_code': reason, 'broadcast_id': run.broadcast_id,
        'recipient_count': broadcast.recipient_count if broadcast else None, 'counts': counts,
        'source': source, 'created_at': run.created_at, 'updated_at': run.updated_at,
    }


class RulePreviewSerializer(serializers.Serializer):
    subject = serializers.CharField(help_text='Rendered subject.')
    body = serializers.CharField(help_text='Stored template body (or rich text fallback).')
    body_format = serializers.ChoiceField(choices=BodyFormat.choices)
    body_html = serializers.CharField(allow_null=True)
    rendered_text = serializers.CharField()
    rendered_html = serializers.CharField(help_text='Complete email document; show only in a sandboxed frame.')
    audience_count = serializers.IntegerField()
    skip_reason_code = serializers.CharField(
        allow_null=True, help_text='Set when this occurrence would be recorded as skipped now '
                                   '(no_eligible_summaries, no_recipients) or no event matches (no_upcoming_event; '
                                   'the preview then uses sample event values).')
    source = SourceField()


def preview_payload(rule, rendered, count, skip, source):
    return {'subject': rendered.subject, 'body': rule.body, 'body_format': rule.body_format,
            'body_html': rule.body_html, 'rendered_text': rendered.text, 'rendered_html': rendered.html,
            'audience_count': count, 'skip_reason_code': skip, 'source': source}

