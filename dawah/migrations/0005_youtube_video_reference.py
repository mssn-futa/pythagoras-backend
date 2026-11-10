"""DAWAH-05: validated YouTube video reference.

Existing-data effect: adds an empty ``youtube_video_id`` (and a format check
constraint). Video rows keep every field. The only legacy "link" data is the
old free-form ``file`` value; where a video row's ``file`` holds a supported
YouTube URL it is normalized into ``youtube_video_id`` (the value in ``file``
and the row's status are left untouched). Every other video row stays without
an ID: it keeps its status but is not visible/publishable until an admin saves a
valid link. A *published* legacy video row whose ``file`` holds a valid link
therefore becomes visible; that is only possible with hand-edited data. The
parser below is a frozen copy (the live ``dawah.youtube`` may change later).
Reverse drops the column; no other data changes.
"""

import re
from urllib.parse import urlsplit

from django.conf import settings
from django.db import migrations, models


_VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}', re.ASCII)
_YOUTUBE_HOSTS = frozenset({'youtube.com', 'www.youtube.com', 'm.youtube.com'})


def _single_segment(path, prefix):
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    return rest[:-1] if rest.endswith('/') else rest


def _parse_youtube_url(value):
    """Frozen copy of the DAWAH-05 parser; returns the video ID or None."""
    value = value.strip()
    if not value or len(value) > 2048 or not value.isascii():
        return None
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch == '\\' for ch in value):
        return None
    if not value.lower().startswith('https://'):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None or '@' in parts.netloc or port is not None:
        return None
    host = parts.hostname or ''
    if parts.netloc.lower() != host:
        return None
    video_id = None
    if host == 'youtu.be':
        video_id = _single_segment(parts.path, '/')
    elif host in _YOUTUBE_HOSTS:
        if parts.path == '/watch':
            ids = [token[2:] for token in parts.query.split('&') if token.startswith('v=')]
            if len(ids) != 1:
                return None
            video_id = ids[0]
        else:
            video_id = _single_segment(parts.path, '/embed/')
            if video_id is None:
                video_id = _single_segment(parts.path, '/shorts/')
    elif host == 'www.youtube-nocookie.com':
        video_id = _single_segment(parts.path, '/embed/')
    if video_id is None or not _VIDEO_ID.fullmatch(video_id) or video_id == 'videoseries':
        return None
    return video_id


def normalize_legacy_links(apps, schema_editor):
    EventResource = apps.get_model('dawah', 'EventResource')
    for resource in EventResource.objects.filter(type='vid').exclude(file='').exclude(file__isnull=True):
        video_id = _parse_youtube_url(resource.file.name)
        if video_id:
            EventResource.objects.filter(pk=resource.pk).update(youtube_video_id=video_id)


class Migration(migrations.Migration):

    dependencies = [
        ("dawah", "0004_review_hardening"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="eventresource",
            name="youtube_video_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Validated YouTube video ID (video resources only).",
                max_length=11,
            ),
        ),
        migrations.RunPython(normalize_legacy_links, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="eventresource",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("youtube_video_id", ""),
                    ("youtube_video_id__regex", "^[A-Za-z0-9_-]{11}$"),
                    _connector="OR",
                ),
                name="dawah_resource_youtube_id_format",
            ),
        ),
    ]
