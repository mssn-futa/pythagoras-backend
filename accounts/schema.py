"""OpenAPI extension so the shared bearer authentication is documented."""
from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme


class ActiveUserJWTScheme(SimpleJWTScheme):
    target_class = 'accounts.authentication.ActiveUserJWTAuthentication'
