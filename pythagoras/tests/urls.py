"""Executable wire-contract fixtures; not mounted in application URLs."""
from django.urls import path
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, Throttled, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import CustomUser
from pythagoras.utils import APIError, success_response


class ItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()


class RawView(APIView):
    permission_classes = [AllowAny]
    serializer_class = ItemSerializer

    @extend_schema(responses=ItemSerializer)
    def get(self, request):
        return Response({'id': 1})

    @extend_schema(request=ItemSerializer, responses=ItemSerializer)
    def post(self, request):
        serializer = ItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data)

    @extend_schema(responses={204: ItemSerializer})
    def delete(self, request):
        return Response({'id': 1}, status=204)


class EnvelopedView(RawView):
    def get(self, request):
        return success_response({'id': 1}, 'Saved.', status=201)


class ErrorView(RawView):
    def get(self, request, kind):
        if kind == 'validation':
            raise ValidationError({'title': ['This field is required.']})
        if kind == 'nonfield':
            raise ValidationError(['Invalid combination.'])
        if kind == 'permission':
            raise PermissionDenied('private internal policy details')
        if kind == 'throttle':
            raise Throttled(wait=12)
        if kind == 'conflict':
            raise APIError('QUIZ_IMMUTABLE', 'Published quizzes cannot be changed.')
        if kind == 'provider':
            raise APIError('PROVIDER_UNAVAILABLE', 'provider-key-must-not-leak', status_code=503)
        if kind == 'direct':
            return Response({'detail': 'private-internal-detail'}, status=500)
        if kind == 'missing':
            from django.http import Http404
            raise Http404('private record detail')
        raise RuntimeError('password=must-not-leak token=must-not-leak')


class ProtectedView(RawView):
    permission_classes = [IsAuthenticated]


class ItemListView(ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = ItemSerializer
    queryset = CustomUser.objects.order_by('first_name')


urlpatterns = [
    path('raw/', RawView.as_view()),
    path('enveloped/', EnvelopedView.as_view()),
    path('errors/<str:kind>/', ErrorView.as_view()),
    path('protected/', ProtectedView.as_view()),
    path('items/', ItemListView.as_view()),
]
