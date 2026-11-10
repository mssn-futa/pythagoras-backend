"""Da'wah event/resource API (D2 routes, D0 envelope and pagination).

Reads are public for published content (A03); drafts/unpublished records and
every mutation require an active ``role=admin`` account. Hidden content is 404.
"""
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import generics
from rest_framework.permissions import SAFE_METHODS, AllowAny

from accounts.permissions import IsActiveAdmin
from pythagoras.utils import success_response

from . import examples, filters, services
from .models import Event, EventResource, PublicationStatus
from .serializers import (
    EventDetailSerializer, EventResourceSerializer, EventResourceWriteSerializer, EventSerializer,
    EventWriteSerializer,
)


def reader_is_admin(request):
    return IsActiveAdmin().has_permission(request, None)


class DawahView(generics.GenericAPIView):
    """Public reads, admin-only writes (checked before any object access)."""

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [AllowAny()]
        return [IsActiveAdmin()]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['is_admin'] = reader_is_admin(self.request)
        return context

    def paginated(self, queryset, serializer_class, message):
        page = self.paginate_queryset(queryset)
        data = serializer_class(page, many=True, context=self.get_serializer_context()).data
        response = self.get_paginated_response(data)
        response.message = message
        return response

    def readable_event(self, event_id):
        events = Event.objects.all()
        if not reader_is_admin(self.request):
            events = events.visible()
        return get_object_or_404(events, pk=event_id)

    def respond(self, instance, serializer_class, message, status=200):
        data = serializer_class(instance, context=self.get_serializer_context()).data
        return success_response(data, message, status=status)


# ---------------------------------------------------------------- parameters

PAGE_PARAMS = [
    OpenApiParameter('page', int, description='1-based page number.'),
    OpenApiParameter('page_size', int, description='Default 20, maximum 100.'),
]
CONTENT_PARAMS = [
    OpenApiParameter('search', str, description='Case-insensitive match on event title, summary or tags '
                                                '(archive: also resource title).'),
    OpenApiParameter('speaker', str, description='Case-insensitive substring of the event speakers.'),
    OpenApiParameter('category', str, description='Case-insensitive exact event category.'),
    OpenApiParameter('date_from', str, description='YYYY-MM-DD; event start on/after this Africa/Lagos day.'),
    OpenApiParameter('date_to', str, description='YYYY-MM-DD; event start on/before this Africa/Lagos day.'),
]
STATUS_PARAM = OpenApiParameter(
    'status', str, enum=filters.STATUS_VALUES,
    description='Admin only: publication status filter (`all` includes every state). Ignored for other readers, '
                'who only ever see published content.')
TYPE_PARAM = OpenApiParameter('type', str, enum=EventResource.Type.values, description='Resource type.')


def status_filter(request, errors, default):
    """Admins may widen visibility with ``status``; others never can."""
    value = filters.parse_status(request.query_params, errors)
    if not reader_is_admin(request):
        return PublicationStatus.PUBLISHED
    return value or default


# ---------------------------------------------------------------- events

class EventListView(DawahView):
    serializer_class = EventSerializer

    @extend_schema(
        operation_id='dawah_events_list',
        parameters=[*PAGE_PARAMS, STATUS_PARAM,
                    OpenApiParameter('upcoming', bool, description='true: events whose end_time is after now, '
                                                                   'ordered by start_time then id.'),
                    *CONTENT_PARAMS],
        responses={200: EventSerializer(many=True)},
        description='Published events (newest start first, id descending). Admins may pass `status`.',
        examples=[OpenApiExample('Events', response_only=True, value=examples.EVENT_PUBLISHED)],
    )
    def get(self, request):
        params, errors = request.query_params, {}
        status = status_filter(request, errors, default=PublicationStatus.PUBLISHED)
        upcoming = filters.parse_upcoming(params, errors)
        content = filters.parse_content_filters(params, errors)
        filters.raise_if(errors)

        queryset = Event.objects.all()
        if status != filters.STATUS_ALL:
            queryset = queryset.filter(status=status)
        queryset = filters.apply_content_filters(queryset, content)
        if upcoming:
            queryset = queryset.filter(end_time__gt=timezone.now()).order_by('start_time', 'id')
        else:
            queryset = queryset.order_by('-start_time', '-id')
        return self.paginated(queryset, EventSerializer, 'Events retrieved.')

    @extend_schema(
        operation_id='dawah_events_create',
        request=EventWriteSerializer,
        responses={201: EventSerializer},
        description='Admin: create a draft event.',
        examples=[OpenApiExample('Create event', request_only=True, value=examples.EVENT_CREATE),
                  OpenApiExample('Created', response_only=True, status_codes=['201'],
                                 value=examples.EVENT_DRAFT)],
    )
    def post(self, request):
        serializer = EventWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        event = serializer.save(created_by=request.user)
        return self.respond(event, EventSerializer, 'Event created.', status=201)


class EventDetailView(DawahView):
    serializer_class = EventDetailSerializer

    @extend_schema(operation_id='dawah_events_retrieve', responses={200: EventDetailSerializer},
                   description='Visible event with its visible resources (admins: any event, all resources).',
                   examples=[OpenApiExample('Event', response_only=True, value=examples.EVENT_DETAIL)])
    def get(self, request, event_id):
        return self.respond(self.readable_event(event_id), EventDetailSerializer, 'Event retrieved.')

    def _update(self, request, event_id, partial):
        event = get_object_or_404(Event, pk=event_id)
        serializer = EventWriteSerializer(event, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return self.respond(event, EventDetailSerializer, 'Event updated.')

    @extend_schema(operation_id='dawah_events_update', request=EventWriteSerializer,
                   responses={200: EventDetailSerializer},
                   description='Admin: replace event details. Status changes only through publish/unpublish.')
    def put(self, request, event_id):
        return self._update(request, event_id, partial=False)

    @extend_schema(operation_id='dawah_events_partial_update', request=EventWriteSerializer,
                   responses={200: EventDetailSerializer}, description='Admin: update event details.')
    def patch(self, request, event_id):
        return self._update(request, event_id, partial=True)

    @extend_schema(operation_id='dawah_events_destroy', responses={204: None},
                   description='Admin: delete a draft/unpublished event and its nonpublished resources. '
                               '409 UNPUBLISH_REQUIRED when it or a resource is published; '
                               '409 RESOURCE_REFERENCED when quizzes reference it.')
    def delete(self, request, event_id):
        services.delete_event(event_id)
        return success_response(None, 'Event deleted.', status=204)


class EventPublishView(DawahView):
    serializer_class = EventSerializer

    @extend_schema(operation_id='dawah_events_publish', request=None, responses={200: EventSerializer},
                   description='Admin: draft/unpublished → published. 409 INVALID_STATE_TRANSITION, '
                               'EVENT_NOT_PUBLISHABLE.')
    def post(self, request, event_id):
        return self.respond(services.publish_event(event_id), EventSerializer, 'Event published.')


class EventUnpublishView(DawahView):
    serializer_class = EventSerializer

    @extend_schema(operation_id='dawah_events_unpublish', request=None, responses={200: EventSerializer},
                   description='Admin: published → unpublished. Hides all its resources without changing '
                               'their own status. 409 INVALID_STATE_TRANSITION.')
    def post(self, request, event_id):
        return self.respond(services.unpublish_event(event_id), EventSerializer, 'Event unpublished.')


# ---------------------------------------------------------------- resources

class EventResourceListView(DawahView):
    serializer_class = EventResourceSerializer

    @extend_schema(
        operation_id='dawah_event_resources_list',
        parameters=[*PAGE_PARAMS, STATUS_PARAM, TYPE_PARAM],
        responses={200: EventResourceSerializer(many=True)},
        description='Visible resources of a visible event, ordered by id. Admins see every resource '
                    '(optionally filtered by `status`).',
    )
    def get(self, request, event_id):
        errors = {}
        status = status_filter(request, errors, default=filters.STATUS_ALL)
        resource_type = filters.parse_type(request.query_params, errors)
        filters.raise_if(errors)
        event = self.readable_event(event_id)
        queryset = EventResource.objects.filter(event=event).select_related('event')
        if not reader_is_admin(request):
            queryset = queryset.visible()
        elif status != filters.STATUS_ALL:
            queryset = queryset.filter(status=status)
        if resource_type:
            queryset = queryset.filter(type=resource_type)
        return self.paginated(queryset.order_by('id'), EventResourceSerializer, 'Resources retrieved.')

    @extend_schema(
        operation_id='dawah_event_resources_create',
        request=EventResourceWriteSerializer,
        responses={201: EventResourceSerializer},
        description='Admin: create draft resource metadata under the URL event. No file bytes.',
        examples=[OpenApiExample('Create resource', request_only=True, value=examples.RESOURCE_CREATE),
                  OpenApiExample('Created', response_only=True, status_codes=['201'],
                                 value=examples.RESOURCE_DRAFT)],
    )
    def post(self, request, event_id):
        event = get_object_or_404(Event, pk=event_id)
        serializer = EventResourceWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource = serializer.save(event=event, created_by=request.user)
        return self.respond(resource, EventResourceSerializer, 'Resource created.', status=201)


class EventResourceDetailView(DawahView):
    serializer_class = EventResourceSerializer

    def owned_resource(self, event_id, resource_id):
        return get_object_or_404(EventResource.objects.select_related('event'), pk=resource_id, event_id=event_id)

    @extend_schema(operation_id='dawah_event_resources_retrieve', responses={200: EventResourceSerializer},
                   description='Visible resource that belongs to the URL event.')
    def get(self, request, event_id, resource_id):
        queryset = EventResource.objects.select_related('event')
        if not reader_is_admin(request):
            queryset = queryset.visible()
        resource = get_object_or_404(queryset, pk=resource_id, event_id=event_id)
        return self.respond(resource, EventResourceSerializer, 'Content retrieved.')

    @extend_schema(operation_id='dawah_event_resources_partial_update', request=EventResourceWriteSerializer,
                   responses={200: EventResourceSerializer},
                   description='Admin: update title/type/duration. The parent cannot be changed.')
    def patch(self, request, event_id, resource_id):
        resource = self.owned_resource(event_id, resource_id)
        serializer = EventResourceWriteSerializer(resource, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return self.respond(resource, EventResourceSerializer, 'Resource updated.')

    @extend_schema(operation_id='dawah_event_resources_destroy', responses={204: None},
                   description='Admin: delete a draft/unpublished resource. 409 UNPUBLISH_REQUIRED if published.')
    def delete(self, request, event_id, resource_id):
        services.delete_resource(event_id, resource_id)
        return success_response(None, 'Resource deleted.', status=204)


class EventResourcePublishView(DawahView):
    serializer_class = EventResourceSerializer

    @extend_schema(operation_id='dawah_event_resources_publish', request=None,
                   responses={200: EventResourceSerializer},
                   description='Admin: publish a resource of a published event. 409 EVENT_NOT_PUBLISHED, '
                               'MEDIA_NOT_READY (no ready asset / validated video reference), '
                               'INVALID_STATE_TRANSITION.')
    def post(self, request, event_id, resource_id):
        resource = services.publish_resource(event_id, resource_id)
        return self.respond(resource, EventResourceSerializer, 'Resource published.')


class EventResourceUnpublishView(DawahView):
    serializer_class = EventResourceSerializer

    @extend_schema(operation_id='dawah_event_resources_unpublish', request=None,
                   responses={200: EventResourceSerializer},
                   description='Admin: published → unpublished. 409 INVALID_STATE_TRANSITION.')
    def post(self, request, event_id, resource_id):
        resource = services.unpublish_resource(event_id, resource_id)
        return self.respond(resource, EventResourceSerializer, 'Resource unpublished.')


class ResourceDetailView(DawahView):
    serializer_class = EventResourceSerializer

    @extend_schema(operation_id='dawah_resources_retrieve', responses={200: EventResourceSerializer},
                   description='Stable public detail link: visible resource with parent event metadata.',
                   examples=[OpenApiExample('Ready published audio', response_only=True,
                                            value=examples.RESOURCE_PUBLISHED)])
    def get(self, request, resource_id):
        queryset = EventResource.objects.select_related('event')
        if not reader_is_admin(request):
            queryset = queryset.visible()
        resource = get_object_or_404(queryset, pk=resource_id)
        return self.respond(resource, EventResourceSerializer, 'Content retrieved.')


class ArchiveView(DawahView):
    serializer_class = EventResourceSerializer

    @extend_schema(
        operation_id='dawah_archive_list',
        parameters=[*PAGE_PARAMS, TYPE_PARAM, *CONTENT_PARAMS],
        responses={200: EventResourceSerializer(many=True)},
        description='Visible resources across published events, for every reader (including admins). '
                    'Ordered by event start_time descending, then resource id descending.',
        examples=[OpenApiExample('Archive', response_only=True, value=examples.RESOURCE_PUBLISHED)],
    )
    def get(self, request):
        params, errors = request.query_params, {}
        resource_type = filters.parse_type(params, errors)
        content = filters.parse_content_filters(params, errors)
        filters.raise_if(errors)
        queryset = EventResource.objects.visible().select_related('event')
        if resource_type:
            queryset = queryset.filter(type=resource_type)
        queryset = filters.apply_content_filters(queryset, content, prefix='event__', search_resource_title=True)
        return self.paginated(queryset.order_by('-event__start_time', '-id'), EventResourceSerializer,
                              'Archive retrieved.')
