from django.urls import path

from . import views


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
    path('resources/<int:resource_id>/', views.ResourceDetailView.as_view(), name='resource-detail'),
    path('archive/', views.ArchiveView.as_view(), name='archive'),

    # Legacy aliases (no trailing slash).
    path('events/<int:event_id>', legacy_alias(views.EventDetailView), name='event-detail-legacy'),
    path('events/<int:event_id>/resources/<int:resource_id>', legacy_alias(views.EventResourceDetailView),
         name='event-resource-detail-legacy'),
]
