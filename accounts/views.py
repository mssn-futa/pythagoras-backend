from django.contrib.auth.models import update_last_login
from django.contrib.auth import get_user_model
from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from pythagoras.permissions import IsActiveUser
from pythagoras.utils import APIError, success_response

from .authentication import inactive_account_error
from . import student_admin
from .permissions import IsActiveAdmin
from .serializers import (
    LoginResponseSerializer,
    ProfileUpdateSerializer,
    RefreshTokenSerializer,
    StudentListFilterSerializer,
    StudentSerializer,
    StudentUpdateSerializer,
    TokenPairSerializer,
    UserLoginSerializer,
    UserRegistrationSerializer,
    UserSerializer,
)
from .services import EmailService
from .throttles import LoginRateThrottle, RefreshRateThrottle, RegistrationRateThrottle
from .tokens import issue_tokens, revoke_refresh_token, rotate_refresh_token

User = get_user_model()

EXAMPLE_USER = {
    "id": 1,
    "email": "student@example.test",
    "first_name": "Amina",
    "last_name": "Yusuf",
    "matric_number": "CSC/21/001",
    "department": "Computer Science",
    "level": "300",
    "role": "student",
    "is_active": True,
}


def envelope_example(name, data, message, *, status_codes=("200",), success=True):
    return OpenApiExample(
        name,
        value={"success": success, "data": data, "message": message},
        response_only=True,
        status_codes=list(status_codes),
    )


def error_example(name, status_code, code, message, fields=None):
    data = {"code": code}
    if fields is not None:
        data["fields"] = fields
    return envelope_example(name, data, message, status_codes=(str(status_code),), success=False)


INVALID_TOKEN_EXAMPLE = error_example(
    "Invalid, expired or reused refresh token", 401, "TOKEN_INVALID",
    "The refresh token is invalid or expired.")
INACTIVE_EXAMPLE = error_example(
    "Inactive account", 403, "ACCOUNT_INACTIVE", "This account is inactive.")
THROTTLED_EXAMPLE = error_example(
    "Throttled", 429, "THROTTLED", "Too many requests. Please try again later.")


def error_response(description):
    # The shared EnvelopeAutoSchema substitutes the standard error envelope.
    return OpenApiResponse(response=OpenApiTypes.OBJECT, description=description)


VALIDATION_RESPONSE = error_response("Field validation failed.")
THROTTLED_RESPONSE = error_response("Too many requests from this IP; see Retry-After.")
INVALID_TOKEN_RESPONSE = error_response("Invalid, expired, revoked or reused refresh token.")
INACTIVE_RESPONSE = error_response("The account is inactive.")


def invalid_credentials_error():
    return APIError(
        "INVALID_CREDENTIALS", "Invalid email/matric number or password.", status_code=401
    )


class PublicAuthView(APIView):
    """Credential endpoints ignore any bearer header (a stale one must not block
    sign-in or refresh) and authenticate by request body only."""

    authentication_classes = []
    permission_classes = [AllowAny]


class UserRegistrationView(PublicAuthView):
    """POST /accounts/register/"""

    throttle_classes = [RegistrationRateThrottle]

    @extend_schema(
        operation_id="accounts_register",
        request=UserRegistrationSerializer,
        responses={201: UserSerializer, 400: VALIDATION_RESPONSE, 429: THROTTLED_RESPONSE},
        description=(
            "Create an active student account. Role, active state and permission flags "
            "are rejected. An optional welcome email is attempted after the account is "
            "saved; the response never claims delivery."
        ),
        examples=[
            OpenApiExample(
                "Registration",
                value={
                    "email": "student@example.test",
                    "password": "<user password>",
                    "password_confirm": "<user password>",
                    "first_name": "Amina",
                    "last_name": "Yusuf",
                    "matric_number": "CSC/21/001",
                    "department": "Computer Science",
                    "level": "300",
                },
                request_only=True,
            ),
            envelope_example("Created", EXAMPLE_USER, "Account created.", status_codes=("201",)),
            error_example(
                "Duplicate email", 400, "VALIDATION_ERROR", "Correct the highlighted fields.",
                {"email": ["An account with this email already exists."]}),
            error_example(
                "Privileged field", 400, "VALIDATION_ERROR", "Correct the highlighted fields.",
                {"role": ["This field cannot be set or changed here."]}),
            THROTTLED_EXAMPLE,
        ],
    )
    def post(self, request):
        serializer = UserRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            user = serializer.save()
            EmailService.schedule_welcome_email(user)
        return success_response(
            UserSerializer(user).data, "Account created.", status=status.HTTP_201_CREATED
        )


class UserLoginView(PublicAuthView):
    """POST /accounts/login/"""

    throttle_classes = [LoginRateThrottle]

    @extend_schema(
        operation_id="accounts_login",
        request=UserLoginSerializer,
        responses={
            200: LoginResponseSerializer,
            400: VALIDATION_RESPONSE,
            401: error_response("Generic credential error for any wrong identifier/password."),
            403: INACTIVE_RESPONSE,
            429: THROTTLED_RESPONSE,
        },
        description=(
            "Authenticate with email or matric number and password. Returns the user and a "
            "token pair (access 60 minutes, refresh 7 days). Wrong identifier or password "
            "returns one generic credential error."
        ),
        examples=[
            OpenApiExample(
                "Login",
                value={"email_or_matric": "CSC/21/001", "password": "<user password>"},
                request_only=True,
            ),
            envelope_example(
                "Signed in",
                {"user": EXAMPLE_USER, "tokens": {"access": "<token>", "refresh": "<token>"}},
                "Signed in.",
            ),
            error_example("Bad credentials", 401, "INVALID_CREDENTIALS",
                          "Invalid email/matric number or password."),
            INACTIVE_EXAMPLE,
            THROTTLED_EXAMPLE,
        ],
    )
    def post(self, request):
        serializer = UserLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.get_user()
        if user is None:
            raise invalid_credentials_error()
        if not user.is_active:
            raise inactive_account_error()
        tokens = issue_tokens(user)
        update_last_login(None, user)
        return success_response(
            {"user": UserSerializer(user).data, "tokens": tokens}, "Signed in."
        )


class TokenRefreshView(PublicAuthView):
    """POST /accounts/refresh/"""

    throttle_classes = [RefreshRateThrottle]

    @extend_schema(
        operation_id="accounts_refresh",
        request=RefreshTokenSerializer,
        responses={
            200: TokenPairSerializer,
            400: VALIDATION_RESPONSE,
            401: INVALID_TOKEN_RESPONSE,
            403: INACTIVE_RESPONSE,
            429: THROTTLED_RESPONSE,
        },
        description=(
            "Exchange a refresh token for a new access token and a rotated refresh token. "
            "The presented refresh token is revoked; reusing it fails."
        ),
        examples=[
            OpenApiExample("Refresh", value={"refresh": "<token>"}, request_only=True),
            envelope_example(
                "Rotated", {"access": "<token>", "refresh": "<rotated token>"},
                "Session refreshed."),
            INVALID_TOKEN_EXAMPLE,
            INACTIVE_EXAMPLE,
            THROTTLED_EXAMPLE,
        ],
    )
    def post(self, request):
        serializer = RefreshTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tokens = rotate_refresh_token(serializer.validated_data["refresh"])
        return success_response(tokens, "Session refreshed.")


class LogoutView(PublicAuthView):
    """POST /accounts/logout/"""

    @extend_schema(
        operation_id="accounts_logout",
        request=RefreshTokenSerializer,
        responses={
            204: OpenApiResponse(description="Refresh token revoked (also on replay)."),
            400: VALIDATION_RESPONSE,
            401: INVALID_TOKEN_RESPONSE,
        },
        description=(
            "Revoke the presented refresh token. Replaying an already revoked or expired "
            "token issued by this server also returns an empty 204. Existing access tokens "
            "remain valid until expiry."
        ),
        examples=[
            OpenApiExample("Logout", value={"refresh": "<token>"}, request_only=True),
            INVALID_TOKEN_EXAMPLE,
        ],
    )
    def post(self, request):
        serializer = RefreshTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        revoke_refresh_token(serializer.validated_data["refresh"])
        return success_response(None, "Signed out.", status=status.HTTP_204_NO_CONTENT)


class CurrentUserView(APIView):
    """GET/PATCH /accounts/me/"""

    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="accounts_me_retrieve",
        responses={200: UserSerializer, 403: INACTIVE_RESPONSE},
        description="The authenticated account's own representation.",
        examples=[envelope_example("Current user", EXAMPLE_USER, "Request successful."),
                  INACTIVE_EXAMPLE],
    )
    def get(self, request):
        return success_response(UserSerializer(request.user).data)

    @extend_schema(
        operation_id="accounts_me_update",
        request=ProfileUpdateSerializer,
        responses={200: UserSerializer, 400: VALIDATION_RESPONSE, 403: INACTIVE_RESPONSE},
        description=(
            "Edit own first_name, last_name, department and level (100-500). Any other "
            "field, including email, matric_number, role and is_active, is rejected."
        ),
        examples=[
            OpenApiExample(
                "Profile edit",
                value={"first_name": "Amina", "department": "Computer Science", "level": "400"},
                request_only=True,
            ),
            envelope_example(
                "Updated", {**EXAMPLE_USER, "level": "400"}, "Profile updated."),
            error_example(
                "Protected field", 400, "VALIDATION_ERROR", "Correct the highlighted fields.",
                {"email": ["This field cannot be set or changed here."]}),
        ],
    )
    def patch(self, request):
        serializer = ProfileUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return success_response(UserSerializer(user).data, "Profile updated.")


EXAMPLE_STUDENT = {**EXAMPLE_USER, "date_joined": "2026-09-14T08:30:00Z"}
FORBIDDEN_RESPONSE = error_response("Not an active administrator, or the account is inactive.")
NOT_FOUND_RESPONSE = error_response("No student with this id (administrators are not students).")
FORBIDDEN_EXAMPLE = error_example(
    "Not an administrator", 403, "PERMISSION_DENIED", "You must be an admin to perform this action.")
NOT_FOUND_EXAMPLE = error_example(
    "Unknown student", 404, "NOT_FOUND", "The requested resource was not found.")


class StudentListView(generics.ListAPIView):
    """GET /accounts/students/ (administrators only)."""

    permission_classes = [IsActiveAdmin]
    serializer_class = StudentSerializer

    def get_queryset(self):
        filters = StudentListFilterSerializer(data=self.request.query_params)
        filters.is_valid(raise_exception=True)
        return student_admin.filter_students(student_admin.students(), filters.validated_data)

    @extend_schema(
        operation_id="accounts_students_list",
        parameters=[
            OpenApiParameter("search", str, description=(
                "Case-insensitive; every whitespace-separated term must match the full name, "
                "email or matric number.")),
            OpenApiParameter("department", str, description=(
                "Exact department name, compared trimmed and case-insensitively.")),
            OpenApiParameter("level", str, enum=["100", "200", "300", "400", "500"]),
            OpenApiParameter("is_active", bool),
        ],
        responses={200: StudentSerializer(many=True), 400: VALIDATION_RESPONSE,
                   403: FORBIDDEN_RESPONSE},
        description=(
            "Paginated student-role accounts only (page, page_size default 20 max 100), "
            "ordered by last name, first name, id. Filters combine with AND."
        ),
        examples=[
            envelope_example(
                "Page of students",
                {"count": 1, "next": None, "previous": None, "results": [EXAMPLE_STUDENT]},
                "Request successful."),
            error_example("Invalid level", 400, "VALIDATION_ERROR", "Correct the highlighted fields.",
                          {"level": ['"900" is not a valid choice.']}),
            FORBIDDEN_EXAMPLE,
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class StudentDetailView(APIView):
    """GET/PATCH /accounts/students/{id}/ (administrators only)."""

    permission_classes = [IsActiveAdmin]

    @extend_schema(
        operation_id="accounts_students_retrieve",
        responses={200: StudentSerializer, 403: FORBIDDEN_RESPONSE, 404: NOT_FOUND_RESPONSE},
        description="One student. Administrator ids return 404.",
        examples=[envelope_example("Student", EXAMPLE_STUDENT, "Request successful."),
                  FORBIDDEN_EXAMPLE, NOT_FOUND_EXAMPLE],
    )
    def get(self, request, pk):
        student = student_admin.students().filter(pk=pk).first()
        if student is None:
            raise NotFound()
        return success_response(StudentSerializer(student).data)

    @extend_schema(
        operation_id="accounts_students_update",
        request=StudentUpdateSerializer,
        responses={
            200: StudentSerializer, 400: VALIDATION_RESPONSE, 403: FORBIDDEN_RESPONSE,
            404: NOT_FOUND_RESPONSE,
            409: error_response("Status change refused (self-deactivation or last administrator)."),
        },
        description=(
            "Partial update of first_name, last_name, department, level, email, matric_number "
            "and is_active (normalized like registration). Deactivation takes effect on the "
            "student's very next request. Any other key, including role and permission flags, "
            "is rejected with 400."
        ),
        examples=[
            OpenApiExample("Deactivate", value={"is_active": False}, request_only=True),
            OpenApiExample(
                "Edit identity",
                value={"email": "Amina.Yusuf@Example.test", "matric_number": " csc/21/001 ",
                       "level": "400"},
                request_only=True),
            envelope_example(
                "Updated", {**EXAMPLE_STUDENT, "is_active": False}, "Student updated."),
            error_example(
                "Duplicate email", 400, "VALIDATION_ERROR", "Correct the highlighted fields.",
                {"email": ["An account with this email already exists."]}),
            error_example(
                "Protected field", 400, "VALIDATION_ERROR", "Correct the highlighted fields.",
                {"role": ["This field cannot be set or changed here."]}),
            FORBIDDEN_EXAMPLE, NOT_FOUND_EXAMPLE,
        ],
    )
    def patch(self, request, pk):
        changes_status = hasattr(request.data, "keys") and "is_active" in request.data
        with transaction.atomic():
            if changes_status:
                locked = student_admin.lock_status_scope(pk)
            else:
                locked = User.objects.select_for_update().filter(pk=pk).first()
            if locked is None or locked.role != User.Role.STUDENT:
                raise NotFound()
            serializer = StudentUpdateSerializer(locked, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            if "is_active" in serializer.validated_data:
                student_admin.assert_status_change_allowed(
                    locked, request.user, serializer.validated_data["is_active"])
            student = serializer.save()
        return success_response(StudentSerializer(student).data, "Student updated.")
