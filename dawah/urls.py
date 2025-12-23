from django.urls import path, include
from . import views


urlpatterns = [
    path('events/', views.EventList.as_view(), name='events-list'),
    path('events/<int:event_id>', views.EventDetail.as_view(), name='event-detail'),
    path('events/<int:event_id>/resources/', views.EventResourceList.as_view(), name='resources-list'),
    path('events/<int:event_id>/resources/<int:resource_id>', views.EventResourceDetail.as_view(), name='resources-list'),
]











