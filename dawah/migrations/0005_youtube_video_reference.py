"""DAWAH-05: validated YouTube video reference.

Existing-data effect: adds an empty ``youtube_video_id`` (and a format check
constraint). Video rows keep every field. The only legacy "link" data is the
old free-form ``file`` value; where a video row's ``file`` holds a supported
YouTube URL it is normalized into ``youtube_video_id`` (the value in ``file``
and the row's status are left untouched). Every other video row stays without
an ID: it keeps its status but is not visible/publishable until an admin saves a
valid link. Reverse drops the column; no other data changes.
"""

from django.conf import settings
from django.db import migrations, models


def normalize_legacy_links(apps, schema_editor):
    from dawah.youtube import InvalidYouTubeUrl, parse_youtube_url

    EventResource = apps.get_model('dawah', 'EventResource')
    for resource in EventResource.objects.filter(type='vid').exclude(file='').exclude(file__isnull=True):
        try:
            video_id = parse_youtube_url(resource.file.name)
        except InvalidYouTubeUrl:
            continue
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
