"""Weekly digests and event reminders (COMM-06): API contract, controlled-clock
scheduling, cancellation, outbox handoff and run history."""
import logging
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from django.db.models import F
from django.test import TestCase, override_settings
from django.utils import timezone

# Import the API module first (pre-existing circular import, see test_concurrency).
from dawah import views  # noqa: F401, I001
from dawah import automations, outbox
from dawah.models import (
    AutomationRule, AutomationRun, Broadcast, Delivery, DeliveryAttempt, Event, EventResource, PublicationStatus,
)
from pythagoras.mail import SendResult

from .helpers import PUBLISHED, DawahAPITestCase, User, make_event, make_resource
from .test_outbox import MAIL_SETTINGS, FakeTransport

UTC = dt_timezone.utc
A = '/dawah/automations/'
# Monday 9 November 2026 08:00 Africa/Lagos (UTC+1) == 07:00Z.
MONDAY_8_LAGOS = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)


def rule(kind='weekly_digest', enabled=True, owner=None, active_from=None, **fields):
    defaults = {'subject': 'Weekly summary', 'body': 'From {{week_start}}:\n\n{{summaries}}'}
    if kind == 'weekly_digest':
        defaults.update(weekday=0, local_time=time(8, 0), timezone='Africa/Lagos')
    else:
        defaults.update(offset_minutes=1440, categories=['jumuah'], subject='Reminder: {{event_title}}',
                        body='{{event_title}} starts {{event_start}} at {{event_location}}. {{event_url}}')
    defaults.update(fields)
    return AutomationRule.objects.create(kind=kind, enabled=enabled, owner=owner,
                                         active_from=active_from or MONDAY_8_LAGOS - timedelta(days=1), **defaults)


def runs(rule_obj):
    return list(AutomationRun.objects.filter(rule=rule_obj).order_by('scheduled_at', 'id'))


class AutomationBase(DawahAPITestCase, TestCase):
    def ensure_recipients(self):
        return User.objects.filter(role='student', is_active=True).count()


# ---------------------------------------------------------------- API contract

class AutomationApiTests(AutomationBase):
    def test_admin_only(self):
        rule_obj = rule(owner=self.admin)
        for user, status in ((None, 401), (self.student, 403), (self.staff_student, 403), (self.inactive_admin, 403)):
            self.as_user(user)
            with self.subTest(user=user):
                self.assertEqual(self.client.get(A).status_code, status)
                self.assertEqual(self.client.post(A, {'kind': 'weekly_digest'}, format='json').status_code, status)
                self.assertEqual(self.client.get(f'{A}{rule_obj.pk}/runs/').status_code, status)
                self.assertEqual(self.client.post(f'{A}{rule_obj.pk}/enable/', {'version': 1},
                                                  format='json').status_code, status)
        rule_obj.refresh_from_db()
        self.assertEqual(rule_obj.version, 1)

    def test_create_d4_examples_start_disabled_with_defaults(self):
        self.as_admin()
        weekly = self.data(self.client.post(A, {
            'kind': 'weekly_digest', 'audience': {'type': 'all'}, 'timezone': 'Africa/Lagos', 'weekday': 0,
            'local_time': '08:00', 'enabled': False}, format='json'), 201)
        self.assertEqual({k: weekly[k] for k in ('kind', 'enabled', 'timezone', 'weekday', 'local_time',
                                                  'offset_minutes', 'owner_id', 'owner_active', 'blocked_reason',
                                                  'version', 'categories')},
                         {'kind': 'weekly_digest', 'enabled': False, 'timezone': 'Africa/Lagos', 'weekday': 0,
                          'local_time': '08:00', 'offset_minutes': None, 'owner_id': self.admin.pk,
                          'owner_active': True, 'blocked_reason': None, 'version': 1, 'categories': []})
        self.assertIn('{{summaries}}', weekly['template']['body'])
        self.assertEqual(weekly['template']['body_html'], None)
        reminder = self.data(self.client.post(A, {
            'kind': 'event_reminder', 'audience': {'type': 'all'}, 'offset_minutes': 1440, 'categories': ['jumuah'],
            'enabled': False, 'template': {'subject': 'Reminder: {{event_title}}', 'body_format': 'rich',
                                           'body_html': '<p onclick="x()">Starts {{event_start}}<script>1</script>'
                                                        '</p>'}}, format='json'), 201)
        self.assertEqual((reminder['offset_minutes'], reminder['weekday'], reminder['local_time']), (1440, None, None))
        self.assertEqual(reminder['template']['body_html'], '<p>Starts {{event_start}}</p>')
        self.assertEqual(reminder['template']['body'], 'Starts {{event_start}}')
        self.assertFalse(AutomationRule.objects.filter(enabled=True).exists())
        listed = self.data(self.client.get(A))
        self.assertEqual([r['id'] for r in listed['results']], [reminder['id'], weekly['id']])

    def test_create_validation_errors_are_flat_fields(self):
        self.as_admin()
        cases = [
            ({'kind': 'weekly_digest', 'enabled': True}, 'enabled'),
            ({'kind': 'event_reminder', 'offset_minutes': 60}, 'categories'),
            ({'kind': 'event_reminder', 'offset_minutes': 60, 'categories': []}, 'categories'),
            ({'kind': 'event_reminder', 'offset_minutes': 60, 'categories': ['party']}, 'categories'),
            ({'kind': 'event_reminder', 'offset_minutes': 0, 'categories': ['usroh']}, 'offset_minutes'),
            ({'kind': 'weekly_digest', 'offset_minutes': 60}, 'offset_minutes'),
            ({'kind': 'weekly_digest', 'timezone': 'Europe/London'}, 'timezone'),
            ({'kind': 'weekly_digest', 'local_time': '8:00'}, 'local_time'),
            ({'kind': 'weekly_digest', 'weekday': 7}, 'weekday'),
            ({'kind': 'weekly_digest', 'template': {'subject': 'Hi {{event_title}}', 'body': 'x'}}, 'template'),
            ({'kind': 'weekly_digest', 'template': {'subject': 'Hi', 'body': '{% if x %}y{% endif %}'}}, 'template'),
            ({'kind': 'weekly_digest', 'template': {'subject': '', 'body': 'x'}}, 'template.subject'),
            ({'kind': 'weekly_digest', 'template': {'subject': 'Hi', 'body_format': 'rich',
                                                    'body_html': '<script>x</script>'}}, 'template.body_html'),
            ({'kind': 'weekly_digest', 'audience': {'type': 'level', 'level': '900'}}, 'audience'),
            ({'kind': 'nope'}, 'kind'),
        ]
        for body, field in cases:
            with self.subTest(body=body):
                error = self.error(self.client.post(A, body, format='json'), 400, 'VALIDATION_ERROR')
                self.assertIn(field, error['fields'])
                self.assertTrue(all(isinstance(m, str) for msgs in error['fields'].values() for m in msgs))
        self.assertFalse(AutomationRule.objects.exists())

    def test_patch_is_versioned_and_kind_bound(self):
        rule_obj = rule(kind='event_reminder', enabled=False, owner=self.admin)
        path = f'{A}{rule_obj.pk}/'
        self.as_admin()
        self.error(self.client.patch(path, {'offset_minutes': 60}, format='json'), 400, 'VALIDATION_ERROR')
        self.error(self.client.patch(path, {'offset_minutes': 60, 'version': 9}, format='json'), 409,
                   'AUTOMATION_VERSION_CONFLICT')
        for body in ({'kind': 'weekly_digest', 'version': 1}, {'enabled': True, 'version': 1},
                     {'weekday': 1, 'version': 1}, {'owner_id': self.admin.pk, 'offset_minutes': 5, 'version': 1},
                     {'owner_id': self.student.pk, 'version': 1}, {'owner_id': self.inactive_admin.pk, 'version': 1}):
            with self.subTest(body=body):
                self.error(self.client.patch(path, body, format='json'), 400, 'VALIDATION_ERROR')
        data = self.data(self.client.patch(path, {
            'audience': {'type': 'level', 'level': '300'}, 'categories': ['JUMUAH', 'usroh'], 'offset_minutes': 120,
            'template': {'subject': 'Soon: {{event_title}}', 'body_format': 'plain', 'body': '{{event_url}}'},
            'version': 1}, format='json'))
        self.assertEqual((data['version'], data['offset_minutes'], data['categories'], data['audience'],
                          data['enabled']),
                         (2, 120, ['jumuah', 'usroh'], {'type': 'level', 'level': '300'}, False))
        other = User.objects.create_user(email='other-admin@example.test', password=None, role='admin')
        data = self.data(self.client.patch(path, {'owner_id': other.pk, 'version': 2}, format='json'))
        self.assertEqual((data['owner_id'], data['version']), (other.pk, 3))

    def test_enable_disable_and_owner_validity(self):
        rule_obj = rule(enabled=False, owner=self.inactive_admin)
        path = f'{A}{rule_obj.pk}/'
        self.as_admin()
        data = self.data(self.client.get(path))
        self.assertEqual((data['owner_active'], data['blocked_reason']), (False, 'owner_inactive'))
        self.error(self.client.post(f'{path}enable/', {'version': 1}, format='json'), 409, 'AUTOMATION_OWNER_INACTIVE')
        self.error(self.client.post(f'{path}enable/', {'version': 1, 'x': 1}, format='json'), 400, 'VALIDATION_ERROR')
        self.data(self.client.patch(path, {'owner_id': self.admin.pk, 'version': 1}, format='json'))
        self.error(self.client.post(f'{path}enable/', {'version': 1}, format='json'), 409,
                   'AUTOMATION_VERSION_CONFLICT')
        data = self.data(self.client.post(f'{path}enable/', {'version': 2}, format='json'))
        self.assertEqual((data['enabled'], data['version']), (True, 3))
        rule_obj.refresh_from_db()
        self.assertIsNotNone(rule_obj.active_from)
        data = self.data(self.client.post(f'{path}disable/', {'version': 3}, format='json'))
        self.assertEqual((data['enabled'], data['version']), (False, 4))
        self.assertFalse(Broadcast.objects.exists())

    def test_enable_rejects_invalid_stored_rule(self):
        rule_obj = rule(enabled=False, owner=self.admin, body='{{unknown}}')
        self.as_admin()
        error = self.error(self.client.post(f'{A}{rule_obj.pk}/enable/', {'version': 1}, format='json'), 400,
                           'VALIDATION_ERROR')
        self.assertIn('template', error['fields'])

    def test_preview_never_sends_and_uses_shared_renderer(self):
        event = make_event(status=PUBLISHED, start=timezone.now() - timedelta(days=1), category='Jumuah',
                           summary='Patience <script>alert(1)</script> & gratitude')
        make_resource(event, status=PUBLISHED, type='vid', youtube_video_id='dQw4w9WgXcQ', title='Recording')
        tomorrow = (timezone.now().astimezone(automations.ZoneInfo('Africa/Lagos')) + timedelta(days=1)).weekday()
        rule_obj = rule(enabled=False, owner=self.admin, weekday=tomorrow)  # window covers the last ~5 days
        self.as_admin()
        self.error(self.client.post(f'{A}{rule_obj.pk}/preview/', {'version': 2}, format='json'), 409,
                   'AUTOMATION_VERSION_CONFLICT')
        data = self.data(self.client.post(f'{A}{rule_obj.pk}/preview/', {'version': 1}, format='json'))
        self.assertEqual((data['audience_count'], data['skip_reason_code']), (2, None))
        self.assertEqual([e['id'] for e in data['source']['events']], [event.pk])
        self.assertIn('Patience &lt;script&gt;alert(1)&lt;/script&gt; &amp; gratitude', data['rendered_html'])
        self.assertNotIn('<script', data['rendered_html'])
        self.assertIn('Recording: https://www.youtube.com/watch?v=dQw4w9WgXcQ', data['rendered_text'])
        self.assertTrue(data['rendered_html'].startswith('<!DOCTYPE html>'))
        self.assertFalse(Broadcast.objects.exists() or Delivery.objects.exists() or AutomationRun.objects.exists())

    def test_reminder_preview_without_events_uses_sample_and_reports_skip(self):
        rule_obj = rule(kind='event_reminder', enabled=False, owner=self.admin)
        self.as_admin()
        data = self.data(self.client.post(f'{A}{rule_obj.pk}/preview/', {'version': 1}, format='json'))
        self.assertEqual(data['skip_reason_code'], 'no_upcoming_event')
        self.assertEqual(data['subject'], 'Reminder: Sample event')
        self.assertIsNone(data['source']['event'])

    def test_run_history_payload_and_schema(self):
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator, RefResolver

        from .test_schema import draft4

        rule_obj = rule(owner=self.admin)
        make_event(status=PUBLISHED, start=MONDAY_8_LAGOS - timedelta(days=3))
        automations.tick(MONDAY_8_LAGOS + timedelta(minutes=1))
        AutomationRun.objects.create(rule=rule_obj, occurrence_key='rule-x:old', scheduled_at=MONDAY_8_LAGOS
                                     - timedelta(days=7), state='missed', reason_code='missed_catch_up_window')
        self.as_admin()
        response = self.client.get(f'{A}{rule_obj.pk}/runs/')
        page = self.data(response)
        first = page['results'][0]
        self.assertEqual({k: first[k] for k in ('occurrence_key', 'scheduled_at', 'state', 'reason_code',
                                                'recipient_count')},
                         {'occurrence_key': f'rule-{rule_obj.pk}:2026-11-09T07:00:00Z',
                          'scheduled_at': '2026-11-09T07:00:00Z', 'state': 'queued', 'reason_code': None,
                          'recipient_count': 2})
        self.assertEqual(first['counts']['queued'], 2)
        self.assertEqual((first['source']['window_start'], first['source']['window_end']),
                         ('2026-11-02T07:00:00Z', '2026-11-09T07:00:00Z'))
        self.assertEqual((page['results'][1]['state'], page['results'][1]['counts']), ('missed', None))
        schema = draft4(SchemaGenerator().get_schema(public=True))
        rule_response = self.client.get(f'{A}{rule_obj.pk}/')
        preview = self.client.post(f'{A}{rule_obj.pk}/preview/', {'version': 1}, format='json')
        for path, method, resp in ((f'{A}{{rule_id}}/runs/', 'get', response),
                                   (f'{A}{{rule_id}}/', 'get', rule_response),
                                   (f'{A}{{rule_id}}/preview/', 'post', preview), (A, 'get', self.client.get(A))):
            with self.subTest(path=path):
                content = schema['paths'][path][method]['responses']['200']['content']['application/json']
                validator = Draft4Validator(content['schema'], resolver=RefResolver.from_schema(schema))
                validator.validate(resp.json())
                for example in content.get('examples', {}).values():
                    validator.validate(example['value'])


# ---------------------------------------------------------------- weekly scheduling

class WeeklyScheduleTests(AutomationBase):
    def test_window_boundaries_timezone_and_selection(self):
        rule_obj = rule(owner=self.admin, categories=['jumuah', 'usroh'])
        start = MONDAY_8_LAGOS - timedelta(days=7)
        included = [make_event(status=PUBLISHED, start=start, title='At window start', category=' JUMUAH '),
                    make_event(status=PUBLISHED, start=MONDAY_8_LAGOS - timedelta(seconds=1), title='Last second',
                               category='Usroh')]
        make_event(status=PUBLISHED, start=MONDAY_8_LAGOS, title='At window end')
        make_event(status=PUBLISHED, start=start - timedelta(seconds=1), title='Before window')
        make_event(status=PUBLISHED, start=start + timedelta(days=1), summary='  ', title='No summary')
        make_event(status=PUBLISHED, start=start + timedelta(days=1), category='dawah_night', title='Other category')
        make_event(status=PUBLISHED, start=start + timedelta(days=1), category='jumuah social', title='Free text')
        make_event(start=start + timedelta(days=1), title='Draft')

        self.assertEqual(automations.tick(MONDAY_8_LAGOS - timedelta(seconds=1)), 0)  # Monday 07:59:59 Lagos
        self.assertEqual(runs(rule_obj), [])
        self.assertEqual(automations.tick(MONDAY_8_LAGOS), 1)
        (run,) = runs(rule_obj)
        self.assertEqual((run.occurrence_key, run.state, run.window_start, run.window_end),
                         (f'rule-{rule_obj.pk}:2026-11-09T07:00:00Z', 'queued', start, MONDAY_8_LAGOS))
        self.assertEqual([e['id'] for e in run.source['events']], [e.pk for e in included])
        broadcast = run.broadcast
        self.assertEqual((broadcast.state, broadcast.recipient_count, broadcast.created_by_id),
                         ('queued', 2, self.admin.pk))
        self.assertIn('At window start', broadcast.snapshot_text)
        self.assertIn('Monday 2 November 2026, 08:00 (Africa/Lagos)', broadcast.snapshot_text)
        self.assertNotIn('Other category', broadcast.snapshot_text)
        # Repeated ticks (restart, next Cron pass) return the existing run.
        self.assertEqual(automations.tick(MONDAY_8_LAGOS + timedelta(minutes=5)), 0)
        self.assertEqual(automations.tick(MONDAY_8_LAGOS + timedelta(hours=23)), 0)
        self.assertEqual((AutomationRun.objects.count(), Broadcast.objects.count(), Delivery.objects.count()),
                         (1, 1, 2))

    def test_lagos_local_day_differs_from_utc_day(self):
        # Monday 00:30 Lagos is Sunday 23:30Z.
        rule_obj = rule(owner=self.admin, local_time=time(0, 30))
        make_event(status=PUBLISHED, start=datetime(2026, 11, 6, 12, tzinfo=UTC))
        self.assertEqual(automations.tick(datetime(2026, 11, 8, 23, 29, tzinfo=UTC)), 0)
        self.assertEqual(automations.tick(datetime(2026, 11, 8, 23, 30, tzinfo=UTC)), 1)
        self.assertEqual(runs(rule_obj)[0].occurrence_key, f'rule-{rule_obj.pk}:2026-11-08T23:30:00Z')

    def test_catch_up_sends_only_latest_and_records_older_as_missed(self):
        rule_obj = rule(owner=self.admin, active_from=MONDAY_8_LAGOS - timedelta(days=20))
        make_event(status=PUBLISHED, start=MONDAY_8_LAGOS - timedelta(days=2))
        self.assertEqual(automations.tick(MONDAY_8_LAGOS + timedelta(hours=23, minutes=59)), 1)
        self.assertEqual([(r.scheduled_at.date().isoformat(), r.state, r.reason_code or None) for r in runs(rule_obj)],
                         [('2026-10-26', 'missed', 'missed_catch_up_window'),
                          ('2026-11-02', 'missed', 'missed_catch_up_window'),
                          ('2026-11-09', 'queued', None)])
        self.assertEqual(Broadcast.objects.count(), 1)

    def test_occurrence_outside_catch_up_window_is_missed_not_sent(self):
        rule_obj = rule(owner=self.admin, active_from=MONDAY_8_LAGOS - timedelta(days=1))
        make_event(status=PUBLISHED, start=MONDAY_8_LAGOS - timedelta(days=2))
        self.assertEqual(automations.tick(MONDAY_8_LAGOS + timedelta(days=1, seconds=1)), 0)
        self.assertEqual([(r.state, r.reason_code) for r in runs(rule_obj)], [('missed', 'missed_catch_up_window')])
        self.assertFalse(Broadcast.objects.exists())

    def test_occurrences_before_enable_are_never_materialized(self):
        rule(owner=self.admin, active_from=MONDAY_8_LAGOS + timedelta(minutes=1))
        make_event(status=PUBLISHED, start=MONDAY_8_LAGOS - timedelta(days=2))
        self.assertEqual(automations.tick(MONDAY_8_LAGOS + timedelta(minutes=2)), 0)
        self.assertFalse(AutomationRun.objects.exists())

    def test_empty_summaries_and_empty_audience_are_recorded_skips(self):
        empty = rule(owner=self.admin)
        nobody = rule(owner=self.admin, audience_type='department', audience_department='Nobody here')
        automations.tick(MONDAY_8_LAGOS)
        self.assertEqual([(r.state, r.reason_code) for r in runs(empty)], [('skipped', 'no_eligible_summaries')])
        make_event(status=PUBLISHED, start=MONDAY_8_LAGOS + timedelta(days=6))
        automations.tick(MONDAY_8_LAGOS + timedelta(days=7))
        self.assertEqual([(r.state, r.reason_code) for r in runs(nobody)],
                         [('skipped', 'no_eligible_summaries'), ('skipped', 'no_recipients')])
        self.assertEqual(list(Broadcast.objects.values_list('automation_run__rule', flat=True)), [empty.pk])

    def test_inactive_or_non_admin_owner_blocks_visibly(self):
        blocked = rule(owner=self.inactive_admin)
        demoted = rule(owner=self.student)
        make_event(status=PUBLISHED, start=MONDAY_8_LAGOS - timedelta(days=1))
        self.assertEqual(automations.tick(MONDAY_8_LAGOS), 0)
        for rule_obj in (blocked, demoted):
            self.assertEqual([(r.state, r.reason_code) for r in runs(rule_obj)], [('blocked', 'owner_inactive')])
        self.assertFalse(Broadcast.objects.exists())

    def test_disabled_rules_create_nothing(self):
        rule(owner=self.admin, enabled=False)
        make_event(status=PUBLISHED, start=MONDAY_8_LAGOS - timedelta(days=1))
        self.assertEqual(automations.tick(MONDAY_8_LAGOS), 0)
        self.assertFalse(AutomationRun.objects.exists())

    def test_rich_template_goes_through_shared_sanitizer(self):
        rule(owner=self.admin, body_format='rich', body='x',
             body_html='<p>Summary for {{week_start}}:</p><p>{{summaries}}</p>')
        event = make_event(status=PUBLISHED, start=MONDAY_8_LAGOS - timedelta(days=1), title='<b>Talk</b>',
                           summary='Line one\nLine <img src=x onerror=alert(1)>')
        resource = make_resource(event, status=PUBLISHED, type='vid', youtube_video_id='dQw4w9WgXcQ', title='Video')
        automations.tick(MONDAY_8_LAGOS)
        broadcast = Broadcast.objects.get()
        self.assertEqual(broadcast.body_format, 'rich')
        self.assertIn('<h3 style=', broadcast.snapshot_html)
        self.assertIn('&lt;b&gt;Talk&lt;/b&gt;', broadcast.snapshot_html)
        self.assertNotIn('<img', broadcast.snapshot_html)
        self.assertIn('href="https://www.youtube.com/watch?v=dQw4w9WgXcQ"', broadcast.snapshot_html)
        self.assertIn(f'{resource.title} (https://www.youtube.com/watch?v=dQw4w9WgXcQ)', broadcast.snapshot_text)


# ---------------------------------------------------------------- reminders

@override_settings(FRONTEND_BASE_URL='https://app.example.test/')
class ReminderScheduleTests(AutomationBase):
    START = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)  # Friday 13:00 Lagos

    def setUp(self):
        super().setUp()
        self.rule = rule(kind='event_reminder', owner=self.admin)
        self.event = make_event(status=PUBLISHED, start=self.START, category='Jumuah')
        self.due = self.START - timedelta(days=1)

    def test_offset_boundary_and_revisioned_key(self):
        self.assertEqual(automations.tick(self.due - timedelta(seconds=1)), 0)
        self.assertEqual(automations.tick(self.due), 1)
        (run,) = runs(self.rule)
        self.assertEqual((run.occurrence_key, run.scheduled_at, run.event_id, run.event_revision),
                         (f'rule-{self.rule.pk}:event-{self.event.pk}:rev-1', self.due, self.event.pk, 1))
        self.assertEqual(run.broadcast.snapshot_subject, 'Reminder: Friday khutbah')
        self.assertIn(f'https://app.example.test/dawah/events/{self.event.pk}', run.broadcast.snapshot_text)
        self.assertIn('Friday 13 November 2026, 13:00 (Africa/Lagos)', run.broadcast.snapshot_text)
        self.assertEqual(automations.tick(self.due + timedelta(minutes=10)), 0)
        self.assertEqual(AutomationRun.objects.count(), 1)

    def test_grace_elapsed_and_started_events_are_missed_not_replayed(self):
        self.assertEqual(automations.tick(self.due + timedelta(minutes=15, seconds=1)), 0)
        self.assertEqual([(r.state, r.reason_code) for r in runs(self.rule)], [('missed', 'reminder_grace_elapsed')])
        short = rule(kind='event_reminder', owner=self.admin, offset_minutes=5)
        self.assertEqual(automations.tick(self.START), 0)
        self.assertEqual([(r.state, r.reason_code) for r in runs(short)], [('missed', 'reminder_grace_elapsed')])
        self.assertFalse(Broadcast.objects.exists())

    def test_reschedule_cancels_unsent_and_schedules_new_revision(self):
        automations.tick(self.due)
        (old,) = runs(self.rule)
        self.as_admin()
        new_start = self.START + timedelta(days=2)
        self.data(self.client.patch(f'/dawah/events/{self.event.pk}/', {
            'start_time': new_start.isoformat(), 'end_time': (new_start + timedelta(hours=1)).isoformat()},
            format='json'))
        self.event.refresh_from_db()
        self.assertEqual(self.event.schedule_revision, 2)
        old.refresh_from_db()
        self.assertEqual((old.state, old.reason_code), ('cancelled', 'source_rescheduled'))
        self.assertEqual(set(Delivery.objects.filter(broadcast=old.broadcast).values_list('state', 'error_code')),
                         {('cancelled', 'SOURCE_RESCHEDULED')})
        # A title-only edit is not a reschedule.
        self.data(self.client.patch(f'/dawah/events/{self.event.pk}/', {'title': 'Renamed'}, format='json'))
        self.event.refresh_from_db()
        self.assertEqual(self.event.schedule_revision, 2)
        self.assertEqual(automations.tick(new_start - timedelta(days=1)), 1)
        self.assertEqual([r.occurrence_key.rsplit(':', 1)[1] for r in runs(self.rule)], ['rev-1', 'rev-2'])

    def test_unpublish_cancels_before_handoff_but_never_rewrites_sent(self):
        automations.tick(self.due)
        (run,) = runs(self.rule)
        first, second = Delivery.objects.filter(broadcast=run.broadcast).order_by('id')
        Delivery.objects.filter(pk=first.pk).update(state='sent', provider_email_id='em_1', sent_at=timezone.now())
        self.as_admin()
        self.data(self.client.post(f'/dawah/events/{self.event.pk}/unpublish/'))
        run.refresh_from_db()
        self.assertEqual((run.state, run.reason_code), ('cancelled', 'source_unpublished'))
        self.assertEqual([d.state for d in Delivery.objects.filter(broadcast=run.broadcast).order_by('id')],
                         ['sent', 'cancelled'])

    def test_disable_stops_new_reminders(self):
        self.as_admin()
        self.data(self.client.post(f'{A}{self.rule.pk}/disable/', {'version': 1}, format='json'))
        self.assertEqual(automations.tick(self.due), 0)
        self.assertFalse(AutomationRun.objects.exists())


# ---------------------------------------------------------------- outbox integration

@MAIL_SETTINGS
class OutboxIntegrationTests(AutomationBase):
    def worker(self, transport):
        return outbox.OutboxWorker(outbox.OutboxConfig(send_timeout=30), transport=transport, limiter=None)

    def reminder_due_now(self):
        """A reminder whose occurrence is due at the real clock (the worker uses it)."""
        now = timezone.now()
        rule_obj = rule(kind='event_reminder', owner=self.admin, offset_minutes=60,
                        active_from=now - timedelta(hours=1))
        event = make_event(status=PUBLISHED, start=now + timedelta(minutes=55), category='jumuah')
        return rule_obj, event

    def test_worker_pass_materializes_and_delivers_through_outbox(self):
        rule_obj, event = self.reminder_due_now()
        transport = FakeTransport()
        summary = self.worker(transport).run_once()
        self.assertEqual((summary.materialized, summary.outcomes), (1, {'accepted': 2}))
        (run,) = runs(rule_obj)
        self.assertEqual(sorted(m.to for m in transport.calls), sorted(['student@example.test', 'staff@example.test']))
        self.assertTrue(all(k.startswith('test/broadcast/') for k in transport.keys()))
        state, reason = automations.run_state(run)
        self.assertEqual((state, reason), ('completed', None))
        # A second pass (restart) neither re-creates the occurrence nor resends.
        summary = self.worker(transport).run_once()
        self.assertEqual((summary.materialized, len(transport.calls)), (0, 2))

    def test_handoff_recheck_cancels_stale_reminder_and_keeps_ambiguity(self):
        rule_obj, event = self.reminder_due_now()
        automations.tick(timezone.now())
        (run,) = runs(rule_obj)
        first, second = Delivery.objects.filter(broadcast=run.broadcast).order_by('id')
        # The first delivery had an ambiguous same-key handoff and is waiting for replay.
        DeliveryAttempt.objects.create(delivery=first, generation=0, attempt=1, started_at=timezone.now(),
                                       finished_at=timezone.now(), outcome='uncertain', error_code='TIMEOUT')
        Delivery.objects.filter(pk=first.pk).update(attempt_count=1)
        # Revision bumped outside the service (no cancellation ran).
        Event.objects.filter(pk=event.pk).update(schedule_revision=F('schedule_revision') + 1)
        AutomationRule.objects.filter(pk=rule_obj.pk).update(enabled=False)  # no new-revision occurrence here
        transport = FakeTransport()
        self.worker(transport).run_once()
        self.assertEqual(transport.calls, [])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.state, first.error_code), ('uncertain', 'SOURCE_RESCHEDULED'))
        self.assertEqual((second.state, second.error_code), ('cancelled', 'SOURCE_RESCHEDULED'))
        run.refresh_from_db()
        self.assertEqual((run.state, run.reason_code), ('cancelled', 'source_rescheduled'))

    def test_failed_and_uncertain_runs_are_visible(self):
        rule_obj, event = self.reminder_due_now()
        failing = FakeTransport(SendResult.failed('VALIDATION_ERROR'), SendResult.failed('VALIDATION_ERROR'))
        self.worker(failing).run_once()
        self.assertEqual(automations.run_state(runs(rule_obj)[0]), ('failed', 'provider_failure'))

    def test_tick_logs_ids_only(self):
        rule_obj, event = self.reminder_due_now()
        with self.assertLogs('dawah.automations', level=logging.INFO) as logs:
            automations.tick(timezone.now())
        text = '\n'.join(logs.output)
        self.assertIn(f'rule={rule_obj.pk}', text)
        for secret in ('student@example.test', 'Friday khutbah', 'Reminder'):
            self.assertNotIn(secret, text)

    def test_one_failing_rule_does_not_stop_the_tick(self):
        good, event = self.reminder_due_now()
        bad = rule(kind='event_reminder', owner=self.admin, offset_minutes=60, active_from=good.active_from)
        real = automations._tick_reminders

        def flaky(rule_obj, now):
            if rule_obj.pk == bad.pk:
                raise RuntimeError('boom')
            return real(rule_obj, now)

        with mock.patch.object(automations, '_tick_reminders', side_effect=flaky), \
                self.assertLogs('dawah.automations', level=logging.ERROR):
            self.assertEqual(automations.tick(timezone.now()), 1)


class EventResourceVisibilityTests(TestCase):
    def test_only_visible_resources_are_linked(self):
        event = make_event(status=PUBLISHED, start=MONDAY_8_LAGOS - timedelta(days=1))
        make_resource(event, status=PublicationStatus.DRAFT, type='vid', youtube_video_id='dQw4w9WgXcQ')
        self.assertEqual(automations._resource_links([event]), {})
        self.assertEqual(EventResource.objects.count(), 1)
