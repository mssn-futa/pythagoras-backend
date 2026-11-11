"""OpenAPI example payloads (``data`` only; the schema adds the envelope and,
for list routes, wraps a single item in the paginated page)."""

EVENT_CREATE = {
    'title': 'Friday khutbah', 'category': 'jumuah', 'tags': 'patience', 'speakers': 'Guest speaker',
    'location': 'FUTA mosque', 'start_time': '2026-10-09T12:00:00Z', 'end_time': '2026-10-09T13:00:00Z',
    'summary': 'Key takeaways', 'description': 'Friday session',
}

EVENT_DRAFT = {
    'id': 3, **EVENT_CREATE, 'status': 'draft', 'published_at': None, 'featured_image_url': None,
    'featured_image_preview_url': None, 'featured_image_storage_status': 'none',
    'featured_image_uploaded_at': None, 'featured_image_alt': '',
    'created_at': '2026-10-01T09:00:00Z', 'updated_at': '2026-10-01T09:00:00Z', 'created_by': 1,
}

FEATURED_IMAGE_URL = 'https://media.example.test/images/Zr6wKe9LmNoPq2Jq3-x8Yt0aPq4sVb1c/friday.jpg'

EVENT_PUBLISHED = {
    **EVENT_DRAFT, 'status': 'published', 'published_at': '2026-10-02T08:00:00Z',
    'updated_at': '2026-10-02T08:00:00Z', 'created_by': None,
    'featured_image_url': FEATURED_IMAGE_URL, 'featured_image_storage_status': 'ready',
    'featured_image_uploaded_at': '2026-10-01T09:30:00Z', 'featured_image_alt': 'Speaker at the FUTA mosque',
}

RESOURCE_CREATE = {'title': 'Patience', 'type': 'aud', 'duration': 2100}

EVENT_SUMMARY = {
    'id': 3, 'title': 'Friday khutbah', 'speakers': 'Guest speaker', 'category': 'jumuah',
    'start_time': '2026-10-09T12:00:00Z', 'summary': 'Key takeaways', 'featured_image_url': FEATURED_IMAGE_URL,
}

RESOURCE_NESTED = {
    'id': 8, 'event_id': 3, 'title': 'Patience', 'type': 'aud', 'status': 'published',
    'storage_status': 'ready', 'duration': 2100,
    'media_url': 'https://media.example.test/audio/Jq3-x8Yt0aPq4sVb1cZr6wKe9LmNoPq2/khutbah.mp3',
    'playback': None, 'content_type': 'audio/mpeg', 'size_bytes': 12000000, 'file_name': 'khutbah.mp3',
    'uploaded_at': '2026-10-09T13:50:00Z', 'preview_url': None,
    'published_at': '2026-10-09T14:00:00Z', 'created_at': '2026-10-01T09:05:00Z',
    'updated_at': '2026-10-09T14:00:00Z', 'created_by': None, 'legacy_file_present': None,
}

RESOURCE_PUBLISHED = {**RESOURCE_NESTED, 'event': EVENT_SUMMARY}

RESOURCE_DRAFT = {
    **RESOURCE_NESTED, 'status': 'draft', 'storage_status': 'none', 'media_url': None, 'published_at': None,
    'content_type': None, 'size_bytes': None, 'file_name': None, 'uploaded_at': None,
    'updated_at': '2026-10-01T09:05:00Z', 'created_by': 1, 'legacy_file_present': False,
    'event': {**EVENT_SUMMARY, 'featured_image_url': None},
}

EVENT_DETAIL = {**EVENT_PUBLISHED, 'resources': [RESOURCE_NESTED]}

UPLOAD_INTENT_REQUEST = {'file_name': 'khutbah.mp3', 'content_type': 'audio/mpeg', 'size_bytes': 12000000}

UPLOAD_INTENT_RESPONSE = {
    'upload_id': 12, 'resource_id': 8, 'event_id': 3, 'method': 'PUT',
    'upload_url': '<short-lived signed S3 PUT URL on <account>.r2.cloudflarestorage.com>',
    'headers': {'Content-Type': 'audio/mpeg'}, 'expires_at': '2026-10-09T12:15:00Z',
    'max_size_bytes': 104857600,
}

PREVIEW = '<short-lived signed S3 GET URL (admin only)>'

FINALIZE_READY = {
    'upload': {'id': 12, 'kind': 'audio', 'resource_id': 8, 'event_id': 3, 'status': 'ready', 'error_code': None,
               'message': None, 'expires_at': '2026-10-09T12:15:00Z', 'finalized_at': '2026-10-09T12:03:10Z'},
    'resource': {**RESOURCE_DRAFT, 'storage_status': 'ready', 'content_type': 'audio/mpeg', 'size_bytes': 12000000,
                 'file_name': 'khutbah.mp3', 'uploaded_at': '2026-10-09T12:03:10Z', 'preview_url': PREVIEW},
    'event': None,
    'preview_url': PREVIEW,
}

DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

DOCUMENT_INTENT_REQUEST = {'file_name': 'agenda.pdf', 'content_type': 'application/pdf', 'size_bytes': 245760}

DOCUMENT_INTENT_RESPONSE = {
    'upload_id': 13, 'resource_id': 9, 'event_id': 3, 'method': 'PUT',
    'upload_url': '<short-lived signed S3 PUT URL on <account>.r2.cloudflarestorage.com>',
    'headers': {'Content-Type': 'application/pdf'}, 'expires_at': '2026-10-09T12:15:00Z',
    'max_size_bytes': 20971520,
}

IMAGE_INTENT_REQUEST = {'file_name': 'friday.jpg', 'content_type': 'image/jpeg', 'size_bytes': 524288}

IMAGE_INTENT_RESPONSE = {
    'upload_id': 14, 'resource_id': None, 'event_id': 3, 'method': 'PUT',
    'upload_url': '<short-lived signed S3 PUT URL on <account>.r2.cloudflarestorage.com>',
    'headers': {'Content-Type': 'image/jpeg'}, 'expires_at': '2026-10-09T12:15:00Z',
    'max_size_bytes': 10485760,
}

FINALIZE_IMAGE_READY = {
    'upload': {'id': 14, 'kind': 'image', 'resource_id': None, 'event_id': 3, 'status': 'ready',
               'error_code': None, 'message': None, 'expires_at': '2026-10-09T12:15:00Z',
               'finalized_at': '2026-10-09T12:03:10Z'},
    'resource': None,
    'event': {**EVENT_PUBLISHED, 'featured_image_preview_url': PREVIEW,
              'featured_image_uploaded_at': '2026-10-09T12:03:10Z', 'created_by': 1},
    'preview_url': PREVIEW,
}

RESOURCE_DOCUMENT = {
    **RESOURCE_NESTED, 'id': 9, 'title': 'Agenda', 'type': 'doc', 'duration': None,
    'media_url': 'https://media.example.test/documents/b1cZr6wKe9LmNoPq2Jq3-x8Yt0aPq4sV/agenda.pdf',
    'content_type': 'application/pdf', 'size_bytes': 245760, 'file_name': 'agenda.pdf', 'event': EVENT_SUMMARY,
}
