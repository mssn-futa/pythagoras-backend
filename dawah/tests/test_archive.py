from datetime import timedelta

from django.test import TestCase

from .helpers import PUBLISHED, UNPUBLISHED, DawahAPITestCase, at, make_event, make_resource, make_visible_resource

ARCHIVE = '/dawah/archive/'


class ArchiveTests(DawahAPITestCase, TestCase):
    def test_only_visible_resources_of_published_events(self):
        published = make_event(status=PUBLISHED)
        audio = make_visible_resource(published, type='aud')
        doc = make_visible_resource(published, type='doc')
        make_resource(published)  # draft
        make_resource(published, status=PUBLISHED)  # legacy file-only/no asset
        make_resource(published, status=UNPUBLISHED, ready=True)
        make_resource(published, status=PUBLISHED, type='vid')  # no validated reference yet
        make_visible_resource(make_event(), type='aud')  # draft parent
        make_visible_resource(make_event(status=UNPUBLISHED), type='aud')  # hidden parent
        for user in (None, self.student, self.admin):
            self.as_user(user)
            with self.subTest(user=getattr(user, 'email', 'anonymous')):
                self.assertEqual(self.ids(self.client.get(ARCHIVE)), [doc.pk, audio.pk])

    def test_order_by_event_start_desc_then_resource_id_desc(self):
        older = make_event(status=PUBLISHED, start=at(2026, 1, 2))
        newer = make_event(status=PUBLISHED, start=at(2026, 2, 6))
        o1, o2 = make_visible_resource(older), make_visible_resource(older)
        n1, n2 = make_visible_resource(newer), make_visible_resource(newer)
        self.assertEqual(self.ids(self.client.get(ARCHIVE)), [n2.pk, n1.pk, o2.pk, o1.pk])
        page = self.data(self.client.get(ARCHIVE, {'page_size': 3, 'page': 2}))
        self.assertEqual((page['count'], [r['id'] for r in page['results']]), (4, [o1.pk]))
        self.assertIsNone(page['next'])
        self.assertIsNotNone(page['previous'])

    def test_type_filter(self):
        event = make_event(status=PUBLISHED)
        audio = make_visible_resource(event, type='aud')
        doc = make_visible_resource(event, type='doc')
        self.assertEqual(self.ids(self.client.get(ARCHIVE, {'type': 'aud'})), [audio.pk])
        self.assertEqual(self.ids(self.client.get(ARCHIVE, {'type': 'doc'})), [doc.pk])
        self.assertEqual(self.ids(self.client.get(ARCHIVE, {'type': 'vid'})), [])
        fields = self.error(self.client.get(ARCHIVE, {'type': 'mp3'}), 400, 'VALIDATION_ERROR')['fields']
        self.assertIn('type', fields)

    def test_search_speaker_category(self):
        patience = make_event(status=PUBLISHED, title='Friday khutbah', summary='On patience', speakers='Imam Bello',
                              category='jumuah', start=at(2026, 3, 6))
        night = make_event(status=PUBLISHED, title='Dawah night', tags='Family', speakers='Dr. Musa',
                           category='dawah_night', start=at(2026, 3, 7))
        p = make_visible_resource(patience, title='Sabr')
        n = make_visible_resource(night, title='Raising children')
        self.assertEqual(self.ids(self.client.get(ARCHIVE, {'search': 'PATIENCE'})), [p.pk])
        self.assertEqual(self.ids(self.client.get(ARCHIVE, {'search': 'family'})), [n.pk])
        self.assertEqual(self.ids(self.client.get(ARCHIVE, {'search': 'children'})), [n.pk])  # resource title
        self.assertEqual(self.ids(self.client.get(ARCHIVE, {'search': 'nothing-matches'})), [])
        self.assertEqual(self.ids(self.client.get(ARCHIVE, {'speaker': 'imam BELLO'})), [p.pk])
        self.assertEqual(self.ids(self.client.get(ARCHIVE, {'speaker': 'musa'})), [n.pk])
        self.assertEqual(self.ids(self.client.get(ARCHIVE, {'category': 'Dawah_Night'})), [n.pk])
        self.assertEqual(self.ids(self.client.get(ARCHIVE, {'category': 'dawah'})), [])  # exact, not substring
        both = self.data(self.client.get(ARCHIVE, {'speaker': 'bello', 'category': 'dawah_night'}))
        self.assertEqual(both['count'], 0)

    def test_date_boundaries_use_lagos_calendar_days(self):
        # Lagos is UTC+1: 2026-03-05T23:30Z is 2026-03-06 00:30 in Lagos.
        late_utc = make_visible_resource(make_event(status=PUBLISHED, start=at(2026, 3, 5, 23, 30)))
        # 2026-03-05T22:59Z is 23:59 on 2026-03-05 in Lagos.
        before = make_visible_resource(make_event(status=PUBLISHED, start=at(2026, 3, 5, 22, 59)))
        # 2026-03-06T22:59Z is 23:59 on 2026-03-06 in Lagos (still that day).
        end_of_day = make_visible_resource(make_event(status=PUBLISHED, start=at(2026, 3, 6, 22, 59)))
        # 2026-03-06T23:00Z is the next Lagos day.
        next_day = make_visible_resource(make_event(status=PUBLISHED, start=at(2026, 3, 6, 23, 0)))
        day = {'date_from': '2026-03-06', 'date_to': '2026-03-06'}
        self.assertEqual(sorted(self.ids(self.client.get(ARCHIVE, day))), sorted([late_utc.pk, end_of_day.pk]))
        self.assertEqual(sorted(self.ids(self.client.get(ARCHIVE, {'date_from': '2026-03-06'}))),
                         sorted([late_utc.pk, end_of_day.pk, next_day.pk]))
        self.assertEqual(sorted(self.ids(self.client.get(ARCHIVE, {'date_to': '2026-03-05'}))), [before.pk])

    def test_invalid_date_filters(self):
        for params, field in [({'date_from': '06-03-2026'}, 'date_from'), ({'date_to': '2026-02-30'}, 'date_to'),
                              ({'date_from': '2026-03-06T00:00:00Z'}, 'date_from'),
                              ({'date_from': '2026-03-07', 'date_to': '2026-03-06'}, 'date_to'),
                              ({'date_from': '2026-W01-1'}, 'date_from'),  # ISO week date
                              ({'date_to': '20260306'}, 'date_to'),
                              ({'date_to': '9999-12-31'}, 'date_to'),  # would overflow
                              ({'date_from': '0001-01-01'}, 'date_from'),
                              ({'date_from': '0001-01-01', 'date_to': '9999-12-31'}, 'date_from')]:
            with self.subTest(params=params):
                fields = self.error(self.client.get(ARCHIVE, params), 400, 'VALIDATION_ERROR')['fields']
                self.assertIn(field, fields)

    def test_out_of_range_dates_rejected_on_events_too(self):
        for params in ({'date_to': '9999-12-31'}, {'date_from': '0001-01-01'}):
            with self.subTest(params=params):
                self.error(self.client.get('/dawah/events/', params), 400, 'VALIDATION_ERROR')
        self.data(self.client.get(ARCHIVE, {'date_from': '1900-01-01', 'date_to': '9998-12-31'}))

    def test_null_characters_rejected(self):
        for url in (ARCHIVE, '/dawah/events/'):
            for name in ('search', 'speaker', 'category'):
                with self.subTest(url=url, name=name):
                    fields = self.error(self.client.get(url, {name: 'a\x00b'}), 400, 'VALIDATION_ERROR')['fields']
                    self.assertEqual(fields, {name: ['Null characters are not allowed.']})

    def test_pagination_bounds(self):
        event = make_event(status=PUBLISHED, start=at(2026, 1, 1))
        for _ in range(22):
            make_visible_resource(event)
        first = self.data(self.client.get(ARCHIVE))
        self.assertEqual((first['count'], len(first['results'])), (22, 20))
        second = self.data(self.client.get(first['next']))
        self.assertEqual(len(second['results']), 2)
        self.assertFalse({r['id'] for r in first['results']} & {r['id'] for r in second['results']})

    def test_events_share_date_filters(self):
        e = make_event(status=PUBLISHED, start=at(2026, 3, 5, 23, 30))
        make_event(status=PUBLISHED, start=at(2026, 3, 5, 23, 30) - timedelta(hours=1))
        self.assertEqual(self.ids(self.client.get('/dawah/events/', {'date_from': '2026-03-06'})), [e.pk])
