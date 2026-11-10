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
    'storage_status': 'ready', 'duration': 2100, 'media_url': 'https://media.example.test/khutbah.mp3',
    'playback': None, 'published_at': '2026-10-09T14:00:00Z', 'created_at': '2026-10-01T09:05:00Z',
    'updated_at': '2026-10-09T14:00:00Z', 'created_by': None, 'legacy_file_present': None,
}

RESOURCE_PUBLISHED = {**RESOURCE_NESTED, 'event': EVENT_SUMMARY}

RESOURCE_DRAFT = {
    **RESOURCE_NESTED, 'status': 'draft', 'storage_status': 'none', 'media_url': None, 'published_at': None,
    'updated_at': '2026-10-01T09:05:00Z', 'created_by': 1, 'legacy_file_present': False,
    'event': EVENT_SUMMARY,
}

EVENT_DETAIL = {**EVENT_PUBLISHED, 'resources': [RESOURCE_NESTED]}
