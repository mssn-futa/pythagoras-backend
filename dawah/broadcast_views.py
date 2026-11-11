"""Broadcast API (D4 Backend Design routes, frozen COMM-03 contract).

Every route requires an active ``role=admin`` account; students never see
drafts, snapshots or recipient addresses. Queue and retry only persist outbox
work (202): nothing here sends mail.
"""
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import generics
from rest_framework.exceptions import ValidationError

from accounts.permissions import IsActiveAdmin
from pythagoras.utils import success_response

from . import broadcasts, examples
from .broadcast_serializers import (
    BroadcastSerializer, BroadcastWriteSerializer, DeliveryDetailSerializer, DeliverySerializer,
    PreviewSerializer, RetryRequestSerializer, VersionSerializer, parse_version, patch_version, preview_payload,
    sanitize_context,
)
from .models import Broadcast, BroadcastState, Delivery, DeliveryState
from .throttles import BroadcastSendRateThrottle

KIND_DRAFTS, KIND_HISTORY = 'drafts', 'history'
HISTORY_STATES = [state for state in BroadcastState.values if state != BroadcastState.DRAFT]
PAGE_PARAMS = [
    OpenApiParameter('page', int, description='1-based page number.'),
    OpenApiParameter('page_size', int, description='Default 20, maximum 100.'),
]
QUEUED_MESSAGE = 'Broadcast queued. Delivery is pending.'
SEND_LIMIT = '429 THROTTLED (60/hour per admin, shared by queue and retry).'


def _choice(params, name, choices, errors):
    value = params.get(name)
    if value is None or value == '':
        return None
    if value not in choices:
        errors[name] = [f'Select one of: {", ".join(choices)}.']
        return None
    return value


class BroadcastView(generics.GenericAPIView):
    permission_classes = [IsActiveAdmin]
    serializer_class = BroadcastSerializer

    def respond(self, broadcast_id, message, status=200):
        broadcast = get_object_or_404(broadcasts.with_counts(Broadcast.objects.all()), pk=broadcast_id)
        return success_response(BroadcastSerializer(broadcast).data, message, status=status)

    @staticmethod
    def require(broadcast_id):
        """404 before body validation (no lock; services re-read under lock)."""
        if not Broadcast.objects.filter(pk=broadcast_id).exists():
            raise Http404


class BroadcastListView(BroadcastView):
    @extend_schema(
        operation_id='dawah_broadcasts_list',
        parameters=[*PAGE_PARAMS,
                    OpenApiParameter('kind', str, enum=[KIND_DRAFTS, KIND_HISTORY],
                                     description='drafts: state=draft (newest edit first). history: every '
                                                 'non-draft state (newest queued first). Omitted: all.'),
                    OpenApiParameter('state', str, enum=BroadcastState.values,
                                     description='One broadcast state; narrows history (draft with kind=history, '
                                                 'or a non-draft state with kind=drafts, is a 400).')],
        responses={200: BroadcastSerializer(many=True)},
        description='Admin: paginated broadcasts with counts and eligible actions. Invalid kind/state → 400.',
        examples=[OpenApiExample('Drafts', response_only=True, value=examples.BROADCAST_DRAFT)],
    )
    def get(self, request):
        errors = {}
        kind = _choice(request.query_params, 'kind', [KIND_DRAFTS, KIND_HISTORY], errors)
        state = _choice(request.query_params, 'state', BroadcastState.values, errors)
        if state and kind == KIND_DRAFTS and state != BroadcastState.DRAFT:
            errors['state'] = ['Drafts only have state "draft".']
        if state and kind == KIND_HISTORY and state == BroadcastState.DRAFT:
            errors['state'] = [f'Select one of: {", ".join(HISTORY_STATES)}.']
        if errors:
            raise ValidationError(errors)
        queryset = Broadcast.objects.all()
        if kind == KIND_DRAFTS:
            queryset = queryset.filter(state=BroadcastState.DRAFT).order_by('-updated_at', '-id')
        elif kind == KIND_HISTORY:
            queryset = queryset.exclude(state=BroadcastState.DRAFT).order_by('-queued_at', '-id')
        else:
            queryset = queryset.order_by('-created_at', '-id')
        if state:
            queryset = queryset.filter(state=state)
        page = self.paginate_queryset(broadcasts.with_counts(queryset))
        response = self.get_paginated_response(BroadcastSerializer(page, many=True).data)
        response.message = 'Broadcasts retrieved.'
        return response

    @extend_schema(
        operation_id='dawah_broadcasts_create',
        request=BroadcastWriteSerializer,
        responses={201: BroadcastSerializer},
        description='Admin: save a draft (version 1). Subject: one line, ≤200 characters; audience '
                    'all/department/level. Plain: `{subject, body, audience}`. Rich: `{subject, body_format: '
                    '"rich", body_html, audience}` without `body`; `body_html` is sanitized to the D4 allowlist '
                    '(stored canonical form returned) and `body` is the generated text fallback. Content that is '
                    'empty after sanitization is a 400 under `fields.body_html`. Nothing is sent.',
        examples=[OpenApiExample('Create draft', request_only=True, value=examples.BROADCAST_CREATE),
                  OpenApiExample('Create rich draft', request_only=True, value=examples.BROADCAST_RICH_CREATE),
                  OpenApiExample('Created', response_only=True, status_codes=['201'],
                                 value={**examples.BROADCAST_DRAFT, 'version': 1}),
                  OpenApiExample('Created rich draft', response_only=True, status_codes=['201'],
                                 value=examples.BROADCAST_RICH_DRAFT)],
    )
    def post(self, request):
        serializer = BroadcastWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        broadcast = serializer.save(created_by=request.user)
        return self.respond(broadcast.pk, 'Draft saved.', status=201)


class BroadcastDetailView(BroadcastView):
    @extend_schema(operation_id='dawah_broadcasts_retrieve', responses={200: BroadcastSerializer},
                   description='Admin: one broadcast (draft or queued) with counts.',
                   examples=[OpenApiExample('Queued', response_only=True, value=examples.BROADCAST_QUEUED)])
    def get(self, request, broadcast_id):
        self.require(broadcast_id)
        return self.respond(broadcast_id, 'Broadcast retrieved.')

    @extend_schema(
        operation_id='dawah_broadcasts_partial_update',
        request=BroadcastWriteSerializer,
        responses={200: BroadcastSerializer},
        description='Admin: edit a draft. `version` is required and must equal the stored version '
                    '(409 BROADCAST_VERSION_CONFLICT otherwise); success increments it. Rich drafts take '
                    '`body_html` (never `body`); send `body_format` "plain" with `body` to convert to plain, '
                    'or "rich" with `body_html` to convert to rich. Queued broadcasts are immutable: 409 '
                    'BROADCAST_ALREADY_QUEUED.',
        examples=[OpenApiExample('Edit draft', request_only=True, value=examples.BROADCAST_PATCH),
                  OpenApiExample('Edit rich draft', request_only=True, value=examples.BROADCAST_RICH_PATCH)],
    )
    def patch(self, request, broadcast_id):
        self.require(broadcast_id)
        version = patch_version(request.data)
        context = sanitize_context(request.data)  # sanitize before the row lock
        broadcast = broadcasts.update_draft(
            broadcast_id, version,
            lambda locked: BroadcastWriteSerializer(locked, data=request.data, partial=True, context=context))
        return self.respond(broadcast.pk, 'Draft saved.')

    @extend_schema(operation_id='dawah_broadcasts_destroy', responses={204: None},
                   description='Admin: delete a draft. 409 BROADCAST_ALREADY_QUEUED once queued.')
    def delete(self, request, broadcast_id):
        broadcasts.delete_draft(broadcast_id)
        return success_response(None, 'Draft deleted.', status=204)


class BroadcastPreviewView(BroadcastView):
    serializer_class = PreviewSerializer

    @extend_schema(
        operation_id='dawah_broadcasts_preview',
        request=VersionSerializer,
        responses={200: PreviewSerializer},
        description='Admin: the exact email (subject, text part, HTML document) that queue would freeze and '
                    'the worker sends, from the stored draft re-sanitized now, plus the current audience '
                    'count. No side effects: no snapshot, no deliveries, no send. 409 '
                    'BROADCAST_VERSION_CONFLICT (stale version), BROADCAST_ALREADY_QUEUED.',
        examples=[OpenApiExample('Preview request', request_only=True, value=examples.VERSION_REQUEST),
                  OpenApiExample('Preview', response_only=True, value=examples.BROADCAST_PREVIEW),
                  OpenApiExample('Rich preview', response_only=True, value=examples.BROADCAST_RICH_PREVIEW)],
    )
    def post(self, request, broadcast_id):
        self.require(broadcast_id)
        version = parse_version(request.data)
        broadcast, rendered, count = broadcasts.preview(broadcast_id, version)
        return success_response(preview_payload(broadcast, rendered, count), 'Preview generated. Nothing was sent.')


class BroadcastQueueView(BroadcastView):
    throttle_classes = [BroadcastSendRateThrottle]

    @extend_schema(
        operation_id='dawah_broadcasts_queue',
        request=VersionSerializer,
        responses={202: BroadcastSerializer},
        description='Admin: freeze content and the active-student recipient snapshot, then return 202 once '
                    'persisted (not delivered). Replay with the queued version returns 202 with the same '
                    'broadcast/counts. 400 when no active student matches (draft unchanged); 409 '
                    'BROADCAST_VERSION_CONFLICT, BROADCAST_ALREADY_QUEUED (queued under another version); '
                    + SEND_LIMIT,
        examples=[OpenApiExample('Queue request', request_only=True, value=examples.VERSION_REQUEST),
                  OpenApiExample('Queued', response_only=True, status_codes=['202'],
                                 value=examples.BROADCAST_QUEUED)],
    )
    def post(self, request, broadcast_id):
        self.require(broadcast_id)
        version = parse_version(request.data)
        broadcast, replayed = broadcasts.queue(broadcast_id, version, request.user)
        message = 'Broadcast already queued. Delivery is pending.' if replayed else QUEUED_MESSAGE
        return self.respond(broadcast.pk, message, status=202)


class BroadcastDeliveriesView(BroadcastView):
    serializer_class = DeliverySerializer

    @extend_schema(
        operation_id='dawah_broadcasts_deliveries',
        parameters=[*PAGE_PARAMS, OpenApiParameter('state', str, enum=DeliveryState.values,
                                                   description='One delivery state.')],
        responses={200: DeliverySerializer(many=True)},
        description='Admin: per-recipient outcomes (ordered by id), including the snapshot address. '
                    '`sent` means provider accepted, not inbox delivery.',
        examples=[OpenApiExample('Deliveries', response_only=True, value=examples.DELIVERY_QUEUED)],
    )
    def get(self, request, broadcast_id):
        self.require(broadcast_id)
        errors = {}
        state = _choice(request.query_params, 'state', DeliveryState.values, errors)
        if errors:
            raise ValidationError(errors)
        queryset = Delivery.objects.filter(broadcast_id=broadcast_id).order_by('id')
        if state:
            queryset = queryset.filter(state=state)
        page = self.paginate_queryset(queryset)
        response = self.get_paginated_response(DeliverySerializer(page, many=True).data)
        response.message = 'Deliveries retrieved.'
        return response


class BroadcastDeliveryDetailView(BroadcastView):
    serializer_class = DeliveryDetailSerializer

    @extend_schema(
        operation_id='dawah_broadcasts_delivery_retrieve',
        responses={200: DeliveryDetailSerializer},
        description='Admin: one delivery of this broadcast with its current provider request identity '
                    '(idempotency key, request hash, first dispatch and safety deadline), closed retry '
                    'generations and every attempt (safe codes, timestamps, provider email IDs, late results '
                    'after a lost lease). Never message content. 404 if the delivery belongs to another broadcast.',
        examples=[OpenApiExample('Delivery detail', response_only=True, value=examples.DELIVERY_DETAIL)],
    )
    def get(self, request, broadcast_id, delivery_id):
        delivery = get_object_or_404(Delivery, pk=delivery_id, broadcast_id=broadcast_id)
        return success_response(DeliveryDetailSerializer(delivery).data, 'Delivery retrieved.')


class BroadcastRetryView(BroadcastView):
    throttle_classes = [BroadcastSendRateThrottle]

    @extend_schema(
        operation_id='dawah_broadcasts_retry',
        request=RetryRequestSerializer,
        responses={202: BroadcastSerializer},
        description='Admin: requeue explicitly selected known-failed (unaccepted) deliveries as a new retry '
                    'generation. All or nothing: IDs from another broadcast → 400; any sent/uncertain/skipped/'
                    'in-flight delivery → 409 DELIVERY_NOT_RETRYABLE; draft → 409 BROADCAST_NOT_QUEUED. '
                    + SEND_LIMIT,
        examples=[OpenApiExample('Retry request', request_only=True, value=examples.RETRY_REQUEST)],
    )
    def post(self, request, broadcast_id):
        self.require(broadcast_id)
        serializer = RetryRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        broadcast = broadcasts.retry(broadcast_id, serializer.validated_data['delivery_ids'], request.user)
        return self.respond(broadcast.pk, 'Retry queued. Delivery is pending.', status=202)
