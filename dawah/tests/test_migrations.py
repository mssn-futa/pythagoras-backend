from datetime import datetime, timezone

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [('dawah', '0001_initial')]
AFTER = [('dawah', '0002_publication_state')]
UTC = timezone.utc


class PublicationStateMigrationTests(TransactionTestCase):
    """Existing rows survive the upgrade and are never made public."""

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_rows_become_drafts_and_keep_their_data(self):
        old = self.migrate(BEFORE)
        Event = old.get_model('dawah', 'Event')
        EventResource = old.get_model('dawah', 'EventResource')
        start = datetime(2026, 1, 9, 12, tzinfo=UTC)
        event = Event.objects.create(title='Legacy', category='jumuah', tags='t', speakers='s', location='l',
                                     description='d', summary='sum', start_time=start, end_time=start,
                                     featured_image='events/old.png')
        audio = EventResource.objects.create(event=event, title='Audio', type='aud', duration=60,
                                             file='event_resources/old.mp3')
        video = EventResource.objects.create(event=event, title='Video', type='vid', file='event_resources/old.mp4')
        doc = EventResource.objects.create(event=event, title='Doc', type='doc')
        audio_created = EventResource.objects.get(pk=audio.pk).created_at

        new = self.migrate(AFTER)
        Event = new.get_model('dawah', 'Event')
        EventResource = new.get_model('dawah', 'EventResource')
        migrated = Event.objects.get(pk=event.pk)
        self.assertEqual((migrated.status, migrated.published_at, migrated.created_by_id), ('draft', None, None))
        self.assertEqual((migrated.title, migrated.summary, migrated.featured_image.name, migrated.end_time),
                         ('Legacy', 'sum', 'events/old.png', start))
        self.assertIsNotNone(migrated.created_at)
        self.assertIsNotNone(migrated.updated_at)
        rows = {r.pk: r for r in EventResource.objects.all()}
        self.assertEqual(set(rows), {audio.pk, video.pk, doc.pk})
        self.assertEqual({r.status for r in rows.values()}, {'draft'})
        self.assertEqual(rows[audio.pk].storage_status, 'none')
        self.assertEqual(rows[doc.pk].storage_status, 'none')
        self.assertEqual(rows[video.pk].storage_status, 'not_applicable')
        self.assertEqual(rows[audio.pk].file.name, 'event_resources/old.mp3')
        self.assertEqual(rows[video.pk].file.name, 'event_resources/old.mp4')
        self.assertEqual(rows[audio.pk].created_at, audio_created)
        self.assertEqual(rows[audio.pk].duration, 60)
        self.assertEqual(Event.objects.filter(status='published').count()
                         + EventResource.objects.filter(status='published').count(), 0)

    def test_reverse_migration_preserves_rows(self):
        new = self.migrate(AFTER)
        Event = new.get_model('dawah', 'Event')
        start = datetime(2026, 1, 9, 12, tzinfo=UTC)
        event = Event.objects.create(title='Kept', category='c', speakers='s', location='l', start_time=start,
                                     end_time=start, status='published')
        new.get_model('dawah', 'EventResource').objects.create(event=event, title='r', type='vid')
        old = self.migrate(BEFORE)
        self.assertEqual(old.get_model('dawah', 'Event').objects.get(pk=event.pk).title, 'Kept')
        self.assertEqual(old.get_model('dawah', 'EventResource').objects.count(), 1)


class DurableAudioMigrationTests(TransactionTestCase):
    """0003 adds asset metadata without publishing or claiming legacy files."""

    migrate = PublicationStateMigrationTests.migrate

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_rows_keep_data_and_assetless_ready_rows_are_demoted(self):
        old = self.migrate(AFTER)
        Event = old.get_model('dawah', 'Event')
        EventResource = old.get_model('dawah', 'EventResource')
        start = datetime(2026, 1, 9, 12, tzinfo=UTC)
        event = Event.objects.create(title='Legacy', category='c', speakers='s', location='l', start_time=start,
                                     end_time=start, status='published')
        legacy = EventResource.objects.create(event=event, title='Legacy file', type='aud', duration=60,
                                              file='event_resources/old.mp3')
        claimed = EventResource.objects.create(event=event, title='Claimed', type='aud', status='published',
                                               storage_status='ready')
        video = EventResource.objects.create(event=event, title='Video', type='vid', storage_status='not_applicable')

        new = self.migrate([('dawah', '0003_durable_audio_upload')])
        EventResource = new.get_model('dawah', 'EventResource')
        rows = {r.pk: r for r in EventResource.objects.all()}
        self.assertEqual((rows[legacy.pk].file.name, rows[legacy.pk].storage_status, rows[legacy.pk].status,
                          rows[legacy.pk].private_object_key, rows[legacy.pk].public_media_url),
                         ('event_resources/old.mp3', 'none', 'draft', '', ''))
        self.assertEqual((rows[claimed.pk].storage_status, rows[claimed.pk].status), ('none', 'unpublished'))
        self.assertEqual(rows[video.pk].storage_status, 'not_applicable')
        self.assertEqual(new.get_model('dawah', 'UploadIntent').objects.count(), 0)

        back = self.migrate(AFTER)
        self.assertEqual(back.get_model('dawah', 'EventResource').objects.count(), 3)
