from rest_framework.views import exception_handler
from rest_framework.renderers import JSONRenderer


class StandardizedJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        response = renderer_context.get('response')

        if response and 200 <= response.status_code < 300:
            data = {
                "success": True,
                "data": data,
                "message": getattr(response, 'message', "Request successful")
            }
        return super().render(data, accepted_media_type, renderer_context)
    

def exception_handler(exc, context):
    response = exception_handler(exc, context)

    if response is not None:
        response.data = {
            "success": False,
            "data": response.data,
            "message": "Validation failed" if response.status_code == 400 else str(exc)
        }

    return response