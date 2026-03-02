from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from .services import EmailService

User = get_user_model()


class UserRegistrationSerializer(serializers.ModelSerializer):
    """
    Serializer for user registration.
    Handles validation and user creation.
    """

    password = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password],
        style={"input_type": "password"},
    )
    password_confirm = serializers.CharField(
        write_only=True,
        required=True,
        style={"input_type": "password"},
    )

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "password",
            "password_confirm",
            "first_name",
            "last_name",
            "matric_number",
            "department",
            "level",
        ]
        extra_kwargs = {
            "first_name": {"required": True},
            "last_name": {"required": True},
        }

    def validate(self, attrs):
        """
        Validate that passwords match.
        """
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError(
                {"password_confirm": "Password fields didn't match."}
            )
        return attrs

    def create(self, validated_data):
        """
        Create and return a new user.
        """
        validated_data.pop("password_confirm")
        password = validated_data.pop("password")
        user = User.objects.create_user(password=password, **validated_data)

        EmailService.send_welcome_email(user)

        return user


class UserSerializer(serializers.ModelSerializer):
    """
    Serializer for user details.
    """

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "matric_number",
            "department",
            "level",
            "role",
            "date_joined",
        ]
        read_only_fields = ["id", "email", "date_joined", "role"]


class UserLoginSerializer(serializers.Serializer):
    """
    Serializer for user login.
    Accepts email or matric number with password.
    """

    email_or_matric = serializers.CharField(
        required=True,
        help_text="Email address or matric number",
    )
    password = serializers.CharField(
        required=True,
        write_only=True,
        style={"input_type": "password"},
    )

    def validate(self, attrs):
        """
        Validate credentials and authenticate user.
        """
        email_or_matric = attrs.get("email_or_matric")
        password = attrs.get("password")

        user = User.objects.filter(
            Q(email__iexact=email_or_matric) | Q(matric_number__iexact=email_or_matric)
        ).first()

        if not user:
            raise serializers.ValidationError(
                {"email_or_matric": "No account found with this email or matric number."}
            )

        if not user.is_active:
            raise serializers.ValidationError(
                {"email_or_matric": "This account has been deactivated."}
            )

        if not user.check_password(password):
            raise serializers.ValidationError(
                {"password": "Incorrect password."}
            )

        refresh = RefreshToken.for_user(user)

        attrs["user"] = user
        attrs["tokens"] = {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
        }

        return attrs
