from django.core.validators import FileExtensionValidator
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

class EventCreateSerializer(serializers.ModelSerializer):
    
    def validate_time(self, event):
        if event['start_time'] > event['end_time']:
            raise serializers.ValidationError(
                {"end_time": "End time must be after Start time"}
            )
  
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

class EventResourceSerializer(serializers.ModelSerializer):
    
    class Meta:
        model = EventResource
        fields = [
            'event', 'title', 'type', 
            'file', 'duration'
        ]
