from django.shortcuts import get_object_or_404, render
from rest_framework import status
from rest_framework.permissions import IsAdminUser, IsAuthenticatedOrReadOnly
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema
from .models import Event, EventResource
from .serializers import (
                        EventSerializer, EventCreateSerializer, 
                        EventResourceSerializer, EventResourceCreateSerializer
                        )


class EventList(APIView):

    permission_classes = [IsAuthenticatedOrReadOnly]

    @extend_schema(
        responses={200: EventSerializer(many=True)},
        description="Retrieve a list of all events."
    )
    def get(self, request):
        queryset = Event.objects.all()
        serializer = EventSerializer(queryset, many=True, context = {'request' :request})
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Event Retrieved Successfully"
            },
            status=status.HTTP_200_OK
        )

    @extend_schema(
        request=EventCreateSerializer,
        responses={201: EventSerializer},
        description="Create a new event."
    )
    def post(self, request):
        serializer = EventCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Event Created Successfully"
            },
            status=status.HTTP_201_CREATED
        )


class EventDetail(APIView):

    permission_classes = [IsAuthenticatedOrReadOnly]

    @extend_schema(
        responses={200: EventSerializer},
        description="Retrieve a specific event by ID."
    )
    def get(self, request, event_id):
        event = get_object_or_404(Event, pk=event_id)
        serializer = EventSerializer(event)
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Event Retrieved Successfully"
            },
            status=status.HTTP_200_OK
        )

    @extend_schema(
        request=EventCreateSerializer,
        responses={200: EventSerializer},
        description="Update an existing event."
    )
    def put(self, request, event_id):
        event = get_object_or_404(Event, pk=event_id)
        serializer = EventCreateSerializer(event, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Event Updated Successfully"
            },
            status=status.HTTP_200_OK
        )

    @extend_schema(
        responses={204: None},
        description="Delete an event."
    )
    def delete(self, request, event_id):
        permission_classes = [IsAdminUser]
        event = get_object_or_404(Event, pk=event_id)
        event.delete()
        return Response(
            {
                "success": True,
                "data": {},
                "message": "Event Deleted Successfully"
            },
            status=status.HTTP_204_NO_CONTENT
        )


class EventResourceList(APIView):

    permission_classes = [IsAuthenticatedOrReadOnly]

    @extend_schema(
        responses={200: EventResourceSerializer(many=True)},
        description="Retrieve all resources for a specific event."
    )
    def get(self, request, event_id):
        queryset = EventResource.objects.filter(event_id=event_id)
        serializer = EventResourceSerializer(
            queryset, many=True, 
            context = {'request' :request}
        )
        return Response(
            (
                "success" == True,
                "data" == serializer.data,
                "message" == "EventResource Created Successfully"
            ),
            status=status.HTTP_201_CREATED
        )

    @extend_schema(
        request=EventResourceCreateSerializer,
        responses={201: EventResourceSerializer},
        description="Create a new resource for an event."
    )
    def post(self, request, event_id):
        serializer = EventResourceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(event_id=event_id)
        return Response(
            (
                "success" == True,
                "data" == serializer.data,
                "message"== "EventResource Created Successfully"
            ),
            status=status.HTTP_201_CREATED
        )


class EventResourceDetail(APIView):

    permission_classes = [IsAuthenticatedOrReadOnly]

    @extend_schema(
        responses={200: EventResourceSerializer},
        description="Retrieve a specific resource by ID."
    )
    def get(self, request, event_id, resource_id):
        resource = get_object_or_404(EventResource, pk=resource_id, event_id=event_id)
        serializer = EventResourceSerializer(resource)
        return Response(serializer.data)

    @extend_schema(
        request=EventResourceCreateSerializer,
        responses={200: EventResourceSerializer},
        description="Update an existing event resource."
    )
    def put(self, request, event_id, resource_id):
        resource = get_object_or_404(EventResource, pk=resource_id, event_id=event_id)
        serializer = EventResourceCreateSerializer(resource, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(resource_id=resource_id)
        return Response(
            {
                "success": True,
                "data" : serializer.data,
                "message":  "EventResource updated Successfully"
            },
            status= status.HTTP_201_CREATED
        )

    @extend_schema(
        responses={204: None},
        description="Delete an event resource."
    )
    def delete(self, request, event_id, resource_id):
        permission_classes = [IsAdminUser]
        resource = get_object_or_404(EventResource, pk=resource_id, event_id=event_id)
        resource.delete()
        return Response(
            {"detail": "EventResource deleted Successfully"}, 
            status=status.HTTP_204_NO_CONTENT
        )
