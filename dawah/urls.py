from django.urls import path
from . import views


urlpatterns = [
    path('api/events/', views.EventList.as_view(), name='events-list'),
    path('api/events/<int:event_id>', views.EventDetail.as_view(), name='event-detail'),
    path('api/events/<int:event_id>/resources/', views.EventResourceList.as_view(), name='resources-list'),
    path('api/events/<int:event_id>/resources/<int:resource_id>', views.EventResourceDetail.as_view(), name='resources-list'),
]











