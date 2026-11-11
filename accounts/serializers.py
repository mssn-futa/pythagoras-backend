from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers

from .identity import normalize_email, normalize_matric

User = get_user_model()

EMAIL_TAKEN = "An account with this email already exists."
MATRIC_TAKEN = "An account with this matric number already exists."
PROTECTED_FIELD = "This field cannot be set or changed here."


class NormalizedEmailField(serializers.EmailField):
    def to_internal_value(self, data):
        return normalize_email(super().to_internal_value(data))


class NormalizedMatricField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 20)
        kwargs.setdefault("required", False)
        kwargs.setdefault("allow_null", True)
        kwargs.setdefault("allow_blank", True)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        return normalize_matric(super().to_internal_value(data))


class StrictFieldsMixin:
    """Reject any input key outside the writable allowlist (400), so privileged
    or protected fields are never silently ignored or applied."""

    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            writable = {name for name, field in self.fields.items() if not field.read_only}
            rejected = sorted(set(data.keys()) - writable)
            if rejected:
                raise serializers.ValidationError({name: [PROTECTED_FIELD] for name in rejected})
        return super().to_internal_value(data)


class UserSerializer(serializers.ModelSerializer):
    """The frozen D1 user representation (login, current user, registration)."""

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
            "is_active",
        ]
        read_only_fields = fields


def identity_conflict_errors(email=None, matric=None, exclude_pk=None):
    qs = User.objects.all()
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    errors = {}
    if email and qs.filter(email__iexact=email).exists():
        errors["email"] = [EMAIL_TAKEN]
    if matric and qs.filter(matric_number__iexact=matric).exists():
        errors["matric_number"] = [MATRIC_TAKEN]
    return errors


class UserRegistrationSerializer(StrictFieldsMixin, serializers.ModelSerializer):
    """
    Public registration. Always creates an active student; role, active state
    and permission flags are not accepted.
    """

    email = NormalizedEmailField(max_length=254)
    matric_number = NormalizedMatricField()
    password = serializers.CharField(
        write_only=True,
        required=True,
        trim_whitespace=False,
        style={"input_type": "password"},
    )
    password_confirm = serializers.CharField(
        write_only=True,
        required=True,
        trim_whitespace=False,
        style={"input_type": "password"},
    )

    class Meta:
        model = User
        fields = [
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
            "first_name": {"required": True, "allow_blank": False},
            "last_name": {"required": True, "allow_blank": False},
        }

    def validate_email(self, value):
        if identity_conflict_errors(email=value):
            raise serializers.ValidationError(EMAIL_TAKEN)
        return value

    def validate_matric_number(self, value):
        if identity_conflict_errors(matric=value):
            raise serializers.ValidationError(MATRIC_TAKEN)
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError(
                {"password_confirm": ["Password fields didn't match."]}
            )
        candidate = User(**{k: v for k, v in attrs.items() if k not in ("password", "password_confirm")})
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)})
        return attrs

    def create(self, validated_data):
        validated_data.pop("password_confirm")
        password = validated_data.pop("password")
        try:
            with transaction.atomic():
                return User.objects.create_user(
                    password=password,
                    role=User.Role.STUDENT,
                    is_active=True,
                    is_staff=False,
                    is_superuser=False,
                    **validated_data,
                )
        except IntegrityError:
            # A concurrent registration won the race for the same identity.
            errors = identity_conflict_errors(
                email=validated_data.get("email"), matric=validated_data.get("matric_number")
            )
            raise serializers.ValidationError(
                errors or {"non_field_errors": ["This account could not be created."]}
            )


class ProfileUpdateSerializer(StrictFieldsMixin, serializers.ModelSerializer):
    """Self-service profile edits: names, department and level only (D1)."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "department", "level"]
        extra_kwargs = {
            "first_name": {"allow_blank": False},
            "last_name": {"allow_blank": False},
        }


class UserLoginSerializer(serializers.Serializer):
    """
    Credentials. Accepts email (contains "@") or matric number with password.
    """

    email_or_matric = serializers.CharField(
        required=True,
        max_length=254,
        help_text="Email address or matric number",
    )
    password = serializers.CharField(
        required=True,
        write_only=True,
        trim_whitespace=False,
        style={"input_type": "password"},
    )

    def get_user(self):
        """Return the user for valid credentials, else None. Timing is
        comparable whether or not an account exists."""
        identifier = self.validated_data["email_or_matric"]
        password = self.validated_data["password"]
        if "@" in identifier:
            lookup = {"email": normalize_email(identifier)}
        else:
            lookup = {"matric_number": normalize_matric(identifier)}
        user = User.objects.filter(**lookup).first() if all(lookup.values()) else None
        if user is None:
            make_password(password)  # equalize hashing cost
            return None
        if not user.check_password(password):
            return None
        return user


class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()


class LoginResponseSerializer(serializers.Serializer):
    user = UserSerializer()
    tokens = TokenPairSerializer()


class RefreshTokenSerializer(serializers.Serializer):
    refresh = serializers.CharField(trim_whitespace=True)
