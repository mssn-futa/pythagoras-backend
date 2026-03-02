from django.conf import settings
from django.db import models

# Create your models here.
class Event(models.Model):
    title = models.CharField(max_length=250)
    category = models.CharField(max_length=250)
    tags = models.CharField(max_length=250)
    description = models.TextField(blank=True, null=True)
    summary = models.TextField(blank=True, null=True)

    speakers = models.CharField(max_length=256)
    location = models.CharField(max_length=256)
    
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    
    featured_image = models.ImageField(upload_to='events/', blank=True, null=True)

    def __str__(self):
        return self.title

class EventResource(models.Model):
    class Type(models.TextChoices):
        DOC = 'doc', 'Document'
        VIDEO = 'vid', 'Video'
        AUDIO = 'aud', 'Audio'

    event = models.ForeignKey(Event, on_delete=models.CASCADE)

    title = models.CharField(max_length=250)
    type = models.CharField(max_length=3, choices=Type)
    file = models.FileField(upload_to='event_resources/', blank=True, null=True)
    duration = models.PositiveSmallIntegerField(blank=True, null=True, help_text="Duration in seconds")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

