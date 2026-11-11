"""DAWAH-02: validated R2 asset metadata, UploadIntent and StorageCleanup.

Existing-data effect (D2 "Migration / Existing-Data Impact"):

* New asset columns are empty for every existing resource. Legacy local
  ``file`` values are kept untouched and are never treated as R2 objects.
* Defensive: an audio/document row claiming ``storage_status=ready`` without a
  validated private object (no path before DAWAH-02 could create one) becomes
  ``none``; if it was published it becomes ``unpublished``, because published
  audio requires a persisted ready asset. No rows, files or provider objects
  are deleted.
* Reverse migration drops the added tables/columns; reversing the defensive
  status correction is a no-op.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def demote_assetless_ready(apps, schema_editor):
    EventResource = apps.get_model('dawah', 'EventResource')
    rows = EventResource.objects.filter(type__in=['aud', 'doc'], storage_status='ready', private_object_key='')
    rows.filter(status='published').update(status='unpublished')
    rows.update(storage_status='none')


class Migration(migrations.Migration):

    dependencies = [
        ("dawah", "0002_publication_state"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="eventresource",
            name="content_type",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="eventresource",
            name="etag",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="eventresource",
            name="file_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Sanitized original file name.",
                max_length=120,
            ),
        ),
        migrations.AddField(
            model_name="eventresource",
            name="private_object_key",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Server-only validated private copy.",
                max_length=512,
            ),
        ),
        migrations.AddField(
            model_name="eventresource",
            name="public_media_url",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Canonical public delivery URL of the promoted copy.",
                max_length=1024,
            ),
        ),
        migrations.AddField(
            model_name="eventresource",
            name="public_object_key",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.AddField(
            model_name="eventresource",
            name="size_bytes",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="eventresource",
            name="uploaded_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the current validated asset was attached.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="eventresource",
            name="storage_status",
            field=models.CharField(
                choices=[
                    ("none", "No asset"),
                    ("pending", "Upload pending"),
                    ("ready", "Ready"),
                    ("failed", "Upload failed"),
                    ("not_applicable", "Not applicable"),
                ],
                default="none",
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="StorageCleanup",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "bucket",
                    models.CharField(
                        choices=[("private", "Private"), ("public", "Public")],
                        max_length=8,
                    ),
                ),
                ("key", models.CharField(max_length=512)),
                ("reason", models.CharField(max_length=40)),
                ("resource_id", models.BigIntegerField(blank=True, null=True)),
                ("upload_id", models.BigIntegerField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("done", "Done"),
                            ("skipped", "Skipped (still referenced)"),
                        ],
                        default="pending",
                        max_length=8,
                    ),
                ),
                ("attempts", models.PositiveIntegerField(default=0)),
                (
                    "last_error_code",
                    models.CharField(blank=True, default="", max_length=40),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["status", "id"], name="dawah_cleanup_status")
                ],
            },
        ),
        migrations.CreateModel(
            name="UploadIntent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("audio", "Audio")], default="audio", max_length=10
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("issued", "Issued"),
                            ("validating", "Validating"),
                            ("ready", "Ready"),
                            ("rejected", "Rejected"),
                            ("failed", "Failed"),
                            ("expired", "Expired"),
                        ],
                        default="issued",
                        max_length=12,
                    ),
                ),
                ("staging_key", models.CharField(max_length=512, unique=True)),
                ("validation_key", models.CharField(max_length=512, unique=True)),
                ("file_name", models.CharField(max_length=120)),
                ("content_type", models.CharField(max_length=100)),
                ("size_bytes", models.BigIntegerField()),
                ("expires_at", models.DateTimeField()),
                (
                    "lease_token",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                (
                    "observed_etag",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Staged ETag copied to the validation key.",
                        max_length=128,
                    ),
                ),
                (
                    "validation_etag",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                ("observed_size", models.BigIntegerField(blank=True, null=True)),
                ("duration", models.PositiveIntegerField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, default="", max_length=40)),
                (
                    "error_message",
                    models.CharField(blank=True, default="", max_length=200),
                ),
                ("finalized_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "resource",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="upload_intents",
                        to="dawah.eventresource",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["status", "expires_at"], name="dawah_intent_status_exp"
                    )
                ],
            },
        ),
        migrations.RunPython(demote_assetless_ready, migrations.RunPython.noop),
    ]
