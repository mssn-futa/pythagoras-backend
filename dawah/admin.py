"""Django admin (is_staff) is an inspection/correction tool, not a publication path.

Publication/storage state is read-only here and deletion is disabled, so the
product API's publication rules and protected deletion cannot be bypassed.
"""
from django.contrib import admin

from .models import Event, EventResource

STATE_FIELDS = ('status', 'published_at', 'created_by', 'created_at', 'updated_at')


class ReadOnlyStateAdmin(admin.ModelAdmin):
    def has_delete_permission(self, request, obj=None):
        return False


class EventResourceInline(admin.TabularInline):
    model = EventResource
    extra = 0
    fields = ('title', 'type', 'duration', 'status', 'storage_status', 'file')
    readonly_fields = ('status', 'storage_status', 'file')
    can_delete = False
    show_change_link = True


@admin.register(Event)
class EventAdmin(ReadOnlyStateAdmin):
    list_display = ('id', 'title', 'category', 'start_time', 'status')
    list_filter = ('status', 'category')
    search_fields = ('title', 'speakers', 'tags')
    ordering = ('-start_time', '-id')
    readonly_fields = (*STATE_FIELDS, 'featured_image')
    inlines = [EventResourceInline]


@admin.register(EventResource)
class EventResourceAdmin(ReadOnlyStateAdmin):
    list_display = ('id', 'title', 'type', 'event', 'status', 'storage_status')
    list_filter = ('type', 'status', 'storage_status')
    search_fields = ('title', 'event__title')
    readonly_fields = (*STATE_FIELDS, 'event', 'type', 'storage_status', 'file')
