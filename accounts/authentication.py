"""Bearer authentication that re-checks the account's active state per request."""
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

INACTIVE_CODE = 'ACCOUNT_INACTIVE'
INACTIVE_MESSAGE = 'This account is inactive.'


def inactive_account_error():
    # Imported lazily: DRF loads this module while rest_framework.views initializes.
    from pythagoras.utils import APIError
    return APIError(INACTIVE_CODE, INACTIVE_MESSAGE, status_code=403)


class ActiveUserJWTAuthentication(JWTAuthentication):
    """SimpleJWT access-token authentication. A valid token for a deactivated
    account is rejected with 403 ACCOUNT_INACTIVE (D0), on every endpoint."""

    def get_user(self, validated_token):
        try:
            return super().get_user(validated_token)
        except AuthenticationFailed as exc:
            detail = exc.detail
            code = detail.get('code') if isinstance(detail, dict) else getattr(detail, 'code', None)
            if code == 'user_inactive':
                raise inactive_account_error() from None
            raise
