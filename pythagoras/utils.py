"""Stable response/error imports for all backend work packages."""
import logging

from rest_framework.exceptions import APIException, ValidationError
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler, set_rollback

logger = logging.getLogger('pythagoras.api')

ERRORS = {
    400: ('VALIDATION_ERROR', 'Correct the highlighted fields.'),
    401: ('AUTHENTICATION_REQUIRED', 'Valid authentication is required.'),
    403: ('PERMISSION_DENIED', 'You do not have permission to perform this action.'),
    404: ('NOT_FOUND', 'The requested resource was not found.'),
    405: ('METHOD_NOT_ALLOWED', 'This method is not allowed.'),
    406: ('NOT_ACCEPTABLE', 'The requested response format is not supported.'),
    409: ('STATE_CONFLICT', 'The operation conflicts with the current state.'),
    415: ('UNSUPPORTED_MEDIA_TYPE', 'The request content type is not supported.'),
    429: ('THROTTLED', 'Too many requests. Please try again later.'),
    500: ('INTERNAL_ERROR', 'An internal error occurred. Please try again later.'),
    503: ('PROVIDER_UNAVAILABLE', 'The service is temporarily unavailable.'),
}


class APIError(APIException):
    """Explicit, public domain error. Messages/fields must not contain secrets."""
    status_code = 409
    default_code = 'STATE_CONFLICT'
    default_detail = 'The operation conflicts with the current state.'

    def __init__(self, code=None, message=None, *, status_code=409, fields=None):
        self.status_code = status_code
        self.code = code or ERRORS.get(status_code, ERRORS[500])[0]
        self.fields = fields
        super().__init__(message or ERRORS.get(status_code, ERRORS[500])[1], self.code)


def is_envelope(data):
    return (
        isinstance(data, dict)
        and type(data.get('success')) is bool
        and 'data' in data
        and isinstance(data.get('message'), str)
    )


def success_response(data=None, message='Request successful.', *, status=200):
    return Response({'success': True, 'data': data, 'message': message}, status=status)


def error_envelope(status_code, *, code=None, message=None, fields=None):
    default_code, default_message = ERRORS.get(status_code, ERRORS[500])
    data = {'code': code or default_code}
    if fields is not None:
        data['fields'] = fields
    return {'success': False, 'data': data, 'message': message or default_message}


def spectacular_preprocessing_filter_spec(endpoints):
    """Exclude schema views without affecting application mounts."""
    return [endpoint for endpoint in endpoints if not endpoint[0].startswith('/schema')]


class StandardizedJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        response = (renderer_context or {}).get('response')
        if response is not None:
            if response.status_code == 204:
                return b''
            if 200 <= response.status_code < 300:
                if not is_envelope(data):
                    data = {'success': True, 'data': data,
                            'message': getattr(response, 'message', 'Request successful.')}
            else:
                normalized = (
                    is_envelope(data) and data['success'] is False
                    and isinstance(data['data'], dict)
                    and isinstance(data['data'].get('code'), str)
                )
                if response.status_code >= 500 or not normalized:
                    # Direct and legacy errors cannot bypass internal sanitization.
                    data = error_envelope(response.status_code)
                if response.status_code >= 500 and not getattr(response, '_api_error_logged', False):
                    log_internal_error(response, (renderer_context or {}).get('request'), data)
        return super().render(data, accepted_media_type, renderer_context)


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        set_rollback()
        response = Response(status=500)
    fields = None
    code = message = None
    if isinstance(exc, APIError) and response.status_code < 500:
        code, message, fields = exc.code, str(exc.detail), exc.fields
    elif isinstance(exc, ValidationError):
        fields = response.data if isinstance(response.data, dict) else {'non_field_errors': response.data}
    elif response.status_code == 400:
        code, message = 'MALFORMED_REQUEST', 'The request could not be parsed.'
    response.data = error_envelope(response.status_code, code=code, message=message, fields=fields)
    if response.status_code >= 500:
        log_internal_error(response, context.get('request'), response.data)
    return response


def log_internal_error(response, request, data):
    # Never include exception text, traceback, URL, headers or request payload.
    logger.error('api_error request_id=%s code=%s',
                 getattr(request, 'request_id', 'unavailable'), data['data']['code'])
    response._api_error_logged = True
