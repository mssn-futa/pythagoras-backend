"""OpenAPI shapes for the JSON renderer's actual envelope."""
from copy import deepcopy

from drf_spectacular.openapi import AutoSchema

from .utils import ERRORS


def envelope_schema(data, success=True):
    return {
        'type': 'object',
        'required': ['success', 'data', 'message'],
        'properties': {
            'success': {'type': 'boolean', 'enum': [success]},
            'data': data,
            'message': {'type': 'string'},
        },
    }


ERROR_SCHEMA = envelope_schema({
    'type': 'object',
    'required': ['code'],
    'properties': {
        'code': {'type': 'string'},
        'fields': {'type': 'object', 'additionalProperties': True},
    },
}, success=False)


class EnvelopeAutoSchema(AutoSchema):
    def _get_response_for_code(self, serializer, status_code, media_types=None, direction='response'):
        response = super()._get_response_for_code(serializer, status_code, media_types, direction)
        if status_code == '204':
            response.pop('content', None)
            return response
        for media_type, content in response.get('content', {}).items():
            if media_type != 'application/json':
                continue
            if status_code.startswith('2'):
                schema = content['schema']
                if not {'success', 'data', 'message'}.issubset(schema.get('properties', {})):
                    content['schema'] = envelope_schema(schema)
                    for example in content.get('examples', {}).values():
                        value = example.get('value')
                        if value is not None and not (
                            isinstance(value, dict) and {'success', 'data', 'message'} <= value.keys()
                        ):
                            example['value'] = {'success': True, 'data': value, 'message': 'Request successful.'}
            else:
                content['schema'] = deepcopy(ERROR_SCHEMA)
        return response

    def _get_response_bodies(self, direction='response'):
        responses = super()._get_response_bodies(direction)
        for status_code, (_, message) in ERRORS.items():
            responses.setdefault(str(status_code), {
                'description': message,
                'content': {'application/json': {'schema': deepcopy(ERROR_SCHEMA)}},
            })
        return responses
