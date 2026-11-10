"""Session token lifecycle: issue, rotate (single-use refresh), revoke."""
from datetime import datetime, timezone

import jwt

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.crypto import constant_time_compare
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from pythagoras.utils import APIError

from .authentication import inactive_account_error


def invalid_refresh_error():
    return APIError('TOKEN_INVALID', 'The refresh token is invalid or expired.', status_code=401)


def issue_tokens(user):
    refresh = RefreshToken.for_user(user)  # records an OutstandingToken
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


def _locked_outstanding(token, user):
    """Lock the outstanding row so concurrent rotations of one token serialize."""
    jti = token[api_settings.JTI_CLAIM]
    outstanding, _ = OutstandingToken.objects.get_or_create(
        jti=jti,
        defaults={
            'user': user,
            'token': str(token),
            'created_at': token.current_time,
            'expires_at': datetime.fromtimestamp(token['exp'], tz=timezone.utc),
        },
    )
    return OutstandingToken.objects.select_for_update().get(pk=outstanding.pk)


def rotate_refresh_token(raw):
    """Exchange a valid refresh token of an active user for a new pair.

    The presented token is blacklisted in the same transaction; a second use
    (including a concurrent one) is rejected as invalid.
    """
    try:
        token = RefreshToken(raw)
    except TokenError:
        raise invalid_refresh_error() from None
    User = get_user_model()
    with transaction.atomic():
        user = User.objects.filter(
            **{api_settings.USER_ID_FIELD: token.get(api_settings.USER_ID_CLAIM)}).first()
        if user is None:
            raise invalid_refresh_error()
        if not user.is_active:
            raise inactive_account_error()
        outstanding = _locked_outstanding(token, user)
        _, created = BlacklistedToken.objects.get_or_create(token=outstanding)
        if not created:
            raise invalid_refresh_error()
        return issue_tokens(user)


def revoke_refresh_token(raw):
    """Blacklist a refresh token. Idempotent: a token this server issued that is
    already revoked or expired succeeds silently. Unrecognized input is invalid."""
    try:
        token = RefreshToken(raw)
    except TokenError:
        outstanding = _issued_outstanding(raw)
        if outstanding is None:
            raise invalid_refresh_error() from None
        BlacklistedToken.objects.get_or_create(token=outstanding)
        return
    token.blacklist()


def _issued_outstanding(raw):
    """The OutstandingToken for exactly this issued token string, else None.

    Claims are read unverified only to find the row; equality with the stored
    server-issued token string is what authenticates the presented value.
    """
    try:
        jti = jwt.decode(raw, options={'verify_signature': False}).get(api_settings.JTI_CLAIM)
    except (jwt.PyJWTError, AttributeError):
        return None
    if not isinstance(jti, str):
        return None
    outstanding = OutstandingToken.objects.filter(jti=jti).first()
    if outstanding is None or not constant_time_compare(outstanding.token, raw):
        return None
    return outstanding
