from datetime import timezone

from rest_framework import serializers


class UTCDateTimeField(serializers.DateTimeField):
    def __init__(self, **kwargs):
        super().__init__(default_timezone=timezone.utc, **kwargs)


class IdentifiedSerializer(serializers.Serializer):
    """Base for non-model actionable payloads; ModelSerializer already emits IDs."""
    id = serializers.IntegerField(read_only=True)
