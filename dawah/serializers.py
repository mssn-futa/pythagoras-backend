from django.core.validators import FileExtensionValidator
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from .models import Event, EventResource


class EventSerializer(serializers.ModelSerializer):
    
    class Meta:
        model = Event
        fields = [
            'title', 'category', 'summary', 'tags', 'description', 
            'speakers', 'location', 'start_time', 'end_time'
        ]


class EventResourceCreateSerializer(serializers.ModelSerializer):

    file = serializers.FileField(
        validators=[
            FileExtensionValidator(
                allowed_extensions=[
                    'pdf', 'docx', 'jpg', 
                    'png', 'mp3', 'mp4'
                ]
            )
        ]
    )
    
    class Meta:
        model = EventResource
        fields = [
            'event', 'title', 'type',
            'file', 'duration'
        ]


class EventCreateSerializer(serializers.ModelSerializer):
    resources = EventResourceCreateSerializer()
    
    def validate_time(self, event):
        if event['start_time'] > event['end_time']:
            raise serializers.ValidationError(
                {"end_time": "End time must be after Start time"}
            )

    def create(self, validated_data):
        resources_data = validated_data.pop('resources')

        with transaction.atomic():
            event = Event.objects.create(**validated_data)

            for resource_data in resources_data:
                EventResource.objects.create(event=event, **resource_data)

        return event
  
    class Meta:
        model = Event
        fields = [
            'title', 'category', 'summary', 'tags', 'description', 
            'speakers', 'location', 'start_time', 'end_time'
        ]


class EventResourceSerializer(serializers.ModelSerializer):
    
    class Meta:
        model = EventResource
        fields = [
            'event', 'title', 'type', 
            'file', 'duration'
        ]
