"""Django admin (is_staff) is an inspection/correction tool, not a publication path.

Publication/storage state is read-only here and deletion is disabled, so the
product API's publication rules and protected deletion cannot be bypassed.
"""
from django import forms
from django.contrib import admin
from django.db import transaction

from . import automations
from .models import Event, EventResource, StorageCleanup, UploadIntent

STATE_FIELDS = ('status', 'published_at', 'created_by', 'created_at', 'updated_at')
# Validated asset metadata is written only by finalize/publish; object keys and
# ETags are server-only and not shown.
ASSET_FIELDS = ('content_type', 'size_bytes', 'file_name', 'uploaded_at', 'public_media_url')
HIDDEN_ASSET_FIELDS = ('private_object_key', 'public_object_key', 'etag')


class ReadOnlyStateAdmin(admin.ModelAdmin):
    def has_delete_permission(self, request, obj=None):
        return False


class EventResourceInline(admin.TabularInline):
    model = EventResource
    extra = 0
    fields = ('title', 'type', 'duration', 'status', 'storage_status', 'file')
    readonly_fields = ('type', 'status', 'storage_status', 'file')
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False  # resources are created through the API (type is fixed there)


# Featured-image metadata (DAWAH-07) is written only by finalize/publish.
IMAGE_FIELDS = ('featured_image_storage_status', 'featured_image_content_type', 'featured_image_size_bytes',
                'featured_image_file_name', 'featured_image_uploaded_at', 'featured_image_public_url')
HIDDEN_IMAGE_FIELDS = ('featured_image_object_key', 'featured_image_etag', 'featured_image_public_key')


class EventAdminForm(forms.ModelForm):
    class Meta:
        model = Event
        exclude = ('status', 'published_at', 'created_by', 'featured_image', 'schedule_revision', *IMAGE_FIELDS,
                   *HIDDEN_IMAGE_FIELDS)

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get('start_time'), cleaned.get('end_time')
        if start and end and end <= start:
            self.add_error('end_time', 'End time must be after start time.')
        return cleaned


@admin.register(Event)
class EventAdmin(ReadOnlyStateAdmin):
    form = EventAdminForm
    list_display = ('id', 'title', 'category', 'start_time', 'status')
    list_filter = ('status', 'category')
    search_fields = ('title', 'speakers', 'tags')
    ordering = ('-start_time', '-id')
    readonly_fields = (*STATE_FIELDS, 'schedule_revision', 'featured_image', *IMAGE_FIELDS)
    inlines = [EventResourceInline]

    def save_model(self, request, obj, form, change):
        """A start/end change here is a reschedule too (COMM-06): bump the
        revision and cancel obsolete unsent reminders under the event lock."""
        if not (change and {'start_time', 'end_time'} & set(form.changed_data)):
            return super().save_model(request, obj, form, change)
        with transaction.atomic():
            obj.schedule_revision = Event.objects.select_for_update().get(pk=obj.pk).schedule_revision + 1
            super().save_model(request, obj, form, change)
            automations.cancel_event_reminders(obj, automations.SOURCE_RESCHEDULED)


@admin.register(EventResource)
class EventResourceAdmin(ReadOnlyStateAdmin):
    list_display = ('id', 'title', 'type', 'event', 'status', 'storage_status')
    list_filter = ('type', 'status', 'storage_status')
    search_fields = ('title', 'event__title')
    exclude = HIDDEN_ASSET_FIELDS
    readonly_fields = (*STATE_FIELDS, 'event', 'type', 'storage_status', 'file', *ASSET_FIELDS)

    def has_add_permission(self, request):
        return False  # parent and type are read-only here; create through the API


class ReadOnlyAdmin(admin.ModelAdmin):
    """Inspection only: upload/cleanup records change through the API/command."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UploadIntent)
class UploadIntentAdmin(ReadOnlyAdmin):
    list_display = ('id', 'resource', 'status', 'error_code', 'created_by', 'expires_at', 'finalized_at')
    list_filter = ('status', 'kind')
    exclude = ('staging_key', 'validation_key', 'lease_token', 'observed_etag', 'validation_etag')


@admin.register(StorageCleanup)
class StorageCleanupAdmin(ReadOnlyAdmin):
    list_display = ('id', 'bucket', 'reason', 'status', 'resource_id', 'attempts', 'last_error_code', 'created_at')
    list_filter = ('status', 'bucket', 'reason')
    exclude = ('key',)
