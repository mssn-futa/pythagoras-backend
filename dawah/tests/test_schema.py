from copy import deepcopy

from django.test import TestCase
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, RefResolver

from .helpers import PUBLISHED, DawahAPITestCase, event_payload, make_event, make_resource, make_visible_resource


def draft4(node):
    """Translate OpenAPI 3.0 ``nullable`` into JSON Schema for validation."""
    if isinstance(node, list):
        return [draft4(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: draft4(value) for key, value in node.items()}
    if node.pop('nullable', False):
        if 'type' in node:
            node['type'] = [node['type'], 'null']
        else:
            node = {'anyOf': [node, {'type': 'null'}]}
    return node


class DawahSchemaTests(DawahAPITestCase, TestCase):
    """The schema describes the real enveloped wire payloads and examples."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(public=True)
        cls.json_schema = draft4(deepcopy(cls.schema))

    def validate(self, path, method, status, body):
        responses = self.json_schema['paths'][path][method]['responses']
        content = responses[status]['content']['application/json']
        Draft4Validator(content['schema'], resolver=RefResolver.from_schema(self.json_schema)).validate(body)
        return content

    def test_real_responses_and_examples_match_schema(self):
        event = make_event(status=PUBLISHED, created_by=self.admin)
        resource = make_visible_resource(event)
        draft = make_event()
        make_resource(draft)
        self.as_admin()
        created = self.client.post('/dawah/events/', event_payload(), format='json')
        created_resource = self.client.post(f'/dawah/events/{draft.pk}/resources/', {'title': 'x', 'type': 'aud'},
                                            format='json')
        cases = [
            ('/dawah/events/', 'get', '200', self.client.get('/dawah/events/')),
            ('/dawah/events/', 'post', '201', created),
            ('/dawah/events/', 'post', '400', self.client.post('/dawah/events/', {}, format='json')),
            ('/dawah/events/{event_id}/', 'get', '200', self.client.get(f'/dawah/events/{draft.pk}/')),
            ('/dawah/events/{event_id}/', 'patch', '200',
             self.client.patch(f'/dawah/events/{draft.pk}/', {'title': 'y'}, format='json')),
            ('/dawah/events/{event_id}/publish/', 'post', '409',
             self.client.post(f'/dawah/events/{event.pk}/publish/')),
            ('/dawah/events/{event_id}/resources/', 'get', '200',
             self.client.get(f'/dawah/events/{draft.pk}/resources/')),
            ('/dawah/events/{event_id}/resources/', 'post', '201', created_resource),
            ('/dawah/events/{event_id}/resources/{resource_id}/publish/', 'post', '409',
             self.client.post(f'/dawah/events/{draft.pk}/resources/{created_resource.json()["data"]["id"]}/publish/')),
            ('/dawah/events/{event_id}/resources/{resource_id}/unpublish/', 'post', '200',
             self.client.post(f'/dawah/events/{event.pk}/resources/{resource.pk}/unpublish/')),
        ]
        self.as_user(None)
        self.client.post(f'/dawah/events/{event.pk}/resources/{resource.pk}/publish/')  # anonymous: refused
        resource.status = PUBLISHED
        resource.save()
        cases += [
            ('/dawah/archive/', 'get', '200', self.client.get('/dawah/archive/')),
            ('/dawah/resources/{resource_id}/', 'get', '200', self.client.get(f'/dawah/resources/{resource.pk}/')),
            ('/dawah/resources/{resource_id}/', 'get', '404', self.client.get('/dawah/resources/999/')),
            ('/dawah/events/{event_id}/', 'get', '200', self.client.get(f'/dawah/events/{event.pk}/')),
        ]
        for path, method, status, response in cases:
            with self.subTest(path=path, method=method, status=status):
                self.assertEqual(str(response.status_code), status, response.content)
                content = self.validate(path, method, status, response.json())
                for example in content.get('examples', {}).values():
                    self.validate(path, method, status, example['value'])

    def test_deletes_have_no_body_and_aliases_are_undocumented(self):
        paths = self.schema['paths']
        self.assertNotIn('content', paths['/dawah/events/{event_id}/']['delete']['responses']['204'])
        self.assertNotIn('/dawah/events/{event_id}', paths)
        self.assertNotIn('/dawah/events/{event_id}/resources/{resource_id}', paths)
        archive_params = {p['name'] for p in paths['/dawah/archive/']['get']['parameters']}
        self.assertEqual(archive_params, {'page', 'page_size', 'type', 'search', 'speaker', 'category',
                                          'date_from', 'date_to'})
        # DAWAH-02/07 upload routes are the only upload operations, and are admin protocol routes.
        upload_suffixes = ('/audio/', '/document/', '/featured-image/')
        self.assertEqual(sorted(p for p in paths if 'upload' in p or p.endswith(upload_suffixes)),
                         ['/dawah/events/{event_id}/featured-image/',
                          '/dawah/events/{event_id}/resources/{resource_id}/audio/',
                          '/dawah/events/{event_id}/resources/{resource_id}/document/',
                          '/dawah/uploads/{upload_id}/finalize/'])

    def test_document_and_image_responses_match_schema(self):
        from django.core.cache import cache

        from . import files
        event = make_event(status=PUBLISHED)
        doc = make_resource(event, type='doc')
        self.as_admin()
        pdf, png = files.pdf_bytes(), files.image_bytes('PNG')
        doc_intent = self.client.post(f'/dawah/events/{event.pk}/resources/{doc.pk}/document/',
                                      {'file_name': 'a.pdf', 'content_type': 'application/pdf',
                                       'size_bytes': len(pdf)}, format='json')
        image_intent = self.client.post(f'/dawah/events/{event.pk}/featured-image/',
                                        {'file_name': 'a.png', 'content_type': 'image/png', 'size_bytes': len(png)},
                                        format='json')
        self.storage.client_put(doc_intent.json()['data']['upload_url'], pdf, content_type='application/pdf')
        self.storage.client_put(image_intent.json()['data']['upload_url'], png, content_type='image/png')
        doc_final = self.client.post(f'/dawah/uploads/{doc_intent.json()["data"]["upload_id"]}/finalize/')
        image_final = self.client.post(f'/dawah/uploads/{image_intent.json()["data"]["upload_id"]}/finalize/')
        doc_published = self.client.post(f'/dawah/events/{event.pk}/resources/{doc.pk}/publish/')
        cache.clear()
        cases = [
            ('/dawah/events/{event_id}/resources/{resource_id}/document/', 'post', '201', doc_intent),
            ('/dawah/events/{event_id}/featured-image/', 'post', '201', image_intent),
            ('/dawah/uploads/{upload_id}/finalize/', 'post', '200', doc_final),
            ('/dawah/uploads/{upload_id}/finalize/', 'post', '200', image_final),
            ('/dawah/events/{event_id}/resources/{resource_id}/publish/', 'post', '200', doc_published),
            ('/dawah/events/{event_id}/', 'patch', '200',
             self.client.patch(f'/dawah/events/{event.pk}/', {'featured_image_alt': 'Poster'}, format='json')),
        ]
        self.as_user(None)
        cases += [
            ('/dawah/events/{event_id}/', 'get', '200', self.client.get(f'/dawah/events/{event.pk}/')),
            ('/dawah/resources/{resource_id}/', 'get', '200', self.client.get(f'/dawah/resources/{doc.pk}/')),
            ('/dawah/archive/', 'get', '200', self.client.get('/dawah/archive/', {'type': 'doc'})),
        ]
        for path, method, status, response in cases:
            with self.subTest(path=path, method=method, status=status):
                self.assertEqual(str(response.status_code), status, response.content)
                content = self.validate(path, method, status, response.json())
                for example in content.get('examples', {}).values():
                    self.validate(path, method, status, example['value'])
        self.assertIsNotNone(cases[-3][3].json()['data']['featured_image_url'])
