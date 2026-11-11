from django.urls import path

from . import automation_views, broadcast_views, views


def legacy_alias(view_class):
    """Same behavior as the canonical view, omitted from the OpenAPI schema."""
    return type(f'Legacy{view_class.__name__}', (view_class,), {'schema': None}).as_view()

# Canonical routes end with "/" (D0). The two historical no-slash detail routes
# are kept as aliases for this release; they map to the same views.
urlpatterns = [
    path('events/', views.EventListView.as_view(), name='events-list'),
    path('events/<int:event_id>/', views.EventDetailView.as_view(), name='event-detail'),
    path('events/<int:event_id>/publish/', views.EventPublishView.as_view(), name='event-publish'),
    path('events/<int:event_id>/unpublish/', views.EventUnpublishView.as_view(), name='event-unpublish'),
    path('events/<int:event_id>/resources/', views.EventResourceListView.as_view(), name='event-resources-list'),
    path('events/<int:event_id>/resources/<int:resource_id>/', views.EventResourceDetailView.as_view(),
         name='event-resource-detail'),
    path('events/<int:event_id>/resources/<int:resource_id>/publish/', views.EventResourcePublishView.as_view(),
         name='event-resource-publish'),
    path('events/<int:event_id>/resources/<int:resource_id>/unpublish/',
         views.EventResourceUnpublishView.as_view(), name='event-resource-unpublish'),
    path('events/<int:event_id>/resources/<int:resource_id>/audio/', views.AudioUploadIntentView.as_view(),
         name='event-resource-audio-intent'),
    path('events/<int:event_id>/resources/<int:resource_id>/document/', views.DocumentUploadIntentView.as_view(),
         name='event-resource-document-intent'),
    path('events/<int:event_id>/featured-image/', views.FeaturedImageIntentView.as_view(),
         name='event-featured-image-intent'),
    path('uploads/<int:upload_id>/finalize/', views.UploadFinalizeView.as_view(), name='upload-finalize'),
    path('resources/<int:resource_id>/', views.ResourceDetailView.as_view(), name='resource-detail'),
    path('archive/', views.ArchiveView.as_view(), name='archive'),

    # Broadcasts (D4, admin only).
    path('broadcasts/', broadcast_views.BroadcastListView.as_view(), name='broadcasts-list'),
    path('broadcasts/<int:broadcast_id>/', broadcast_views.BroadcastDetailView.as_view(), name='broadcast-detail'),
    path('broadcasts/<int:broadcast_id>/preview/', broadcast_views.BroadcastPreviewView.as_view(),
         name='broadcast-preview'),
    path('broadcasts/<int:broadcast_id>/queue/', broadcast_views.BroadcastQueueView.as_view(),
         name='broadcast-queue'),
    path('broadcasts/<int:broadcast_id>/deliveries/', broadcast_views.BroadcastDeliveriesView.as_view(),
         name='broadcast-deliveries'),
    path('broadcasts/<int:broadcast_id>/deliveries/<int:delivery_id>/',
         broadcast_views.BroadcastDeliveryDetailView.as_view(), name='broadcast-delivery-detail'),
    path('broadcasts/<int:broadcast_id>/retry/', broadcast_views.BroadcastRetryView.as_view(),
         name='broadcast-retry'),

    # Automations (D4, admin only; COMM-06).
    path('automations/', automation_views.AutomationListView.as_view(), name='automations-list'),
    path('automations/<int:rule_id>/', automation_views.AutomationDetailView.as_view(), name='automation-detail'),
    path('automations/<int:rule_id>/enable/', automation_views.AutomationEnableView.as_view(),
         name='automation-enable'),
    path('automations/<int:rule_id>/disable/', automation_views.AutomationDisableView.as_view(),
         name='automation-disable'),
    path('automations/<int:rule_id>/preview/', automation_views.AutomationPreviewView.as_view(),
         name='automation-preview'),
    path('automations/<int:rule_id>/runs/', automation_views.AutomationRunsView.as_view(), name='automation-runs'),

    # Legacy aliases (no trailing slash).
    path('events/<int:event_id>', legacy_alias(views.EventDetailView), name='event-detail-legacy'),
    path('events/<int:event_id>/resources/<int:resource_id>', legacy_alias(views.EventResourceDetailView),
         name='event-resource-detail-legacy'),
]
