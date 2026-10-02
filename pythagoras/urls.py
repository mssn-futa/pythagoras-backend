"""
URL configuration for pythagoras project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from rest_framework.permissions import AllowAny
from rest_framework.settings import api_settings
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView


if not isinstance(api_settings.DEFAULT_SCHEMA_CLASS, type) or \
   api_settings.DEFAULT_SCHEMA_CLASS.__module__ != 'drf_spectacular.openapi':
    api_settings.DEFAULT_SCHEMA_CLASS = AutoSchema


class SchemaView(SpectacularAPIView):
    schema = None
    authentication_classes = []
    permission_classes = [AllowAny]


class SwaggerView(SpectacularSwaggerView):
    schema = None
    authentication_classes = []
    permission_classes = [AllowAny]


urlpatterns = [
    path('', SwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('schema/', SchemaView.as_view(), name='schema'),
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('dawah/', include('dawah.urls')),
]
