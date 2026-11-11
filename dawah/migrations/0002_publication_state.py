"""DAWAH-01: publication/storage state, audit fields and parent-owned resources.

Existing-data effect (D2 "Migration / Existing-Data Impact"):

* Every existing event and resource becomes ``status=draft`` (not published),
  with ``published_at`` and ``created_by`` empty. Nothing becomes publicly
  discoverable until an admin reviews and publishes it.
* Existing resources get ``storage_status=none`` (audio/documents) or
  ``not_applicable`` (video). Legacy local ``file`` values are kept untouched
  but never count as a ready asset; import/validation is DAWAH-02/07 work.
* Events gain ``created_at``/``updated_at`` set to the migration time (the true
  creation time was never recorded). Resource timestamps are unchanged.
* No rows, files or columns are removed. ``tags`` becomes optional (state only).

Reverse migration removes only the added columns; existing data is preserved.
"""
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

STATUS_CHOICES = [('draft', 'Draft'), ('published', 'Published'), ('unpublished', 'Unpublished')]


def classify_storage(apps, schema_editor):
    EventResource = apps.get_model('dawah', 'EventResource')
    EventResource.objects.filter(type='vid').update(storage_status='not_applicable')


def unclassify_storage(apps, schema_editor):
    """Nothing to undo: the column is dropped by the reversed AddField."""


class Migration(migrations.Migration):

    dependencies = [
        ('dawah', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='status',
            field=models.CharField(choices=STATUS_CHOICES, default='draft', max_length=12),
        ),
        migrations.AddField(
            model_name='event',
            name='published_at',
            field=models.DateTimeField(blank=True, help_text='Time of the most recent publication.', null=True),
        ),
        migrations.AddField(
            model_name='event',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='event',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='event',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AlterField(
            model_name='event',
            name='tags',
            field=models.CharField(blank=True, default='', max_length=250),
        ),
        migrations.AddField(
            model_name='eventresource',
            name='status',
            field=models.CharField(choices=STATUS_CHOICES, default='draft', max_length=12),
        ),
        migrations.AddField(
            model_name='eventresource',
            name='storage_status',
            field=models.CharField(choices=[('none', 'No asset'), ('ready', 'Ready'),
                                            ('not_applicable', 'Not applicable')],
                                   default='none', max_length=16),
        ),
        migrations.AddField(
            model_name='eventresource',
            name='published_at',
            field=models.DateTimeField(blank=True, help_text='Time of the most recent publication.', null=True),
        ),
        migrations.AddField(
            model_name='eventresource',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='eventresource',
            name='event',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='resources',
                                    to='dawah.event'),
        ),
        migrations.RunPython(classify_storage, unclassify_storage),
        migrations.AddIndex(
            model_name='event',
            index=models.Index(fields=['status', 'start_time'], name='dawah_event_status_start'),
        ),
        migrations.AddIndex(
            model_name='eventresource',
            index=models.Index(fields=['event', 'status'], name='dawah_resource_event_status'),
        ),
    ]
