"""Broadcast drafts, audiences, queue snapshots and retry eligibility (COMM-01)."""
import json
from copy import deepcopy

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, RefResolver

from dawah import broadcasts
from dawah.models import Broadcast, Delivery, DeliveryAttempt, DeliveryGeneration, DeliveryState
from pythagoras.mail import memory

from .helpers import DawahAPITestCase, User
from .test_schema import draft4

LIST = '/dawah/broadcasts/'
COUNT_KEYS = {'queued', 'sent', 'failed', 'uncertain', 'skipped', 'cancelled'}
PAYLOAD_KEYS = {'id', 'subject', 'body', 'body_format', 'body_html', 'audience', 'state', 'version', 'creator_id',
                'created_at', 'updated_at', 'queued_at', 'recipient_count', 'counts', 'eligible_actions'}
DELIVERY_KEYS = {'id', 'student_id', 'snapshot_email', 'state', 'attempt_count', 'retry_generation',
                 'provider_email_id', 'error_code', 'sent_at', 'created_at', 'updated_at', 'eligible_actions'}

_seq = [0]


def make_student(department='', level='', active=True, role='student', email=None):
    _seq[0] += 1
    user = User.objects.create_user(email=email or f'recipient{_seq[0]}@example.test', password=None,
                                    first_name='R', last_name=str(_seq[0]), role=role,
                                    department=department, level=level)
    if not active:
        user.is_active = False
        user.save()
    return user


def draft_payload(**overrides):
    payload = {'subject': 'Weekly Da’wah summary', 'body': 'Summary and recording link',
               'audience': {'type': 'all'}}
    payload.update(overrides)
    return payload


def detail(pk, suffix=''):
    return f'{LIST}{pk}/{suffix}'


FAST_HASHER = override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])


@FAST_HASHER
class BroadcastTestCase(DawahAPITestCase, TestCase):
    def setUp(self):
        super().setUp()
        memory.reset()
        self.as_admin()

    def create(self, **overrides):
        return self.data(self.client.post(LIST, draft_payload(**overrides), format='json'), 201)

    def queue(self, pk, version, status=202):
        return self.client.post(detail(pk, 'queue/'), {'version': version}, format='json')

    def make_queued(self, recipients=2, **overrides):
        for _ in range(recipients):
            make_student()
        draft = self.create(**overrides)
        return self.data(self.queue(draft['id'], draft['version']), 202)


class DraftCrudTests(BroadcastTestCase):
    def test_create_returns_frozen_contract_payload(self):
        data = self.create(audience={'type': 'department', 'department': '  Computer Science '})
        self.assertEqual(set(data), PAYLOAD_KEYS)
        self.assertEqual(data['audience'], {'type': 'department', 'department': 'Computer Science'})
        self.assertEqual((data['state'], data['version'], data['creator_id']), ('draft', 1, self.admin.pk))
        self.assertEqual((data['body_format'], data['body_html'], data['queued_at'], data['recipient_count']),
                         ('plain', None, None, None))
        self.assertEqual(data['counts'], dict.fromkeys(COUNT_KEYS, 0))
        self.assertEqual(data['eligible_actions'], ['edit', 'delete', 'preview', 'queue'])
        self.assertTrue(data['created_at'].endswith('Z'))
        self.assertEqual(Delivery.objects.count(), 0)

    def test_audience_shapes_round_trip(self):
        for audience in ({'type': 'all'}, {'type': 'level', 'level': '300'},
                         {'type': 'department', 'department': 'Physics'}):
            with self.subTest(audience=audience):
                created = self.create(audience=audience)
                self.assertEqual(self.data(self.client.get(detail(created['id'])))['audience'], audience)

    def test_reload_edit_and_version_increment(self):
        created = self.create()
        response = self.client.patch(detail(created['id']), {
            'subject': 'Updated', 'body': 'New body', 'audience': {'type': 'level', 'level': '200'},
            'version': 1}, format='json')
        data = self.data(response)
        self.assertEqual((data['subject'], data['body'], data['version']), ('Updated', 'New body', 2))
        self.assertEqual(data['audience'], {'type': 'level', 'level': '200'})
        partial = self.data(self.client.patch(detail(created['id']), {'subject': 'Again', 'version': 2},
                                              format='json'))
        self.assertEqual((partial['subject'], partial['body'], partial['version']), ('Again', 'New body', 3))
        self.assertEqual(self.data(self.client.get(detail(created['id'])))['version'], 3)

    def test_stale_or_missing_version_is_rejected_without_change(self):
        created = self.create()
        self.data(self.client.patch(detail(created['id']), {'subject': 'First', 'version': 1}, format='json'))
        error = self.error(self.client.patch(detail(created['id']), {'subject': 'Lost', 'version': 1},
                                             format='json'), 409, 'BROADCAST_VERSION_CONFLICT')
        self.assertEqual(error['fields'], {'version': ['The current version is 2.']})
        missing = self.error(self.client.patch(detail(created['id']), {'subject': 'Lost'}, format='json'),
                             400, 'VALIDATION_ERROR')
        self.assertIn('version', missing['fields'])
        for bad in ('2', 2.0, True, None, 0):
            with self.subTest(version=bad):
                self.error(self.client.patch(detail(created['id']), {'subject': 'Lost', 'version': bad},
                                             format='json'), 400, 'VALIDATION_ERROR')
        broadcast = Broadcast.objects.get(pk=created['id'])
        self.assertEqual((broadcast.subject, broadcast.version), ('First', 2))

    def test_invalid_patch_does_not_bump_version(self):
        created = self.create()
        self.error(self.client.patch(detail(created['id']), {'subject': '', 'version': 1}, format='json'),
                   400, 'VALIDATION_ERROR')
        self.assertEqual(Broadcast.objects.get(pk=created['id']).version, 1)

    def test_delete_draft(self):
        created = self.create()
        response = self.client.delete(detail(created['id']))
        self.assertEqual((response.status_code, response.content), (204, b''))
        self.assertFalse(Broadcast.objects.exists())
        self.error(self.client.delete(detail(created['id'])), 404, 'NOT_FOUND')

    def test_missing_broadcast_is_404_everywhere(self):
        for method, suffix, body in [('get', '', None), ('patch', '', {'version': 1}), ('delete', '', None),
                                     ('post', 'preview/', {'version': 1}), ('post', 'queue/', {'version': 1}),
                                     ('get', 'deliveries/', None), ('post', 'retry/', {'delivery_ids': [1]})]:
            with self.subTest(method=method, suffix=suffix):
                response = getattr(self.client, method)(detail(999, suffix), body, format='json')
                self.error(response, 404, 'NOT_FOUND')


class DraftValidationTests(BroadcastTestCase):
    def fields(self, payload):
        return self.error(self.client.post(LIST, payload, format='json'), 400, 'VALIDATION_ERROR')['fields']

    def test_required_fields(self):
        self.assertEqual(set(self.fields({})), {'subject', 'body', 'audience'})

    def test_subject_header_injection_and_length(self):
        for subject in ('Hello\r\nBcc: victim@example.test', 'Tab\there', 'Line sep', 'Nul\x00',
                        'x' * 201, '   '):
            with self.subTest(subject=subject):
                self.assertIn('subject', self.fields(draft_payload(subject=subject)))
        self.assertEqual(self.create(subject='  ' + 'x' * 200 + '  ')['subject'], 'x' * 200)

    def test_strict_types(self):
        for field, value in (('subject', 12), ('body', True), ('body', ['a']), ('subject', None)):
            with self.subTest(field=field, value=value):
                self.assertIn(field, self.fields(draft_payload(**{field: value})))

    def test_body_rules(self):
        self.assertIn('body', self.fields(draft_payload(body=' \n\t ')))
        self.assertIn('body', self.fields(draft_payload(body='bell\x07')))
        self.assertIn('body', self.fields(draft_payload(body='x' * 50_001)))
        created = self.create(body='Line one\r\nLine two\n\n')
        self.assertEqual(created['body'], 'Line one\nLine two')

    def test_body_format_and_html_are_not_accepted_yet(self):
        self.assertIn('body_format', self.fields(draft_payload(body_format='rich')))
        self.assertIn('body_format', self.fields(draft_payload(body_format='markdown')))
        self.assertEqual(self.fields(draft_payload(body_html='<p>x</p>')), {
            'body_html': ['This field cannot be set or changed here.']})
        self.assertEqual(self.create(body_format='plain')['body_format'], 'plain')

    def test_protected_fields_are_rejected(self):
        for name in ('state', 'version', 'id', 'creator_id', 'queued_at', 'recipient_count'):
            with self.subTest(name=name):
                fields = self.fields(draft_payload(**{name: 'queued' if name == 'state' else 3}))
                self.assertIn(name, fields)
        self.assertFalse(Broadcast.objects.exists())

    def test_audience_validation_is_flat_under_audience(self):
        cases = [
            'all', None, {}, {'type': 'everyone'}, {'type': 'all', 'department': 'CS'},
            {'type': 'department'}, {'type': 'department', 'department': '   '},
            {'type': 'department', 'department': 5}, {'type': 'department', 'department': 'x' * 101},
            {'type': 'department', 'department': 'CS\nX'}, {'type': 'department', 'department': 'CS', 'level': '100'},
            {'type': 'level', 'level': '600'}, {'type': 'level', 'level': 300}, {'type': 'level'},
            {'type': 'all', 'extra': 1},
        ]
        for audience in cases:
            with self.subTest(audience=audience):
                fields = self.fields(draft_payload(audience=audience))
                self.assertEqual(list(fields), ['audience'])
                self.assertTrue(all(isinstance(message, str) for message in fields['audience']))

    def test_department_rejects_nul_and_surrogates_on_create_and_patch(self):
        created = self.create()
        for department in ('Computer\x00Science', 'Physics\ud800', '\udfffX'):
            audience = {'type': 'department', 'department': department}
            # json.dumps escapes lone surrogates as \uXXXX, as a browser client would.
            for method, url, body in (('post', LIST, draft_payload(audience=audience)),
                                      ('patch', detail(created['id']), {'audience': audience, 'version': 1})):
                with self.subTest(department=repr(department), method=method):
                    response = getattr(self.client, method)(url, json.dumps(body), content_type='application/json')
                    fields = self.error(response, 400, 'VALIDATION_ERROR')['fields']
                    self.assertEqual(list(fields), ['audience'])
                    self.assertTrue(all(isinstance(message, str) for message in fields['audience']))
        self.assertEqual(Broadcast.objects.get(pk=created['id']).version, 1)
        self.assertEqual(Broadcast.objects.count(), 1)

    def test_non_object_body(self):
        self.error(self.client.post(LIST, [1, 2], format='json'), 400, 'VALIDATION_ERROR')
        created = self.create()
        self.error(self.client.patch(detail(created['id']), [1], format='json'), 400, 'VALIDATION_ERROR')
        self.error(self.client.post(detail(created['id'], 'queue/'), [1], format='json'), 400, 'VALIDATION_ERROR')


class AudienceResolutionTests(BroadcastTestCase):
    def setUp(self):
        super().setUp()
        self.cs300 = make_student(department='Computer Science', level='300')
        self.cs100 = make_student(department='computer science', level='100')
        self.phy300 = make_student(department='Physics', level='300')
        self.blank = make_student()
        self.inactive = make_student(department='Computer Science', level='300', active=False)
        self.other_admin = make_student(department='Computer Science', level='300', role='admin')
        # Legacy row stored before trimming normalization existed.
        self.legacy = make_student(level='500')
        User.objects.filter(pk=self.legacy.pk).update(department='  COMPUTER science ')

    def recipients(self, audience):
        created = self.create(audience=audience)
        preview = self.data(self.client.post(detail(created['id'], 'preview/'), {'version': 1}, format='json'))
        queued = self.data(self.queue(created['id'], 1), 202)
        ids = set(Delivery.objects.filter(broadcast_id=created['id']).values_list('student_id', flat=True))
        self.assertEqual(preview['audience_count'], len(ids))
        self.assertEqual(queued['recipient_count'], len(ids))
        return ids

    def test_all_active_students_only(self):
        self.assertEqual(self.recipients({'type': 'all'}),
                         {self.cs300.pk, self.cs100.pk, self.phy300.pk, self.blank.pk, self.legacy.pk,
                          self.student.pk, self.staff_student.pk})

    def test_department_is_trimmed_and_case_insensitive(self):
        self.assertEqual(self.recipients({'type': 'department', 'department': ' COMPUTER SCIENCE '}),
                         {self.cs300.pk, self.cs100.pk, self.legacy.pk})

    def test_level_is_exact(self):
        self.assertEqual(self.recipients({'type': 'level', 'level': '300'}), {self.cs300.pk, self.phy300.pk})

    def test_inactive_and_admin_accounts_never_receive(self):
        for audience in ({'type': 'all'}, {'type': 'department', 'department': 'Computer Science'},
                         {'type': 'level', 'level': '300'}):
            ids = self.recipients(audience)
            self.assertNotIn(self.inactive.pk, ids)
            self.assertNotIn(self.other_admin.pk, ids)
            self.assertNotIn(self.admin.pk, ids)

    def test_zero_recipients_is_400_and_leaves_draft_unchanged(self):
        created = self.create(audience={'type': 'department', 'department': 'Nonexistent'})
        before = Broadcast.objects.get(pk=created['id'])
        error = self.error(self.queue(created['id'], 1), 400, 'VALIDATION_ERROR')
        self.assertEqual(error['fields'], {'audience': [broadcasts.NO_RECIPIENTS]})
        after = Broadcast.objects.get(pk=created['id'])
        self.assertEqual((after.state, after.version, after.updated_at, after.queued_at),
                         ('draft', 1, before.updated_at, None))
        self.assertEqual(Delivery.objects.count(), 0)
        preview = self.data(self.client.post(detail(created['id'], 'preview/'), {'version': 1}, format='json'))
        self.assertEqual(preview['audience_count'], 0)


class QueueTests(BroadcastTestCase):
    def test_queue_freezes_snapshot_and_acknowledges_202(self):
        first, second = make_student(), make_student()
        created = self.create(body='Hello <b>all</b>\n\nBye')
        response = self.queue(created['id'], 1)
        data = self.data(response, 202)
        self.assertEqual(response.json()['message'], 'Broadcast queued. Delivery is pending.')
        expected = 2 + 2  # plus the base student and staff student fixtures
        self.assertEqual((data['state'], data['version'], data['recipient_count']), ('queued', 1, expected))
        self.assertEqual(data['counts'], {**dict.fromkeys(COUNT_KEYS, 0), 'queued': expected})
        self.assertEqual(data['eligible_actions'], [])
        self.assertTrue(data['queued_at'].endswith('Z'))
        broadcast = Broadcast.objects.get(pk=created['id'])
        self.assertEqual(broadcast.queued_by, self.admin)
        self.assertEqual(broadcast.snapshot_subject, 'Weekly Da’wah summary')
        self.assertEqual(broadcast.snapshot_text, 'Hello <b>all</b>\n\nBye')
        self.assertEqual(broadcast.snapshot_html, '<p>Hello &lt;b&gt;all&lt;/b&gt;</p><p>Bye</p>')
        self.assertEqual(broadcast.snapshot_template_version, 'plain-v1')
        rows = {d.student_id: d for d in Delivery.objects.filter(broadcast=broadcast)}
        self.assertEqual(rows[first.pk].snapshot_email, first.email)
        self.assertEqual(rows[second.pk].snapshot_email, second.email)
        self.assertEqual({d.state for d in rows.values()}, {DeliveryState.QUEUED})
        self.assertEqual({(d.attempt_count, d.retry_generation, d.idempotency_key) for d in rows.values()},
                         {(0, 0, '')})
        self.assertEqual(memory.outbox, [])  # queue never sends

    def test_replay_returns_same_broadcast_and_counts(self):
        make_student()
        created = self.create()
        first = self.data(self.queue(created['id'], 1), 202)
        replay_response = self.queue(created['id'], 1)
        replay = self.data(replay_response, 202)
        self.assertEqual(replay_response.json()['message'], 'Broadcast already queued. Delivery is pending.')
        self.assertEqual(replay, first)
        self.assertEqual(Delivery.objects.count(), first['recipient_count'])

    def test_queue_with_other_version_after_queue_is_already_queued(self):
        make_student()
        created = self.create()
        self.data(self.queue(created['id'], 1), 202)
        self.error(self.queue(created['id'], 2), 409, 'BROADCAST_ALREADY_QUEUED')

    def test_stale_version_conflicts_without_snapshot(self):
        make_student()
        created = self.create()
        self.data(self.client.patch(detail(created['id']), {'subject': 'New', 'version': 1}, format='json'))
        self.error(self.queue(created['id'], 1), 409, 'BROADCAST_VERSION_CONFLICT')
        self.assertEqual(Delivery.objects.count(), 0)
        self.assertEqual(Broadcast.objects.get(pk=created['id']).state, 'draft')

    def test_queue_request_validation(self):
        created = self.create()
        for body in ({}, {'version': '1'}, {'version': 1, 'extra': True}):
            with self.subTest(body=body):
                self.error(self.client.post(detail(created['id'], 'queue/'), body, format='json'),
                           400, 'VALIDATION_ERROR')

    def test_queue_rechecks_stored_content(self):
        make_student()
        created = self.create()
        Broadcast.objects.filter(pk=created['id']).update(subject='Bad\r\nBcc: x@example.test')
        error = self.error(self.queue(created['id'], 1), 400, 'VALIDATION_ERROR')
        self.assertIn('subject', error['fields'])
        self.assertEqual(Delivery.objects.count(), 0)

    def test_queued_content_and_audience_are_immutable(self):
        queued = self.make_queued()
        pk = queued['id']
        self.error(self.client.patch(detail(pk), {'subject': 'Changed', 'version': 1}, format='json'),
                   409, 'BROADCAST_ALREADY_QUEUED')
        self.error(self.client.patch(detail(pk), {'audience': {'type': 'level', 'level': '100'}, 'version': 1},
                                     format='json'), 409, 'BROADCAST_ALREADY_QUEUED')
        self.error(self.client.delete(detail(pk)), 409, 'BROADCAST_ALREADY_QUEUED')
        self.error(self.client.post(detail(pk, 'preview/'), {'version': 1}, format='json'),
                   409, 'BROADCAST_ALREADY_QUEUED')
        broadcast = Broadcast.objects.get(pk=pk)
        self.assertEqual((broadcast.subject, broadcast.audience_type, broadcast.state),
                         ('Weekly Da’wah summary', 'all', 'queued'))

    def test_snapshot_survives_profile_changes(self):
        student = make_student(department='Physics')
        original_email = student.email
        created = self.create(audience={'type': 'department', 'department': 'Physics'})
        self.data(self.queue(created['id'], 1), 202)
        student.email = 'changed@example.test'
        student.department = 'Chemistry'
        student.save()
        make_student(department='Physics')  # joins after queue
        delivery = Delivery.objects.get(broadcast_id=created['id'])
        self.assertEqual((delivery.student_id, delivery.snapshot_email), (student.pk, original_email))
        self.assertEqual(Delivery.objects.filter(broadcast_id=created['id']).count(), 1)
        self.assertEqual(self.data(self.client.get(detail(created['id'])))['recipient_count'], 1)


class PreviewTests(BroadcastTestCase):
    def test_preview_is_escaped_and_side_effect_free(self):
        make_student()
        created = self.create(subject='Hi <there>', body='<script>alert(1)</script>\nline\n\n\npara & more')
        before = Broadcast.objects.get(pk=created['id'])
        response = self.client.post(detail(created['id'], 'preview/'), {'version': 1}, format='json')
        data = self.data(response)
        self.assertEqual(set(data), {'subject', 'body', 'body_format', 'body_html', 'rendered_text',
                                     'rendered_html', 'audience_count'})
        self.assertEqual(data['rendered_html'],
                         '<p>&lt;script&gt;alert(1)&lt;/script&gt;<br>line</p><p>para &amp; more</p>')
        self.assertEqual(data['rendered_text'], '<script>alert(1)</script>\nline\n\n\npara & more')
        self.assertEqual((data['subject'], data['body_format'], data['body_html'], data['audience_count']),
                         ('Hi <there>', 'plain', None, 3))
        after = Broadcast.objects.get(pk=created['id'])
        self.assertEqual((after.version, after.updated_at, after.state, after.snapshot_text),
                         (before.version, before.updated_at, 'draft', ''))
        self.assertEqual(Delivery.objects.count(), 0)
        self.assertEqual(memory.outbox, [])
        self.assertNotIn('@example.test', response.content.decode())

    def test_preview_stale_version(self):
        created = self.create()
        self.data(self.client.patch(detail(created['id']), {'subject': 'New', 'version': 1}, format='json'))
        self.error(self.client.post(detail(created['id'], 'preview/'), {'version': 1}, format='json'),
                   409, 'BROADCAST_VERSION_CONFLICT')
        self.error(self.client.post(detail(created['id'], 'preview/'), {}, format='json'), 400, 'VALIDATION_ERROR')


class ListTests(BroadcastTestCase):
    def setUp(self):
        super().setUp()
        make_student()
        self.draft_old = self.create(subject='Old draft')
        self.draft_new = self.create(subject='New draft')
        self.queued = self.data(self.queue(self.create(subject='Queued')['id'], 1), 202)
        self.completed = self.data(self.queue(self.create(subject='Done')['id'], 1), 202)
        Broadcast.objects.filter(pk=self.completed['id']).update(state='completed')
        # Touch the old draft so it is the most recently edited.
        self.data(self.client.patch(detail(self.draft_old['id']), {'body': 'Edited', 'version': 1},
                                    format='json'))

    def test_kind_drafts(self):
        self.assertEqual(self.ids(self.client.get(LIST, {'kind': 'drafts'})),
                         [self.draft_old['id'], self.draft_new['id']])

    def test_kind_history_and_single_state(self):
        self.assertEqual(self.ids(self.client.get(LIST, {'kind': 'history'})),
                         [self.completed['id'], self.queued['id']])
        self.assertEqual(self.ids(self.client.get(LIST, {'kind': 'history', 'state': 'queued'})),
                         [self.queued['id']])
        self.assertEqual(self.ids(self.client.get(LIST, {'kind': 'history', 'state': 'uncertain'})), [])

    def test_no_kind_lists_everything_with_counts(self):
        data = self.data(self.client.get(LIST))
        self.assertEqual(data['count'], 4)
        by_id = {item['id']: item for item in data['results']}
        self.assertEqual(by_id[self.queued['id']]['counts']['queued'], self.queued['recipient_count'])
        self.assertEqual(by_id[self.draft_new['id']]['counts'], dict.fromkeys(COUNT_KEYS, 0))
        self.assertEqual(self.ids(self.client.get(LIST, {'state': 'draft'})),
                         [self.draft_new['id'], self.draft_old['id']])

    def test_invalid_filters(self):
        for params in ({'kind': 'sent'}, {'state': 'draft,queued'}, {'state': 'nope'},
                       {'kind': 'history', 'state': 'draft'}, {'kind': 'drafts', 'state': 'queued'}):
            with self.subTest(params=params):
                error = self.error(self.client.get(LIST, params), 400, 'VALIDATION_ERROR')
                self.assertTrue(set(error['fields']) <= {'kind', 'state'})

    def test_pagination(self):
        data = self.data(self.client.get(LIST, {'page_size': 1, 'kind': 'drafts'}))
        self.assertEqual((data['count'], len(data['results'])), (2, 1))
        self.assertIsNotNone(data['next'])

    def test_list_query_count_is_constant(self):
        for _ in range(5):
            self.create()
        with self.assertNumQueries(3):  # auth user, count, page: nothing per row
            self.client.get(LIST)


class DeliveryListTests(BroadcastTestCase):
    def test_deliveries_payload_and_state_filter(self):
        queued = self.make_queued(recipients=3)
        rows = list(Delivery.objects.filter(broadcast_id=queued['id']).order_by('id'))
        Delivery.objects.filter(pk=rows[0].pk).update(state='sent', provider_email_id='re_123',
                                                      sent_at=timezone.now(), attempt_count=1)
        Delivery.objects.filter(pk=rows[1].pk).update(state='failed', error_code='INVALID_RECIPIENT',
                                                      attempt_count=3, nonacceptance_known=True)
        data = self.data(self.client.get(detail(queued['id'], 'deliveries/')))
        self.assertEqual(data['count'], len(rows))
        self.assertEqual([item['id'] for item in data['results']], [row.pk for row in rows])
        first = data['results'][0]
        self.assertEqual(set(first), DELIVERY_KEYS)
        self.assertEqual((first['state'], first['provider_email_id'], first['eligible_actions']),
                         ('sent', 're_123', []))
        failed = self.data(self.client.get(detail(queued['id'], 'deliveries/'), {'state': 'failed'}))['results']
        self.assertEqual([(d['id'], d['error_code'], d['eligible_actions']) for d in failed],
                         [(rows[1].pk, 'INVALID_RECIPIENT', ['retry'])])
        queued_rows = self.data(self.client.get(detail(queued['id'], 'deliveries/'), {'state': 'queued'}))
        self.assertEqual(queued_rows['results'][0]['provider_email_id'], None)
        self.error(self.client.get(detail(queued['id'], 'deliveries/'), {'state': 'delivered'}),
                   400, 'VALIDATION_ERROR')
        broadcast = self.data(self.client.get(detail(queued['id'])))
        self.assertEqual(broadcast['eligible_actions'], ['retry'])
        self.assertEqual((broadcast['counts']['sent'], broadcast['counts']['failed'], broadcast['counts']['queued']),
                         (1, 1, len(rows) - 2))

    def test_draft_has_no_deliveries(self):
        created = self.create()
        self.assertEqual(self.data(self.client.get(detail(created['id'], 'deliveries/')))['count'], 0)


class RetryTests(BroadcastTestCase):
    def setUp(self):
        super().setUp()
        self.queued = self.make_queued(recipients=3)
        self.rows = list(Delivery.objects.filter(broadcast_id=self.queued['id']).order_by('id'))

    def retry(self, ids, pk=None):
        return self.client.post(detail(pk or self.queued['id'], 'retry/'), {'delivery_ids': ids}, format='json')

    def test_nothing_failed_yet_is_not_retryable(self):
        error = self.error(self.retry([self.rows[0].pk]), 409, 'DELIVERY_NOT_RETRYABLE')
        self.assertIn(str(self.rows[0].pk), error['fields']['delivery_ids'][0])

    def test_request_validation(self):
        for body in ({}, {'delivery_ids': []}, {'delivery_ids': ['1']}, {'delivery_ids': [True]},
                     {'delivery_ids': [self.rows[0].pk, self.rows[0].pk]}, {'delivery_ids': list(range(1, 102))},
                     {'delivery_ids': [1], 'all': True}):
            with self.subTest(body=body):
                self.error(self.client.post(detail(self.queued['id'], 'retry/'), body, format='json'),
                           400, 'VALIDATION_ERROR')

    def test_ids_from_another_broadcast_reject_everything(self):
        other = self.make_queued(recipients=0, subject='Other')
        other_row = Delivery.objects.filter(broadcast_id=other['id']).first()
        Delivery.objects.filter(pk__in=[self.rows[0].pk, other_row.pk]).update(state='failed',
                                                                                   nonacceptance_known=True)
        error = self.error(self.retry([self.rows[0].pk, other_row.pk]), 400, 'VALIDATION_ERROR')
        self.assertIn(str(other_row.pk), error['fields']['delivery_ids'][0])
        self.assertEqual(Delivery.objects.get(pk=self.rows[0].pk).state, 'failed')

    def test_mixed_eligibility_is_atomic(self):
        Delivery.objects.filter(pk=self.rows[0].pk).update(state='failed', error_code='X', attempt_count=3,
                                                           nonacceptance_known=True)
        for state, extra in (('sent', {'provider_email_id': 're_1'}), ('uncertain', {}), ('skipped', {}),
                             ('sending', {}), ('queued', {})):
            with self.subTest(state=state):
                Delivery.objects.filter(pk=self.rows[1].pk).update(state=state, **extra)
                self.error(self.retry([self.rows[0].pk, self.rows[1].pk]), 409, 'DELIVERY_NOT_RETRYABLE')
                self.assertEqual(Delivery.objects.get(pk=self.rows[0].pk).state, 'failed')

    def test_failed_with_provider_id_is_not_retryable(self):
        Delivery.objects.filter(pk=self.rows[0].pk).update(state='failed', provider_email_id='re_9',
                                                           nonacceptance_known=True)
        self.error(self.retry([self.rows[0].pk]), 409, 'DELIVERY_NOT_RETRYABLE')

    def test_failure_without_positive_nonacceptance_evidence_is_not_retryable(self):
        """Reviewer reproduction: a payload conflict may hide an accepted earlier
        request, so it must never start a new generation/key."""
        dispatched = timezone.now()
        for code, extra in (('IDEMPOTENCY_PAYLOAD_CONFLICT', {}),
                            ('IDEMPOTENCY_DEADLINE_EXPIRED', {'idempotency_deadline_at': dispatched}),
                            ('PROVIDER_TIMEOUT', {})):
            with self.subTest(code=code):
                Delivery.objects.filter(pk=self.rows[0].pk).update(
                    state='failed', error_code=code, attempt_count=1, idempotency_key='test/broadcast/1/generation/0',
                    request_hash='abc', first_dispatch_at=dispatched, nonacceptance_known=False, **extra)
                error = self.error(self.retry([self.rows[0].pk]), 409, 'DELIVERY_NOT_RETRYABLE')
                self.assertIn(str(self.rows[0].pk), error['fields']['delivery_ids'][0])
                row = Delivery.objects.get(pk=self.rows[0].pk)
                self.assertEqual((row.state, row.retry_generation, row.idempotency_key),
                                 ('failed', 0, 'test/broadcast/1/generation/0'))
                listed = self.data(self.client.get(detail(self.queued['id'], 'deliveries/'), {'state': 'failed'}))
                self.assertEqual(listed['results'][0]['eligible_actions'], [])
                self.assertNotIn('retry', self.data(self.client.get(detail(self.queued['id'])))['eligible_actions'])
        self.assertFalse(DeliveryGeneration.objects.exists())

    def test_known_failure_starts_new_generation(self):
        Delivery.objects.filter(broadcast_id=self.queued['id']).update(state='sent', provider_email_id='re_x',
                                                                       attempt_count=1)
        dispatched = timezone.now()
        snapshot = {'to': ['x@example.test'], 'subject': 's'}
        Delivery.objects.filter(pk=self.rows[0].pk).update(state='failed', provider_email_id='', error_code='E',
                                                           attempt_count=3, idempotency_key='k/1',
                                                           request_hash='abc', request_snapshot=snapshot,
                                                           first_dispatch_at=dispatched,
                                                           idempotency_deadline_at=dispatched,
                                                           nonacceptance_known=True)
        Broadcast.objects.filter(pk=self.queued['id']).update(state='completed_with_errors')
        response = self.retry([self.rows[0].pk])
        data = self.data(response, 202)
        self.assertEqual(data['state'], 'processing')
        self.assertEqual((data['counts']['queued'], data['counts']['failed']), (1, 0))
        row = Delivery.objects.get(pk=self.rows[0].pk)
        self.assertEqual((row.state, row.retry_generation, row.attempt_count, row.error_code, row.idempotency_key,
                          row.request_hash, row.retry_requested_by),
                         ('queued', 1, 0, '', '', '', self.admin))
        self.assertEqual((row.nonacceptance_known, row.request_snapshot, row.first_dispatch_at), (False, None, None))
        evidence = DeliveryGeneration.objects.get(delivery=row)
        self.assertEqual(
            (evidence.generation, evidence.state, evidence.attempt_count, evidence.error_code,
             evidence.nonacceptance_known, evidence.idempotency_key, evidence.request_hash,
             evidence.request_snapshot, evidence.first_dispatch_at, evidence.idempotency_deadline_at,
             evidence.requested_at, evidence.requested_by, evidence.closed_by),
            (0, 'failed', 3, 'E', True, 'k/1', 'abc', snapshot, dispatched, dispatched, None, None, self.admin))
        self.assertEqual(memory.outbox, [])
        # A second known failure closes generation 1, recording who requested it.
        Delivery.objects.filter(pk=row.pk).update(state='failed', error_code='F', attempt_count=1,
                                                  idempotency_key='k/2', nonacceptance_known=True)
        self.data(self.retry([row.pk]), 202)
        second = DeliveryGeneration.objects.get(delivery=row, generation=1)
        self.assertEqual((second.idempotency_key, second.error_code, second.requested_by, second.closed_by),
                         ('k/2', 'F', self.admin, self.admin))
        self.assertIsNotNone(second.requested_at)
        self.assertEqual(Delivery.objects.get(pk=row.pk).retry_generation, 2)
        self.assertEqual(DeliveryGeneration.objects.filter(delivery=row).count(), 2)

    def test_draft_cannot_be_retried(self):
        draft = self.create()
        self.error(self.retry([self.rows[0].pk], pk=draft['id']), 409, 'BROADCAST_NOT_QUEUED')


class DerivedStateTests(TestCase):
    def counts(self, **values):
        return {**dict.fromkeys(broadcasts.COUNT_KEYS, 0), **values}

    def test_precedence(self):
        derive = broadcasts.derive_state
        self.assertEqual(derive(self.counts(queued=2, uncertain=1), True), 'uncertain')
        self.assertEqual(derive(self.counts(queued=2), False), 'queued')
        self.assertEqual(derive(self.counts(queued=2, sent=1), True), 'processing')
        self.assertEqual(derive(self.counts(failed=1, sent=3), True), 'completed_with_errors')
        self.assertEqual(derive(self.counts(sent=3, skipped=1, cancelled=1), True), 'completed')


class PermissionTests(BroadcastTestCase):
    def routes(self, pk, delivery_id):
        return [('get', LIST, None), ('post', LIST, draft_payload()), ('get', detail(pk), None),
                ('patch', detail(pk), {'subject': 'x', 'version': 1}), ('delete', detail(pk), None),
                ('post', detail(pk, 'preview/'), {'version': 1}), ('post', detail(pk, 'queue/'), {'version': 1}),
                ('get', detail(pk, 'deliveries/'), None),
                ('post', detail(pk, 'retry/'), {'delivery_ids': [delivery_id]})]

    def test_only_active_admins(self):
        queued = self.make_queued(recipients=1)
        delivery = Delivery.objects.filter(broadcast_id=queued['id']).first()
        for user, status, code in [(None, 401, 'AUTHENTICATION_REQUIRED'), (self.student, 403, 'PERMISSION_DENIED'),
                                   (self.staff_student, 403, 'PERMISSION_DENIED'),
                                   (self.inactive_admin, 401, 'AUTHENTICATION_REQUIRED')]:
            self.as_user(user)
            for method, url, body in self.routes(queued['id'], delivery.pk):
                with self.subTest(user=getattr(user, 'email', None), method=method, url=url):
                    response = getattr(self.client, method)(url, body, format='json')
                    self.assertIn(response.status_code, (401, 403))
                    if user is not self.inactive_admin:
                        self.error(response, status, code)
                    self.assertNotIn('@example.test', response.content.decode())
        self.assertEqual(Broadcast.objects.count(), 1)

    def test_recipient_addresses_only_in_admin_delivery_detail(self):
        queued = self.make_queued(recipients=2)
        emails = list(Delivery.objects.values_list('snapshot_email', flat=True))
        for url in (LIST, detail(queued['id']), f'{LIST}?kind=history'):
            body = self.client.get(url).content.decode()
            for email in emails:
                self.assertNotIn(email, body)
        body = self.client.get(detail(queued['id'], 'deliveries/')).content.decode()
        for email in emails:
            self.assertIn(email, body)


class ThrottleTests(BroadcastTestCase):
    def test_queue_and_retry_share_sixty_per_hour_per_admin(self):
        make_student()
        created = self.create()
        cache.clear()
        for _ in range(59):
            self.assertEqual(self.queue(created['id'], 1).status_code, 202)
        self.error(self.client.post(detail(created['id'], 'retry/'), {'delivery_ids': [10 ** 6]}, format='json'),
                   400, 'VALIDATION_ERROR')  # the 60th request, counted although invalid
        self.error(self.queue(created['id'], 1), 429, 'THROTTLED')
        self.error(self.client.post(detail(created['id'], 'retry/'), {'delivery_ids': [1]}, format='json'),
                   429, 'THROTTLED')
        # Drafts/preview/list are not throttled, and other admins have their own budget.
        self.data(self.client.get(LIST))
        other = User.objects.create_user(email='admin2@example.test', password='Admin-Pass-9876', role='admin')
        self.as_user(other)
        self.data(self.queue(created['id'], 1), 202)


class BroadcastSchemaTests(BroadcastTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(public=True)
        cls.json_schema = draft4(deepcopy(cls.schema))

    def validate(self, path, method, status, response):
        self.assertEqual(str(response.status_code), status, response.content)
        responses = self.json_schema['paths'][path][method]['responses']
        schema = responses[status]['content']['application/json']['schema']
        Draft4Validator(schema, resolver=RefResolver.from_schema(self.json_schema)).validate(response.json())

    def test_real_responses_match_schema(self):
        make_student()
        created = self.client.post(LIST, draft_payload(), format='json')
        pk = created.json()['data']['id']
        base = '/dawah/broadcasts/{broadcast_id}/'
        self.validate('/dawah/broadcasts/', 'post', '201', created)
        self.validate('/dawah/broadcasts/', 'get', '200', self.client.get(LIST))
        self.validate(base, 'get', '200', self.client.get(detail(pk)))
        self.validate(base, 'patch', '409', self.client.patch(detail(pk), {'version': 9}, format='json'))
        self.validate(base + 'preview/', 'post', '200',
                      self.client.post(detail(pk, 'preview/'), {'version': 1}, format='json'))
        self.validate(base + 'queue/', 'post', '202', self.queue(pk, 1))
        self.validate(base + 'deliveries/', 'get', '200', self.client.get(detail(pk, 'deliveries/')))
        delivery = Delivery.objects.first()
        Delivery.objects.filter(pk=delivery.pk).update(state='failed', nonacceptance_known=True)
        self.validate(base + 'retry/', 'post', '202',
                      self.client.post(detail(pk, 'retry/'), {'delivery_ids': [delivery.pk]}, format='json'))

    def test_examples_match_response_schemas(self):
        from dawah import examples
        component = self.json_schema['components']['schemas']
        resolver = RefResolver.from_schema(self.json_schema)
        for name, value in (('Broadcast', examples.BROADCAST_DRAFT), ('Broadcast', examples.BROADCAST_QUEUED),
                            ('Preview', examples.BROADCAST_PREVIEW), ('Delivery', examples.DELIVERY_QUEUED)):
            with self.subTest(name=name):
                Draft4Validator(component[name], resolver=resolver).validate(value)


class AttemptAuditTests(TestCase):
    def test_attempt_rows_are_unique_per_generation(self):
        from django.db import IntegrityError, transaction
        admin = User.objects.create_user(email='a@example.test', password='x-Pass-9876', role='admin')
        student = make_student()
        broadcast = Broadcast.objects.create(created_by=admin, subject='s', body='b', audience_type='all',
                                             state='queued', queued_at=timezone.now(), recipient_count=1)
        delivery = Delivery.objects.create(broadcast=broadcast, student=student, snapshot_email=student.email)
        DeliveryAttempt.objects.create(delivery=delivery, generation=0, attempt=1, started_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            DeliveryAttempt.objects.create(delivery=delivery, generation=0, attempt=1, started_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Delivery.objects.create(broadcast=broadcast, student=student, snapshot_email='dup@example.test')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Broadcast.objects.create(subject='s', body='b', audience_type='level', audience_level='')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Broadcast.objects.create(subject='s', body='b', audience_type='all', state='queued')
