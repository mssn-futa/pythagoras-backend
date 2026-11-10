from datetime import datetime, timezone
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from dawah import youtube
from dawah.models import EventResource
from dawah.youtube import InvalidYouTubeUrl, parse_youtube_url

from .helpers import DRAFT, PUBLISHED, UNPUBLISHED, DawahAPITestCase, make_event, make_resource

ID = 'dQw4w9WgXcQ'
ID2 = 'a-B_c1D2e3F'


def resources_url(event_id):
    return f'/dawah/events/{event_id}/resources/'


def resource_url(event_id, resource_id):
    return f'/dawah/events/{event_id}/resources/{resource_id}/'


class ParserTests(SimpleTestCase):
    def test_valid_forms(self):
        cases = [
            f'https://www.youtube.com/watch?v={ID}', f'https://youtube.com/watch?v={ID}',
            f'https://m.youtube.com/watch?v={ID}', f'https://music.youtube.com/watch?v={ID}',
            f'https://www.youtube.com/watch?v={ID}&t=42s&si=abc', f'https://www.youtube.com/watch?t=5&v={ID}',
            f'https://www.youtube.com/watch?v={ID}&list=PL123&index=2', f'https://www.youtube.com/watch?v={ID}#t=10',
            f'https://youtu.be/{ID}', f'https://youtu.be/{ID}?t=30&si=xyz', f'https://youtu.be/{ID}/',
            f'https://YOUTU.BE/{ID}', f'HTTPS://WWW.YOUTUBE.COM/watch?v={ID}',
            f'https://www.youtube.com/embed/{ID}', f'https://www.youtube.com/embed/{ID}?start=3',
            f'https://m.youtube.com/embed/{ID}', f'https://www.youtube-nocookie.com/embed/{ID}',
            f'https://www.youtube.com/shorts/{ID}', f'https://m.youtube.com/shorts/{ID}?feature=share',
            f'https://music.youtube.com/shorts/{ID}', f'  https://youtu.be/{ID}  ',
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(parse_youtube_url(url), ID)

    def test_id_alphabet(self):
        self.assertEqual(parse_youtube_url(f'https://youtu.be/{ID2}'), ID2)
        self.assertEqual(parse_youtube_url('https://youtu.be/-----------'), '-----------')
        self.assertEqual(parse_youtube_url('https://youtu.be/___________'), '___________')

    def test_invalid(self):
        cases = [
            '', '   ', 'not a url', ID, f'youtube.com/watch?v={ID}', f'//www.youtube.com/watch?v={ID}',
            # schemes
            f'http://www.youtube.com/watch?v={ID}', f'ftp://youtu.be/{ID}', f'javascript:alert(1)//youtu.be/{ID}',
            'javascript:alert(1)', f'data:text/html,https://youtu.be/{ID}', f'file:///youtu.be/{ID}',
            # hosts
            f'https://evil.com/watch?v={ID}', f'https://youtube.com.evil.com/watch?v={ID}',
            f'https://evil.com/youtube.com/watch?v={ID}', f'https://notyoutube.com/watch?v={ID}',
            f'https://youtube.co/watch?v={ID}', f'https://www.youtube.com.evil.test/watch?v={ID}',
            f'https://youtu.be.evil.com/{ID}', f'https://youtube-nocookie.com.evil.com/embed/{ID}',
            f'https://www.youtube.com./watch?v={ID}', f'https://youtu.be./{ID}',
            f'https://gaming.youtube.com/watch?v={ID}', f'https://studio.youtube.com/watch?v={ID}',
            f'https://www.youtu.be/{ID}', f'https://m.youtu.be/{ID}', f'https://youtube-nocookie.com/embed/{ID}',
            f'https://www.youtube-nocookie.com/watch?v={ID}', f'https://youtube.com@evil.com/watch?v={ID}',
            f'https://evil.com#@www.youtube.com/watch?v={ID}',
            # userinfo, ports
            f'https://user@www.youtube.com/watch?v={ID}', f'https://user:pw@youtu.be/{ID}',
            f'https://@youtu.be/{ID}', f'https://www.youtube.com:443/watch?v={ID}',
            f'https://www.youtube.com:8080/watch?v={ID}', f'https://www.youtube.com:/watch?v={ID}',
            # paths
            'https://www.youtube.com/', 'https://www.youtube.com/watch', 'https://www.youtube.com/watch?v=',
            f'https://www.youtube.com/playlist?list=PLabcdefghij1', 'https://www.youtube.com/watch?list=PLabcdefghij1',
            'https://www.youtube.com/@somechannel', 'https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv',
            'https://www.youtube.com/user/somebody', 'https://www.youtube.com/c/somebody',
            f'https://www.youtube.com/live/{ID}', f'https://www.youtube.com/v/{ID}',
            f'https://www.youtube.com/watch/{ID}', f'https://www.youtube.com/Watch?v={ID}',
            f'https://www.youtube.com/watch/?v={ID}', f'https://www.youtube.com//embed/{ID}',
            f'https://www.youtube.com/embed/{ID}/extra', f'https://www.youtube.com/embed/', f'https://youtu.be/',
            f'https://youtu.be/{ID}/extra', f'https://youtu.be//{ID}', f'https://www.youtube.com/embed/{ID}//',
            f'https://www.youtube.com/shorts/{ID}/x', f'https://www.youtube.com/embed/videoseries?list=PL1',
            f'https://www.youtube.com/embed?v={ID}', f'https://youtu.be/?v={ID}',
            f'https://www.youtube.com/results?search_query={ID}',
            f'https://www.youtube.com/redirect?q=https://youtu.be/{ID}',
            f'https://www.youtube.com/attribution_link?u=/watch?v={ID}',
            # ids
            'https://youtu.be/short', f'https://youtu.be/{ID}x', f'https://youtu.be/{ID[:-1]}',
            'https://www.youtube.com/watch?v=abc', f'https://www.youtube.com/watch?v={ID}x',
            f'https://www.youtube.com/watch?v={ID}&v={ID2}', f'https://www.youtube.com/watch?v={ID}&v={ID}',
            f'https://www.youtube.com/watch?v={ID},{ID2}', f'https://www.youtube.com/watch?v={ID};{ID2}',
            f'https://www.youtube.com/watch?v=%64Qw4w9WgXcQ', 'https://youtu.be/dQw4w9WgXc%51',
            'https://youtu.be/dQw4w9WgXc.', 'https://youtu.be/dQw4w9WgXc!', 'https://youtu.be/dQw4w9WgXc*',
            f'https://www.youtube.com/watch?V={ID}', f'https://www.youtube.com/watch?vv={ID}',
            # unicode, whitespace, control characters
            f'https://youtu.be/{ID[:-1]}é', f'https://youtu.be/{ID[:-1]}١', 'https://youtu.be/dQw4w9WgXcｑ',
            f'https://www.youtube.com/watch?v={ID[:-1]}‮', f'https://yоutu.be/{ID}', f'https://ｙoutu.be/{ID}',
            f'https://www.youtube.com/watch?v={ID}\u200b', f'https://youtu.be/{ID}\u00a0x',
            f'https://youtu.be/ {ID}', f'https://youtu .be/{ID}',
            f'https://youtu.be/{ID}\nx', f'https://youtu.be/{ID}\r\nHost: evil',
            f'https://you\ttu.be/{ID}', f'https://youtu.be/{ID}\x00',
            f'https://youtu.be/{ID}\x7f', f'https://youtu.be/{ID}\x1b', f'https://youtu.be/{ID} https://youtu.be/{ID2}',
            f'https://youtu.be\\{ID}', f'https:\\\\youtu.be\\{ID}', f'https://youtu.be/{ID}\\x',
            # markup
            f'<iframe src="https://www.youtube.com/embed/{ID}"></iframe>',
            f'<iframe width="560" src="https://www.youtube-nocookie.com/embed/{ID}" allowfullscreen></iframe>',
            f'<script>alert(1)</script>https://youtu.be/{ID}', f'"https://youtu.be/{ID}"',
            f'<https://youtu.be/{ID}>', f'[x](https://youtu.be/{ID})',
            f'https://youtu.be/{ID}"><script>', f'https://youtu.be/{ID}<b>',
            # length
            f'https://youtu.be/{ID}?t=' + '1' * 3000,
        ]
        for url in cases:
            with self.subTest(url=url):
                with self.assertRaises(InvalidYouTubeUrl):
                    parse_youtube_url(url)

    def test_non_strings(self):
        for value in (None, 123, b'https://youtu.be/dQw4w9WgXcQ', ['https://youtu.be/dQw4w9WgXcQ'], {}):
            with self.subTest(value=value), self.assertRaises(InvalidYouTubeUrl):
                parse_youtube_url(value)

    def test_playback_constructed_from_id_only(self):
        self.assertEqual(youtube.playback_for(ID), {
            'provider': 'youtube', 'video_id': ID, 'watch_url': f'https://www.youtube.com/watch?v={ID}',
            'embed_url': f'https://www.youtube-nocookie.com/embed/{ID}'})
        for bad in ('', None, 'short', ID + 'x', '<script>aa', 'a b c d e f g'):
            self.assertIsNone(youtube.playback_for(bad))

    def test_parser_makes_no_network_calls(self):
        import socket
        import urllib.request
        with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network')), \
                mock.patch.object(socket, 'getaddrinfo', side_effect=AssertionError('dns')), \
                mock.patch.object(urllib.request, 'urlopen', side_effect=AssertionError('urlopen')):
            self.assertEqual(parse_youtube_url(f'https://youtu.be/{ID}'), ID)


class VideoApiTests(DawahAPITestCase, TestCase):
    def setUp(self):
        super().setUp()
        self.event = make_event()
        self.as_admin()

    def create(self, **payload):
        payload = {'title': 'Lecture', 'type': 'vid', **payload}
        return self.client.post(resources_url(self.event.pk), payload, format='json')

    def test_create_with_link_normalizes_and_persists_id_only(self):
        data = self.data(self.create(youtube_url=f'https://youtu.be/{ID}?si=tracking&t=9'), 201)
        self.assertEqual(data['playback'], {
            'provider': 'youtube', 'video_id': ID, 'watch_url': f'https://www.youtube.com/watch?v={ID}',
            'embed_url': f'https://www.youtube-nocookie.com/embed/{ID}'})
        self.assertEqual((data['type'], data['status'], data['storage_status']), ('vid', 'draft', 'not_applicable'))
        self.assertIsNone(data['media_url'])
        self.assertNotIn('youtube_video_id', data)
        resource = EventResource.objects.get()
        self.assertEqual(resource.youtube_video_id, ID)
        stored = [str(v) for v in EventResource.objects.values().get().values()]
        self.assertFalse(any('tracking' in v or 'si=' in v for v in stored))
        # reload
        got = self.data(self.client.get(resource_url(self.event.pk, resource.pk)))
        self.assertEqual(got['playback'], data['playback'])

    def test_create_without_link_has_null_playback(self):
        data = self.data(self.create(), 201)
        self.assertIsNone(data['playback'])
        self.assertEqual(EventResource.objects.get().youtube_video_id, '')

    def test_invalid_links_are_field_errors_and_nothing_is_created(self):
        for bad in [f'https://evil.com/watch?v={ID}', f'https://youtube.com.evil.com/watch?v={ID}',
                    f'<iframe src="https://www.youtube.com/embed/{ID}"></iframe>', 'javascript:alert(1)',
                    f'http://youtu.be/{ID}', f'https://user@youtu.be/{ID}', 'https://www.youtube.com/playlist?list=PL1',
                    'https://www.youtube.com/@chan', f'https://www.youtube.com/watch?v={ID}&v={ID2}', '', '  ',
                    f'https://youtu.be/{ID}\x00', 'x' * 5000]:
            with self.subTest(bad=bad[:50]):
                body = self.error(self.create(youtube_url=bad), 400, 'VALIDATION_ERROR')
                self.assertEqual(list(body['fields']), ['youtube_url'])
                self.assertIsInstance(body['fields']['youtube_url'][0], str)
        for bad in (None, 5, ['a'], {'a': 1}):
            with self.subTest(bad=bad):
                body = self.error(self.create(youtube_url=bad), 400, 'VALIDATION_ERROR')
                self.assertIn('youtube_url', body['fields'])
        self.assertFalse(EventResource.objects.exists())

    def test_link_on_non_video_is_rejected(self):
        for kind in ('aud', 'doc'):
            body = self.error(self.create(type=kind, youtube_url=f'https://youtu.be/{ID}'), 400, 'VALIDATION_ERROR')
            self.assertIn('youtube_url', body['fields'])
        audio = make_resource(self.event)
        body = self.error(self.client.patch(resource_url(self.event.pk, audio.pk),
                                            {'youtube_url': f'https://youtu.be/{ID}'}, format='json'),
                          400, 'VALIDATION_ERROR')
        self.assertIn('youtube_url', body['fields'])
        self.assertFalse(EventResource.objects.exclude(youtube_video_id='').exists())

    def test_patch_replaces_link_while_not_published(self):
        resource = make_resource(self.event, type='vid', youtube_video_id=ID)
        url = resource_url(self.event.pk, resource.pk)
        data = self.data(self.client.patch(url, {'youtube_url': f'https://www.youtube.com/shorts/{ID2}'},
                                           format='json'))
        self.assertEqual(data['playback']['video_id'], ID2)
        resource.refresh_from_db()
        self.assertEqual(resource.youtube_video_id, ID2)
        # invalid keeps the old value
        self.error(self.client.patch(url, {'youtube_url': 'https://evil.test/x'}, format='json'), 400,
                   'VALIDATION_ERROR')
        resource.refresh_from_db()
        self.assertEqual(resource.youtube_video_id, ID2)
        # title-only PATCH keeps the link
        self.data(self.client.patch(url, {'title': 'Renamed'}, format='json'))
        resource.refresh_from_db()
        self.assertEqual((resource.title, resource.youtube_video_id), ('Renamed', ID2))
        # unpublished resources can be corrected too
        resource.status = UNPUBLISHED
        resource.save()
        self.data(self.client.patch(url, {'youtube_url': f'https://youtu.be/{ID}'}, format='json'))

    def test_patch_published_link_requires_unpublish(self):
        event = make_event(status=PUBLISHED)
        resource = make_resource(event, status=PUBLISHED, type='vid', youtube_video_id=ID)
        url = resource_url(event.pk, resource.pk)
        for link in (f'https://youtu.be/{ID2}', f'https://youtu.be/{ID}'):
            self.error(self.client.patch(url, {'youtube_url': link}, format='json'), 409, 'UNPUBLISH_REQUIRED')
        resource.refresh_from_db()
        self.assertEqual(resource.youtube_video_id, ID)
        # other metadata stays editable; unpublish then replace works
        self.data(self.client.patch(url, {'title': 'Fixed typo'}, format='json'))
        self.data(self.client.post(url + 'unpublish/'))
        self.data(self.client.patch(url, {'youtube_url': f'https://youtu.be/{ID2}'}, format='json'))
        resource.refresh_from_db()
        self.assertEqual(resource.youtube_video_id, ID2)

    def test_type_change_rules(self):
        resource = make_resource(self.event, type='vid', youtube_video_id=ID)
        url = resource_url(self.event.pk, resource.pk)
        data = self.data(self.client.patch(url, {'type': 'aud'}, format='json'))
        self.assertEqual((data['type'], data['storage_status'], data['playback']),
                         ('aud', 'none', None))
        resource.refresh_from_db()
        self.assertEqual(resource.youtube_video_id, '')
        data = self.data(self.client.patch(url, {'type': 'vid', 'youtube_url': f'https://youtu.be/{ID2}'},
                                           format='json'))
        self.assertEqual((data['type'], data['playback']['video_id']), ('vid', ID2))
        self.error(self.client.patch(url, {'type': 'doc', 'youtube_url': f'https://youtu.be/{ID2}'}, format='json'),
                   400, 'VALIDATION_ERROR')

    def test_publish_requires_valid_id(self):
        event = make_event(status=PUBLISHED)
        without = make_resource(event, type='vid')
        url = resource_url(event.pk, without.pk)
        self.error(self.client.post(url + 'publish/'), 409, 'MEDIA_NOT_READY')
        self.data(self.client.patch(url, {'youtube_url': f'https://youtu.be/{ID}'}, format='json'))
        data = self.data(self.client.post(url + 'publish/'))
        self.assertEqual((data['status'], data['playback']['video_id']), ('published', ID))
        self.assertIsNone(data['media_url'])
        self.assertEqual(data['storage_status'], 'not_applicable')
        self.assertIsNone(data['preview_url'])
        self.assertIsNone(data['content_type'])

    def test_no_provider_or_network_use_by_api(self):
        import socket
        import urllib.request
        with mock.patch.object(socket, 'getaddrinfo', side_effect=AssertionError('dns')), \
                mock.patch.object(urllib.request, 'urlopen', side_effect=AssertionError('urlopen')):
            event = make_event(status=PUBLISHED)
            data = self.data(self.client.post(resources_url(event.pk), {
                'title': 'v', 'type': 'vid', 'youtube_url': f'https://youtu.be/{ID}'}, format='json'), 201)
            self.data(self.client.post(f'/dawah/events/{event.pk}/resources/{data["id"]}/publish/'))
            self.data(self.client.post(f'/dawah/events/{event.pk}/resources/{data["id"]}/unpublish/'))

    def test_audio_and_document_regression(self):
        audio = self.data(self.create(type='aud', duration=60), 201)
        doc = self.data(self.create(type='doc'), 201)
        for data in (audio, doc):
            self.assertIsNone(data['playback'])
            self.assertEqual(data['storage_status'], 'none')
        event = make_event(status=PUBLISHED)
        ready = make_resource(event, status=PUBLISHED, ready=True)
        data = self.data(self.client.get(resource_url(event.pk, ready.pk)))
        self.assertIsNone(data['playback'])
        self.assertTrue(data['media_url'])
        unready = make_resource(event, type='aud')
        self.error(self.client.post(resource_url(event.pk, unready.pk) + 'publish/'), 409, 'MEDIA_NOT_READY')

    def test_permissions(self):
        resource = make_resource(self.event, type='vid')
        url = resource_url(self.event.pk, resource.pk)
        for user in (None, self.student, self.staff_student, self.inactive_admin):
            with self.subTest(user=user):
                self.as_user(user)
                payload = {'youtube_url': f'https://youtu.be/{ID}'}
                self.assertIn(self.client.patch(url, payload, format='json').status_code, (401, 403))
                self.assertIn(self.client.post(resources_url(self.event.pk), {
                    'title': 'x', 'type': 'vid', **payload}, format='json').status_code, (401, 403))
        resource.refresh_from_db()
        self.assertEqual(resource.youtube_video_id, '')

    def test_parent_association_is_url_event(self):
        other = make_event(title='Other')
        data = self.data(self.client.post(resources_url(other.pk), {
            'title': 'v', 'type': 'vid', 'youtube_url': f'https://youtu.be/{ID}'}, format='json'), 201)
        self.assertEqual(EventResource.objects.get(pk=data['id']).event, other)
        self.error(self.client.patch(resource_url(self.event.pk, data['id']), {'youtube_url': f'https://youtu.be/{ID2}'},
                                     format='json'), 404, 'NOT_FOUND')

    def test_delete_and_archive_type_filter(self):
        event = make_event(status=PUBLISHED)
        video = make_resource(event, status=PUBLISHED, type='vid', youtube_video_id=ID, title='Vid')
        make_resource(event, status=PUBLISHED, ready=True, title='Aud')
        hidden = make_resource(event, status=PUBLISHED, type='vid', title='No link')
        self.as_user(None)
        items = self.data(self.client.get('/dawah/archive/?type=vid'))
        ids = [r['id'] for r in (items['results'] if isinstance(items, dict) else items)]
        self.assertEqual(ids, [video.pk])
        self.assertNotIn(hidden.pk, ids)
        self.as_admin()
        self.error(self.client.delete(resource_url(event.pk, video.pk)), 409, 'UNPUBLISH_REQUIRED')
        self.data(self.client.post(resource_url(event.pk, video.pk) + 'unpublish/'))
        self.assertEqual(self.client.delete(resource_url(event.pk, video.pk)).status_code, 204)


class VideoVisibilityTests(DawahAPITestCase, TestCase):
    def test_visibility_rules(self):
        live = make_event(status=PUBLISHED)
        draft_event = make_event(status=DRAFT, title='Draft')
        visible = make_resource(live, status=PUBLISHED, type='vid', youtube_video_id=ID)
        no_id = make_resource(live, status=PUBLISHED, type='vid')
        draft_res = make_resource(live, status=DRAFT, type='vid', youtube_video_id=ID)
        unpublished = make_resource(live, status=UNPUBLISHED, type='vid', youtube_video_id=ID)
        parent_hidden = make_resource(draft_event, status=PUBLISHED, type='vid', youtube_video_id=ID)
        wrong_type = make_resource(live, status=PUBLISHED, type='aud', youtube_video_id=ID)
        self.assertEqual(set(EventResource.objects.visible()), {visible})
        self.assertTrue(visible.media_ready())
        self.assertFalse(no_id.media_ready())
        self.assertFalse(wrong_type.media_ready())
        self.assertIsNone(wrong_type.playback)
        # public reads
        for resource in (no_id, draft_res, unpublished, parent_hidden, wrong_type):
            self.assertEqual(self.client.get(f'/dawah/resources/{resource.pk}/').status_code, 404)
        body = self.client.get(f'/dawah/resources/{visible.pk}/').json()['data']
        self.assertEqual(body['playback']['video_id'], ID)
        self.assertIsNone(body['preview_url'])

    def test_unpublishing_parent_hides_video(self):
        event = make_event(status=PUBLISHED)
        video = make_resource(event, status=PUBLISHED, type='vid', youtube_video_id=ID)
        self.as_admin()
        self.data(self.client.post(f'/dawah/events/{event.pk}/unpublish/'))
        self.as_user(None)
        self.assertEqual(self.client.get(f'/dawah/resources/{video.pk}/').status_code, 404)
        self.assertFalse(EventResource.objects.visible().exists())

    def test_db_rejects_malformed_ids(self):
        event = make_event()
        for bad in ('short', '<iframe>aaaa', 'a' * 10 + ' ', 'é' * 11):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                EventResource.objects.create(event=event, title='x', type='vid', youtube_video_id=bad[:11])


class YouTubeMigrationTests(TransactionTestCase):
    BEFORE = [('dawah', '0004_review_hardening')]
    AFTER = [('dawah', '0005_youtube_video_reference')]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_video_rows(self):
        old = self.migrate(self.BEFORE)
        Event = old.get_model('dawah', 'Event')
        Resource = old.get_model('dawah', 'EventResource')
        start = datetime(2026, 1, 9, 12, tzinfo=timezone.utc)
        event = Event.objects.create(title='L', category='c', speakers='s', location='l', start_time=start,
                                     end_time=start, status='published')
        link = Resource.objects.create(event=event, title='Link', type='vid', status='published',
                                       storage_status='not_applicable', file=f'https://youtu.be/{ID}?t=4')
        mp4 = Resource.objects.create(event=event, title='Mp4', type='vid', status='published',
                                      storage_status='not_applicable', file='event_resources/old.mp4')
        bad = Resource.objects.create(event=event, title='Bad', type='vid', file='https://evil.test/watch?v=' + ID)
        empty = Resource.objects.create(event=event, title='Empty', type='vid')
        audio = Resource.objects.create(event=event, title='Aud', type='aud', file=f'https://youtu.be/{ID}')

        new = self.migrate(self.AFTER)
        rows = {r.pk: r for r in new.get_model('dawah', 'EventResource').objects.all()}
        self.assertEqual(rows[link.pk].youtube_video_id, ID)
        self.assertEqual(rows[link.pk].file.name, f'https://youtu.be/{ID}?t=4')
        for other in (mp4, bad, empty, audio):
            self.assertEqual(rows[other.pk].youtube_video_id, '')
        self.assertEqual(rows[mp4.pk].file.name, 'event_resources/old.mp4')
        self.assertEqual((rows[mp4.pk].status, rows[bad.pk].status), ('published', 'draft'))
        # legacy rows without an ID are never visible
        from dawah.models import EventResource
        self.assertEqual(set(EventResource.objects.visible().values_list('pk', flat=True)), {link.pk})

        back = self.migrate(self.BEFORE)
        self.assertEqual(back.get_model('dawah', 'EventResource').objects.count(), 5)
