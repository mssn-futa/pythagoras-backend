"""OpenAPI example payloads (``data`` only; the schema adds the envelope and,
for list routes, wraps a single item in the paginated page)."""

EVENT_CREATE = {
    'title': 'Friday khutbah', 'category': 'jumuah', 'tags': 'patience', 'speakers': 'Guest speaker',
    'location': 'FUTA mosque', 'start_time': '2026-10-09T12:00:00Z', 'end_time': '2026-10-09T13:00:00Z',
    'summary': 'Key takeaways', 'description': 'Friday session',
}

EVENT_DRAFT = {
    'id': 3, **EVENT_CREATE, 'status': 'draft', 'published_at': None, 'featured_image_url': None,
    'created_at': '2026-10-01T09:00:00Z', 'updated_at': '2026-10-01T09:00:00Z', 'created_by': 1,
}

EVENT_PUBLISHED = {
    **EVENT_DRAFT, 'status': 'published', 'published_at': '2026-10-02T08:00:00Z',
    'updated_at': '2026-10-02T08:00:00Z', 'created_by': None,
}

RESOURCE_CREATE = {'title': 'Patience', 'type': 'aud', 'duration': 2100}

EVENT_SUMMARY = {
    'id': 3, 'title': 'Friday khutbah', 'speakers': 'Guest speaker', 'category': 'jumuah',
    'start_time': '2026-10-09T12:00:00Z', 'summary': 'Key takeaways',
}

RESOURCE_NESTED = {
    'id': 8, 'event_id': 3, 'title': 'Patience', 'type': 'aud', 'status': 'published',
    'storage_status': 'ready', 'duration': 2100,
    'media_url': 'https://media.example.test/audio/Jq3-x8Yt0aPq4sVb1cZr6wKe9LmNoPq2/khutbah.mp3',
    'playback': None, 'content_type': 'audio/mpeg', 'size_bytes': 12000000,
    'uploaded_at': '2026-10-09T13:50:00Z', 'preview_url': None,
    'published_at': '2026-10-09T14:00:00Z', 'created_at': '2026-10-01T09:05:00Z',
    'updated_at': '2026-10-09T14:00:00Z', 'created_by': None, 'legacy_file_present': None,
}

RESOURCE_PUBLISHED = {**RESOURCE_NESTED, 'event': EVENT_SUMMARY}

RESOURCE_DRAFT = {
    **RESOURCE_NESTED, 'status': 'draft', 'storage_status': 'none', 'media_url': None, 'published_at': None,
    'content_type': None, 'size_bytes': None, 'uploaded_at': None,
    'updated_at': '2026-10-01T09:05:00Z', 'created_by': 1, 'legacy_file_present': False,
    'event': EVENT_SUMMARY,
}

EVENT_DETAIL = {**EVENT_PUBLISHED, 'resources': [RESOURCE_NESTED]}

UPLOAD_INTENT_REQUEST = {'file_name': 'khutbah.mp3', 'content_type': 'audio/mpeg', 'size_bytes': 12000000}

UPLOAD_INTENT_RESPONSE = {
    'upload_id': 12, 'resource_id': 8, 'method': 'PUT',
    'upload_url': '<short-lived signed S3 PUT URL on <account>.r2.cloudflarestorage.com>',
    'headers': {'Content-Type': 'audio/mpeg'}, 'expires_at': '2026-10-09T12:15:00Z',
    'max_size_bytes': 104857600,
}

FINALIZE_READY = {
    'upload': {'id': 12, 'resource_id': 8, 'status': 'ready', 'error_code': None, 'message': None,
               'expires_at': '2026-10-09T12:15:00Z', 'finalized_at': '2026-10-09T12:03:10Z'},
    'resource': {**RESOURCE_DRAFT, 'storage_status': 'ready', 'content_type': 'audio/mpeg', 'size_bytes': 12000000,
                 'uploaded_at': '2026-10-09T12:03:10Z',
                 'preview_url': '<short-lived signed S3 GET URL (admin only)>'},
    'preview_url': '<short-lived signed S3 GET URL (admin only)>',
}
