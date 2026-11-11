"""Automation API (D4 route table, frozen COMM-07 contract). Admin only.

Nothing here sends mail: rules are evaluated by the outbox scheduler tick
(``dawah_outbox``), and only run history says what actually happened.
"""
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import generics

from accounts.permissions import IsActiveAdmin
from pythagoras.utils import success_response

from . import automations, examples
from .automation_serializers import (
    RulePreviewSerializer, RuleSerializer, RuleWriteSerializer, RunSerializer, preview_payload, run_payload,
)
from .broadcast_serializers import VersionSerializer, parse_version, patch_version
from .broadcast_views import PAGE_PARAMS
from .models import AutomationRule, AutomationRun

CONFLICTS = ('409 AUTOMATION_VERSION_CONFLICT (stale version, `fields.version` names the current one).')


class AutomationView(generics.GenericAPIView):
    permission_classes = [IsActiveAdmin]
    serializer_class = RuleSerializer
    queryset = AutomationRule.objects.none()

    @staticmethod
    def rule_response(rule_id, message, status=200):
        rule = get_object_or_404(AutomationRule.objects.select_related('owner'), pk=rule_id)
        return success_response(RuleSerializer(rule).data, message, status=status)

    @staticmethod
    def require(rule_id):
        if not AutomationRule.objects.filter(pk=rule_id).exists():
            raise Http404


class AutomationListView(AutomationView):
    @extend_schema(
        operation_id='dawah_automations_list', parameters=PAGE_PARAMS, responses={200: RuleSerializer(many=True)},
        description='Admin: automation rules, newest first. `enabled` is not proof that anything ran; see runs.',
        examples=[OpenApiExample('Rules', response_only=True, value=examples.AUTOMATION_WEEKLY)],
    )
    def get(self, request):
        queryset = AutomationRule.objects.select_related('owner').order_by('-created_at', '-id')
        page = self.paginate_queryset(queryset)
        response = self.get_paginated_response(RuleSerializer(page, many=True).data)
        response.message = 'Automation rules retrieved.'
        return response

    @extend_schema(
        operation_id='dawah_automations_create', request=RuleWriteSerializer, responses={201: RuleSerializer},
        description='Admin: save a disabled rule owned by the caller. Weekly: `{kind:"weekly_digest", audience, '
                    'timezone:"Africa/Lagos", weekday (0=Monday), local_time "HH:MM", template, enabled:false'
                    '[, categories]}` (defaults Monday 08:00 Africa/Lagos). Reminder: `{kind:"event_reminder", '
                    'audience, offset_minutes, categories (>=1), template, enabled:false}` (default offset 1440). '
                    'A missing template gets the documented default. Templates accept documented placeholders '
                    'only (weekly: week_start, week_end, summary_count, summaries; reminder: event_title, '
                    'event_start, event_location, event_summary, event_url). Field errors: 400 VALIDATION_ERROR.',
        examples=[OpenApiExample('Create weekly', request_only=True, value=examples.AUTOMATION_WEEKLY_CREATE),
                  OpenApiExample('Create reminder', request_only=True, value=examples.AUTOMATION_REMINDER_CREATE),
                  OpenApiExample('Created', response_only=True, status_codes=['201'],
                                 value=examples.AUTOMATION_REMINDER)],
    )
    def post(self, request):
        serializer = RuleWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = AutomationRule.objects.create(owner=request.user, enabled=False, **serializer.validated_data)
        return self.rule_response(rule.pk, 'Rule saved. It is disabled.', status=201)


class AutomationDetailView(AutomationView):
    @extend_schema(operation_id='dawah_automations_retrieve', responses={200: RuleSerializer},
                   description='Admin: one rule.',
                   examples=[OpenApiExample('Rule', response_only=True, value=examples.AUTOMATION_WEEKLY)])
    def get(self, request, rule_id):
        self.require(rule_id)
        return self.rule_response(rule_id, 'Automation rule retrieved.')

    @extend_schema(
        operation_id='dawah_automations_partial_update', request=RuleWriteSerializer, responses={200: RuleSerializer},
        description='Admin: versioned edit of the fields of the rule\'s kind (never `kind`/`enabled`), or '
                    '`{owner_id, version}` alone to reassign to an active admin. Saving never enables. A schedule '
                    'change of an enabled rule takes effect from now (no back-fill). ' + CONFLICTS,
        examples=[OpenApiExample('Edit', request_only=True, value=examples.AUTOMATION_PATCH),
                  OpenApiExample('Reassign', request_only=True, value={'owner_id': 2, 'version': 3})],
    )
    def patch(self, request, rule_id):
        self.require(rule_id)
        patch_version(request.data)
        rule = AutomationRule.objects.get(pk=rule_id)
        serializer = RuleWriteSerializer(data=request.data, rule=rule)
        serializer.is_valid(raise_exception=True)
        rule = automations.update_rule(rule_id, serializer.version, serializer.validated_data)
        message = 'Rule reassigned.' if 'owner' in serializer.validated_data else 'Rule saved.'
        return self.rule_response(rule.pk, message)


class AutomationEnableView(AutomationView):
    @extend_schema(
        operation_id='dawah_automations_enable', request=VersionSerializer, responses={200: RuleSerializer},
        description='Admin: enable the rule. Requires a valid stored schedule/audience/template (400 '
                    'VALIDATION_ERROR) and an active admin owner (409 AUTOMATION_OWNER_INACTIVE). Occurrences '
                    'scheduled before now are never sent. Enabling an enabled rule returns it unchanged. '
                    + CONFLICTS,
        examples=[OpenApiExample('Enable', request_only=True, value=examples.VERSION_REQUEST)],
    )
    def post(self, request, rule_id):
        self.require(rule_id)
        rule = automations.enable(rule_id, parse_version(request.data))
        return self.rule_response(rule.pk, 'Rule enabled. Check run history for what actually runs.')


class AutomationDisableView(AutomationView):
    @extend_schema(
        operation_id='dawah_automations_disable', request=VersionSerializer, responses={200: RuleSerializer},
        description='Admin: disable the rule. No new occurrences; broadcasts already queued follow the normal '
                    'delivery policy. ' + CONFLICTS,
        examples=[OpenApiExample('Disable', request_only=True, value=examples.VERSION_REQUEST)],
    )
    def post(self, request, rule_id):
        self.require(rule_id)
        rule = automations.disable(rule_id, parse_version(request.data))
        return self.rule_response(rule.pk, 'Rule disabled.')


class AutomationPreviewView(AutomationView):
    serializer_class = RulePreviewSerializer

    @extend_schema(
        operation_id='dawah_automations_preview', request=VersionSerializer, responses={200: RulePreviewSerializer},
        description='Admin: non-sending preview of the next occurrence with the currently known source (weekly: '
                    'the window ending at the next scheduled time; reminder: the next matching published event, '
                    'or sample values). Same renderer as queued mail. No run, snapshot or delivery is written. '
                    + CONFLICTS,
        examples=[OpenApiExample('Preview request', request_only=True, value=examples.VERSION_REQUEST),
                  OpenApiExample('Preview', response_only=True, value=examples.AUTOMATION_PREVIEW)],
    )
    def post(self, request, rule_id):
        self.require(rule_id)
        rule, rendered, count, skip, source = automations.preview(rule_id, parse_version(request.data))
        return success_response(preview_payload(rule, rendered, count, skip, source),
                                'Preview generated. Nothing was sent.')


class AutomationRunsView(AutomationView):
    serializer_class = RunSerializer

    @extend_schema(
        operation_id='dawah_automations_runs', parameters=PAGE_PARAMS, responses={200: RunSerializer(many=True)},
        description='Admin: occurrence history, newest scheduled first, including skipped, missed, blocked, '
                    'cancelled and failed runs with reason codes, and the broadcast/counts once materialized.',
        examples=[OpenApiExample('Runs', response_only=True, value=examples.AUTOMATION_RUN)],
    )
    def get(self, request, rule_id):
        self.require(rule_id)
        queryset = AutomationRun.objects.filter(rule_id=rule_id).order_by('-scheduled_at', '-id')
        page = self.paginate_queryset(queryset)
        materialized = automations.broadcast_counts([run.broadcast_id for run in page if run.broadcast_id])
        response = self.get_paginated_response(RunSerializer([run_payload(r, materialized) for r in page],
                                                             many=True).data)
        response.message = 'Automation runs retrieved.'
        return response
