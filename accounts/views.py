from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import UserLoginSerializer, UserRegistrationSerializer, UserSerializer


class UserRegistrationView(APIView):
    """
    API endpoint for user registration.

    POST /accounts/register/
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = UserRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        return Response(
            {
                "success": True,
                "data": UserSerializer(user).data,
                "message": "User registered successfully.",
            },
            status=status.HTTP_201_CREATED,
        )


class UserLoginView(APIView):
    """
    API endpoint for user login.
    Accepts email or matric number with password.

    POST /accounts/login/
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = UserLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]
        tokens = serializer.validated_data["tokens"]

        return Response(
            {
                "success": True,
                "data": {
                    "user": UserSerializer(user).data,
                    "tokens": tokens,
                },
                "message": "Login successful.",
            },
            status=status.HTTP_200_OK,
        )
