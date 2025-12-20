from django.shortcuts import get_object_or_404, render
from rest_framework import status
from rest_framework.permissions import IsAdminUser, IsAuthenticatedOrReadOnly
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet
from .models import Event, EventResource
from .serializers import (
                        EventSerializer, EventCreateSerializer, 
                        EventResourceSerializer, EventResourceCreateSerializer
                        )


class EventList(APIView):

    permission_classes = [IsAuthenticatedOrReadOnly]

    def get(self, request):
        queryset = Event.objects.all()
        serializer = EventSerializer(queryset, many=True, context = {'request' :request})
        return Response(
            serializer.data
        )

    def post(self, request):
        serializer = EventCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTPS_201_CREATED)


class EventDetail(APIView):

    permission_classes = [IsAuthenticatedOrReadOnly]

    def get(self, request, event_id):
        event = get_object_or_404(Event, pk=event_id)
        serializer = EventSerializer(event)
        return Response(serializer.data)

    def put(self, request, event_id):
        event = get_object_or_404(Event, pk=event_id)
        serializer = EventCreateSerializer(event, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, event_id):
        permission_classes = [IsAdminUser]
        event = get_object_or_404(Event, pk=event_id)
        event.delete()
        return Response(
            { "detail": "Event deleted Successfully" }, 
            status=status.HTTP_204_NO_CONTENT
        )


class EventResourceList(APIView):

    permission_classes = [IsAuthenticatedOrReadOnly]

    def get(self, request, event_id):
        queryset = EventResource.objects.filter(event_id=event_id)
        serializer = EventResourceSerializer(
            queryset, many=True, context = {'request' :request}
        )
        return Response(serializer.data)

    def post(self, request, event_id):
        serializer = EventResourceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(event_id=event_id)
        return Response(serializer.data, status=status.HTTPS_201_CREATED)


class EventResourceDetail(APIView):

    permission_classes = [IsAuthenticatedOrReadOnly]

    def get(self, request, event_id, resource_id):
        resource = get_object_or_404(EventResource, pk=resource_id, event_id=event_id)
        serializer = EventResourceSerializer(resource)
        return Response(serializer.data)

    def put(self, request, event_id, resource_id):
        resource = get_object_or_404(EventResource, pk=resource_id, event_id=event_id)
        serializer = EventResourceCreateSerializer(resource, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(resource_id=resource_id)
        return Response(serializer.data)

    def delete(self, request, event_id, resource_id):
        permission_classes = [IsAdminUser]
        resource = get_object_or_404(EventResource, pk=resource_id, event_id=event_id)
        resource.delete()
        return Response(
            {"detail": "EventResource deleted Successfully"}, 
            status=status.HTTP_204_NO_CONTENT
        )
