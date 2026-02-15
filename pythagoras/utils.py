from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.renderers import JSONRenderer


def spectacular_preprocessing_filter_spec(endpoints):
    """Exclude spectacular's own views from schema generation."""
    return [
        (path, path_regex, method, callback)
        for path, path_regex, method, callback in endpoints
        if not path.startswith('/schema')
    ]


class StandardizedJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        rc = renderer_context or {}
        response = rc.get('response')

        if response and 200 <= response.status_code < 300:
            data = {
                "success": True,
                "data": data,
                "message": getattr(response, 'message', "Request successful")
            }
        return super().render(data, accepted_media_type, renderer_context)
    

def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)

    if response is not None:
        response.data = {
            "success": False,
            "data": response.data,
            "message": "Validation failed" if response.status_code == 400 else str(exc)
        }

    return response