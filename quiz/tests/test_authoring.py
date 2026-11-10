"""ASSESS-03 admin authoring, publication, metadata correction and closing API (PostgreSQL)."""
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from dawah.models import Event
from quiz import authoring, lifecycle
from quiz.errors import QuizUnavailable
from quiz.models import Attempt, Option, Question, Quiz, Submission

from .helpers import KEY_FIELDS, QuizAPITestMixin, make_quiz, walk_keys


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def option_bodies(correct=1, texts='ABCD'):
    return [{'text': text, 'order': index + 1, 'is_correct': index == correct}
            for index, text in enumerate(texts)]


def question_body(**overrides):
    body = {'type': 'mcq', 'text': 'Question', 'points': 2, 'order': 1,
            'options': option_bodies()}
    body.update(overrides)
    return body


def future(days=7):
    return timezone.now() + timedelta(days=days)


class AuthoringTestCase(QuizAPITestMixin, TestCase):

    def as_admin(self, method, path, body=None):
        return self.call(self.admin, method, path, body)

    def create_draft(self, **overrides):
        body = {'title': 'Arabic week 1', 'description': 'Weekly assessment', 'time_limit': 30,
                'deadline': iso(future()), 'related_event': None}
        body.update(overrides)
        response = self.as_admin('post', '/quiz/quizzes/', body)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['data']

    def add_question(self, quiz_id, **overrides):
        response = self.as_admin('post', f'/quiz/quizzes/{quiz_id}/questions/',
                              question_body(**overrides))
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['data']

    def assertFields(self, response, *paths, status_code=400, code='VALIDATION_ERROR'):
        body = self.assertEnvelopeError(response, status_code, code)
        fields = body['data'].get('fields', {})
        for path in paths:
            self.assertIn(path, fields, body)
        # Every field value is a flat list of strings (the admin UI joins them).
        for messages in fields.values():
            self.assertIsInstance(messages, list)
            self.assertTrue(all(isinstance(message, str) for message in messages), fields)
        return fields


class QuizDraftAPITests(AuthoringTestCase):

    def test_create_draft_contract(self):
        data = self.create_draft()
        self.assertEqual(data['status'], 'draft')
        self.assertEqual(data['type'], 'arabic')
        self.assertIs(data['is_active'], False)
        self.assertEqual((data['questions'], data['question_count'], data['total_points']),
                         ([], 0, 0))
        self.assertIs(data['content_editable'], True)
        self.assertIsNone(data['published_at'])
        self.assertTrue(data['deadline'].endswith('Z'))
        self.assertTrue(data['created_at'].endswith('Z'))
        quiz = Quiz.objects.get(pk=data['id'])
        self.assertEqual((quiz.title, quiz.time_limit, quiz.status), ('Arabic week 1', 30, 'draft'))

    def test_draft_may_omit_timing_and_description(self):
        data = self.create_draft(time_limit=None, deadline=None, description='')
        self.assertEqual((data['time_limit'], data['deadline'], data['description']),
                         (None, None, ''))
        response = self.as_admin('post', '/quiz/quizzes/', {'title': 'Minimal'})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['data']['description'], '')

    def test_create_validation_matrix(self):
        event = Event.objects.create(title='E', category='c', tags='t', speakers='s',
                                     location='l', start_time=timezone.now(),
                                     end_time=timezone.now() + timedelta(hours=1))
        cases = {
            'missing title': ({'title': None}, 'title'),
            'blank title': ({'title': '   '}, 'title'),
            'long title': ({'title': 'x' * 251}, 'title'),
            'zero time limit': ({'time_limit': 0}, 'time_limit'),
            'long time limit': ({'time_limit': 181}, 'time_limit'),
            'string time limit': ({'time_limit': '30'}, 'time_limit'),
            'float time limit': ({'time_limit': 30.0}, 'time_limit'),
            'bad deadline': ({'deadline': '12-10-2026'}, 'deadline'),
            'unknown event': ({'related_event': 999999}, 'related_event'),
            'string event': ({'related_event': str(event.pk)}, 'related_event'),
            'boolean event': ({'related_event': True}, 'related_event'),
            'unknown key': ({'questions': []}, 'questions'),
            'read-only key': ({'published_at': iso(future())}, 'published_at'),
            'id key': ({'id': 5}, 'id'),
            'status change': ({'status': 'published'}, 'status'),
            'is_active change': ({'is_active': True}, 'is_active'),
        }
        base = {'title': 'T', 'description': 'd', 'time_limit': 30, 'deadline': iso(future())}
        before = Quiz.objects.count()
        for name, (change, path) in cases.items():
            body = {**base, **change}
            if change.get('title', '') is None:
                body.pop('title')
            with self.subTest(name):
                self.assertFields(self.as_admin('post', '/quiz/quizzes/', body), path)
        self.assertEqual(Quiz.objects.count(), before)
        linked = self.create_draft(related_event=event.pk)
        self.assertEqual((linked['related_event'], linked['event_title']), (event.pk, 'E'))

    def test_patch_draft_full_allowed_set_and_partial(self):
        data = self.create_draft()
        path = f"/quiz/quizzes/{data['id']}/"
        event = Event.objects.create(title='Lecture', category='c', tags='t', speakers='s',
                                     location='l', start_time=timezone.now(),
                                     end_time=timezone.now() + timedelta(hours=1))
        deadline = iso(future(14))
        response = self.as_admin('patch', path, {
            'title': 'Renamed', 'description': 'New', 'time_limit': 45, 'deadline': deadline,
            'related_event': event.pk})
        self.assertEqual(response.status_code, 200, response.content)
        updated = response.json()['data']
        self.assertEqual((updated['title'], updated['description'], updated['time_limit'],
                          updated['deadline'], updated['related_event'], updated['event_title']),
                         ('Renamed', 'New', 45, deadline, event.pk, 'Lecture'))
        # Partial: omitted keys stay as they are.
        response = self.as_admin('patch', path, {'time_limit': 60})
        self.assertEqual(response.status_code, 200, response.content)
        quiz = Quiz.objects.get(pk=data['id'])
        self.assertEqual((quiz.title, quiz.time_limit, quiz.related_event_id),
                         ('Renamed', 60, event.pk))
        # Clearing timing and the event is allowed while drafting.
        response = self.as_admin('patch', path, {'time_limit': None, 'deadline': None,
                                              'related_event': None})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFields(self.as_admin('patch', path, {'title': ''}), 'title')
        self.assertFields(self.as_admin('patch', path, {'status': 'closed'}), 'status')
        self.assertFields(self.as_admin('patch', path, {'questions': []}), 'questions')

    def test_delete_draft_and_missing(self):
        data = self.create_draft()
        self.add_question(data['id'])
        response = self.as_admin('delete', f"/quiz/quizzes/{data['id']}/")
        self.assertEqual((response.status_code, response.content), (204, b''))
        self.assertFalse(Quiz.objects.filter(pk=data['id']).exists())
        self.assertFalse(Question.objects.filter(quiz_id=data['id']).exists())
        self.assertEnvelopeError(self.as_admin('delete', f"/quiz/quizzes/{data['id']}/"), 404,
                                 'NOT_FOUND')
        self.assertEnvelopeError(self.as_admin('patch', '/quiz/quizzes/999999/', {'title': 'x'}),
                                 404, 'NOT_FOUND')


class QuestionAuthoringAPITests(AuthoringTestCase):

    def setUp(self):
        super().setUp()
        self.quiz_id = self.create_draft()['id']
        self.path = f'/quiz/quizzes/{self.quiz_id}/questions/'

    def test_create_question_contract(self):
        data = self.add_question(self.quiz_id)
        self.assertEqual(set(data), {'id', 'quiz', 'type', 'text', 'points', 'order', 'options'})
        self.assertEqual((data['quiz'], data['type'], data['points'], data['order']),
                         (self.quiz_id, 'mcq', 2, 1))
        self.assertEqual([(o['text'], o['order'], o['is_correct']) for o in data['options']],
                         [('A', 1, False), ('B', 2, True), ('C', 3, False), ('D', 4, False)])
        self.assertTrue(all(isinstance(o['id'], int) for o in data['options']))
        detail = self.as_admin('get', f'/quiz/quizzes/{self.quiz_id}/').json()['data']
        self.assertEqual(detail['questions'], [data])
        self.assertEqual((detail['question_count'], detail['total_points']), (1, 2))

    def test_defaults(self):
        body = question_body()
        for key in ('type', 'points', 'order'):
            body.pop(key)
        body['options'] = [{'text': 'A', 'is_correct': True, 'order': 3}, {'text': 'B'},
                           {'text': 'C'}, {'text': 'D', 'order': 1}]
        response = self.as_admin('post', self.path, body)
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()['data']
        self.assertEqual((data['type'], data['points'], data['order']), ('mcq', 1, 1))
        self.assertEqual([(o['text'], o['order']) for o in data['options']],
                         [('D', 1), ('B', 2), ('A', 3), ('C', 4)])
        second = self.as_admin('post', self.path, {**body, 'options': option_bodies()})
        self.assertEqual(second.json()['data']['order'], 2)  # appended

    def test_question_validation_matrix(self):
        self.add_question(self.quiz_id, order=1)
        options = option_bodies()
        cases = {
            'missing options': ({'options': None}, 'options'),
            'three options': ({'options': options[:3]}, 'options'),
            'five options': ({'options': options + [{'text': 'E', 'order': 5,
                                                     'is_correct': False}]}, 'options'),
            'no correct': ({'options': option_bodies(correct=-1)}, 'options'),
            'two correct': ({'options': [*options[:3], {**options[3], 'is_correct': True}]},
                            'options'),
            'case duplicate': ({'options': option_bodies(texts=['A', 'b', 'B', 'D'])}, 'options'),
            'whitespace duplicate': ({'options': option_bodies(
                texts=['salam  alaykum', ' Salam alaykum ', 'C', 'D'])}, 'options'),
            'blank option': ({'options': option_bodies(texts=['A', '   ', 'C', 'D'])},
                             'options.1.text'),
            'long option': ({'options': option_bodies(texts=['A', 'x' * 251, 'C', 'D'])},
                            'options.1.text'),
            'string is_correct': ({'options': [*options[:3], {**options[3],
                                                             'is_correct': 'true'}]},
                                  'options.3.is_correct'),
            'option id': ({'options': [{**options[0], 'id': 5}, *options[1:]]}, 'options.0.id'),
            'duplicate option order': ({'options': [{**o, 'order': 1} for o in options]},
                                       'options'),
            'zero option order': ({'options': [{**options[0], 'order': 0}, *options[1:]]},
                                  'options.0.order'),
            'options not a list': ({'options': {'text': 'A'}}, 'options'),
            'option not an object': ({'options': ['A', 'B', 'C', 'D']}, 'options.0'),
            'zero points': ({'points': 0}, 'points'),
            'negative points': ({'points': -1}, 'points'),
            'string points': ({'points': '2'}, 'points'),
            'float points': ({'points': 2.5}, 'points'),
            'boolean points': ({'points': True}, 'points'),
            'huge points': ({'points': 32768}, 'points'),
            'zero order': ({'order': 0}, 'order'),
            'string order': ({'order': '2'}, 'order'),
            'taken order': ({'order': 1}, 'order'),
            'text type': ({'type': 'text'}, 'type'),
            'blank text': ({'text': '  '}, 'text'),
            'missing text': ({'text': None}, 'text'),
            'quiz key': ({'quiz': self.quiz_id}, 'quiz'),
            'unknown key': ({'is_active': True}, 'is_active'),
        }
        before = (Question.objects.count(), Option.objects.count())
        for name, (change, path) in cases.items():
            body = {**question_body(order=2), **change}
            for key, value in change.items():
                if value is None:
                    body.pop(key)
            with self.subTest(name):
                self.assertFields(self.as_admin('post', self.path, body), path)
        self.assertEqual((Question.objects.count(), Option.objects.count()), before)

    def test_patch_replaces_options_and_keeps_ids_by_position(self):
        created = self.add_question(self.quiz_id)
        ids = [o['id'] for o in created['options']]
        path = f"/quiz/question/{created['id']}/"
        response = self.as_admin('patch', path, question_body(
            text='Edited', points=5, options=option_bodies(correct=3, texts='WXYZ')))
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertEqual((data['text'], data['points']), ('Edited', 5))
        self.assertEqual([(o['id'], o['text'], o['is_correct']) for o in data['options']],
                         [(ids[0], 'W', False), (ids[1], 'X', False), (ids[2], 'Y', False),
                          (ids[3], 'Z', True)])
        self.assertEqual(Option.objects.filter(question_id=created['id']).count(), 4)
        # Partial PATCH keeps the options; reordering options swaps positions.
        response = self.as_admin('patch', path, {'points': 3})
        self.assertEqual(response.json()['data']['options'], data['options'])
        response = self.as_admin('patch', path, {'options': [
            {'text': 'W', 'order': 4, 'is_correct': False},
            {'text': 'X', 'order': 3, 'is_correct': True},
            {'text': 'Y', 'order': 2, 'is_correct': False},
            {'text': 'Z', 'order': 1, 'is_correct': False}]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([o['text'] for o in response.json()['data']['options']], list('ZYXW'))
        # Nested options stay fully validated in a PATCH.
        self.assertFields(self.as_admin('patch', path, {'options': [{'order': 1}] * 4}),
                          'options.0.text')
        self.assertFields(self.as_admin('patch', path, {'options': option_bodies()[:2]}), 'options')

    def test_question_order_clash_on_patch(self):
        first = self.add_question(self.quiz_id, order=1)
        second = self.add_question(self.quiz_id, order=2, text='Second')
        self.assertFields(self.as_admin('patch', f"/quiz/question/{second['id']}/", {'order': 1}),
                          'order')
        response = self.as_admin('patch', f"/quiz/question/{first['id']}/", {'order': 3})
        self.assertEqual(response.status_code, 200, response.content)
        response = self.as_admin('patch', f"/quiz/question/{second['id']}/", {'order': 1})
        self.assertEqual(response.status_code, 200, response.content)
        detail = self.as_admin('get', f'/quiz/quizzes/{self.quiz_id}/').json()['data']
        self.assertEqual([q['id'] for q in detail['questions']], [second['id'], first['id']])

    def test_question_cannot_move_or_be_converted(self):
        created = self.add_question(self.quiz_id)
        other = self.create_draft(title='Other')['id']
        for path in (f"/quiz/question/{created['id']}/", f"/quiz/question/{created['id']}"):
            with self.subTest(path=path):
                self.assertFields(self.as_admin('put', path, question_body(quiz=other)), 'quiz')
                self.assertFields(self.as_admin('patch', path, {'quiz': other}), 'quiz')
        self.assertEqual(Question.objects.get(pk=created['id']).quiz_id, self.quiz_id)
        legacy = Question.objects.create(quiz_id=self.quiz_id, type=Question.Type.text,
                                         text='Legacy essay', points=1)
        self.assertFields(self.as_admin('patch', f'/quiz/question/{legacy.pk}/',
                                     question_body(order=9)), 'type')

    def test_delete_question(self):
        created = self.add_question(self.quiz_id)
        response = self.as_admin('delete', f"/quiz/question/{created['id']}/")
        self.assertEqual((response.status_code, response.content), (204, b''))
        self.assertFalse(Option.objects.filter(question_id=created['id']).exists())
        self.assertEnvelopeError(self.as_admin('delete', f"/quiz/question/{created['id']}/"), 404,
                                 'NOT_FOUND')

    def test_missing_quiz_is_404(self):
        self.assertEnvelopeError(self.as_admin('post', '/quiz/quizzes/999999/questions/',
                                            question_body()), 404, 'NOT_FOUND')

    def test_nested_writes_are_atomic(self):
        with mock.patch.object(Option.objects, 'bulk_create', side_effect=RuntimeError('boom')), \
                self.assertRaises(RuntimeError):
            authoring.create_question(self.quiz_id, {
                'type': 'mcq', 'text': 'Half', 'points': 1,
                'options': [{'text': t, 'is_correct': t == 'A', 'order': i + 1}
                            for i, t in enumerate('ABCD')]})
        self.assertFalse(Question.objects.filter(text='Half').exists())
        created = self.add_question(self.quiz_id)
        with mock.patch.object(authoring, '_replace_options', side_effect=RuntimeError('boom')), \
                self.assertRaises(RuntimeError):
            authoring.update_question(created['id'], {'text': 'Changed', 'options': []})
        self.assertEqual(Question.objects.get(pk=created['id']).text, 'Question')


class LegacyAliasTests(AuthoringTestCase):
    """Old routes run the same rules: they cannot bypass validation or the lifecycle."""

    def setUp(self):
        super().setUp()
        self.published, self.questions = make_quiz('Published', points=(2,))
        self.draft_id = self.create_draft()['id']

    def test_legacy_question_create_uses_the_same_rules(self):
        body = question_body(quiz=self.draft_id)
        response = self.as_admin('post', '/quiz/question/', body)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['data']['quiz'], self.draft_id)
        self.assertFields(self.as_admin('post', '/quiz/question/', question_body()), 'quiz')
        self.assertFields(self.as_admin('post', '/quiz/question/', {
            **body, 'order': 2, 'options': option_bodies()[:3]}), 'options')
        self.assertFields(self.as_admin('post', '/quiz/question/', {**body, 'quiz': '1'}), 'quiz')
        self.assertEnvelopeError(self.as_admin('post', '/quiz/question/', question_body(
            quiz=self.published.pk, order=5)), 409, 'QUIZ_IMMUTABLE')

    def test_aliases_cannot_edit_published_content(self):
        question = self.questions[0][0]
        before = list(Option.objects.filter(question=question).values_list('text', 'is_correct'))
        for path in (f'/quiz/question/{question.pk}/', f'/quiz/question/{question.pk}'):
            for method in ('put', 'patch'):
                with self.subTest(path=path, method=method):
                    self.assertEnvelopeError(self.as_admin(method, path, question_body(
                        quiz=self.published.pk, options=option_bodies(correct=0))), 409,
                        'QUIZ_IMMUTABLE')
            self.assertEnvelopeError(self.as_admin('delete', path), 409, 'QUIZ_IMMUTABLE')
        for path in (f'/quiz/quizzes/{self.published.pk}/', f'/quiz/quizzes/{self.published.pk}'):
            for method in ('put', 'patch'):
                with self.subTest(path=path, method=method):
                    body = self.assertEnvelopeError(self.as_admin(method, path, {
                        'title': 'x', 'time_limit': 99}), 409, 'QUIZ_IMMUTABLE')
                    self.assertEqual(set(body['data']['fields']), {'time_limit'})
                    self.assertFields(self.as_admin(method, path, {'title': 'x', 'status': 'draft'}),
                                      'status')
        question.refresh_from_db()
        self.assertEqual(question.points, 2)
        self.assertEqual(list(Option.objects.filter(question=question).values_list(
            'text', 'is_correct')), before)
        self.published.refresh_from_db()
        self.assertEqual((self.published.title, self.published.time_limit, self.published.status),
                         ('Published', 30, 'published'))

    def test_legacy_question_list_is_admin_only_and_paginated(self):
        self.assertEnvelopeError(self.call(self.student, 'get', '/quiz/question/'), 403,
                                 'PERMISSION_DENIED')
        data = self.as_admin('get', '/quiz/question/?page_size=1').json()['data']
        self.assertEqual((data['count'], len(data['results'])), (Question.objects.count(), 1))


class PublicationAPITests(AuthoringTestCase):

    def ready_draft(self, **overrides):
        quiz_id = self.create_draft(**overrides)['id']
        self.add_question(quiz_id, order=1, points=2)
        self.add_question(quiz_id, order=2, points=3, text='Second')
        return quiz_id

    def test_publish_success(self):
        quiz_id = self.ready_draft()
        response = self.as_admin('post', f'/quiz/quizzes/{quiz_id}/publish/')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['message'], 'Quiz published.')
        data = body['data']
        self.assertEqual((data['status'], data['is_active'], data['content_editable']),
                         ('published', True, False))
        self.assertTrue(data['published_at'].endswith('Z'))
        self.assertEqual((data['question_count'], data['total_points']), (2, 5))
        quiz = Quiz.objects.get(pk=quiz_id)
        self.assertEqual(quiz.status, 'published')
        # Students now see it, without keys.
        listed = self.call(self.student, 'get', '/quiz/quizzes/').json()['data']['results']
        self.assertEqual([row['id'] for row in listed], [quiz_id])
        self.assertEqual(listed[0]['total_points'], 5)

    def test_publish_from_non_draft_is_409(self):
        quiz_id = self.ready_draft()
        self.as_admin('post', f'/quiz/quizzes/{quiz_id}/publish/')
        published_at = Quiz.objects.get(pk=quiz_id).published_at
        self.assertEnvelopeError(self.as_admin('post', f'/quiz/quizzes/{quiz_id}/publish/'), 409,
                                 'INVALID_TRANSITION')
        self.assertEqual(Quiz.objects.get(pk=quiz_id).published_at, published_at)
        self.as_admin('post', f'/quiz/quizzes/{quiz_id}/close/')
        self.assertEnvelopeError(self.as_admin('post', f'/quiz/quizzes/{quiz_id}/publish/'), 409,
                                 'INVALID_TRANSITION')
        self.assertEqual(Quiz.objects.get(pk=quiz_id).status, 'closed')

    def test_publish_validation_errors_use_field_paths(self):
        empty = self.create_draft(time_limit=None, deadline=None)['id']
        fields = self.assertFields(self.as_admin('post', f'/quiz/quizzes/{empty}/publish/'),
                                   'time_limit', 'deadline', 'questions')
        self.assertEqual(fields['questions'], ['A quiz needs at least one question to publish.'])
        past = self.ready_draft(deadline=iso(timezone.now() - timedelta(minutes=1)))
        self.assertFields(self.as_admin('post', f'/quiz/quizzes/{past}/publish/'), 'deadline')
        # A graph that only a pre-release or raw path can produce still blocks publication.
        broken = self.ready_draft()
        legacy = Question.objects.create(quiz_id=broken, type=Question.Type.text, text='Essay',
                                         points=1)
        three = Question.objects.create(quiz_id=broken, type=Question.Type.mcq, text='Three',
                                        points=1)
        for index, text in enumerate('ABC'):
            Option.objects.create(question=three, text=text, is_correct=index == 0)
        fields = self.assertFields(self.as_admin('post', f'/quiz/quizzes/{broken}/publish/'),
                                   f'questions.{legacy.pk}', f'questions.{three.pk}')
        self.assertIn('Exactly 4 options are required.', fields[f'questions.{three.pk}'])
        self.assertEqual(Quiz.objects.filter(status='draft', pk__in=[empty, past, broken]).count(),
                         3)

    def test_publish_and_close_take_no_body(self):
        quiz_id = self.ready_draft()
        for action in ('publish', 'close'):
            with self.subTest(action):
                self.assertFields(self.as_admin('post', f'/quiz/quizzes/{quiz_id}/{action}/',
                                             {'status': 'published'}), 'status')
        self.assertEqual(Quiz.objects.get(pk=quiz_id).status, 'draft')
        response = self.as_admin('post', f'/quiz/quizzes/{quiz_id}/publish/', {})
        self.assertEqual(response.status_code, 200, response.content)

    def test_close_transitions_and_idempotency(self):
        quiz_id = self.ready_draft()
        self.assertEnvelopeError(self.as_admin('post', f'/quiz/quizzes/{quiz_id}/close/'), 409,
                                 'INVALID_TRANSITION')
        self.as_admin('post', f'/quiz/quizzes/{quiz_id}/publish/')
        response = self.as_admin('post', f'/quiz/quizzes/{quiz_id}/close/')
        self.assertEqual(response.status_code, 200, response.content)
        first = response.json()
        self.assertEqual((first['message'], first['data']['status'], first['data']['is_active']),
                         ('Quiz closed.', 'closed', False))
        again = self.as_admin('post', f'/quiz/quizzes/{quiz_id}/close/')
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again.json()['message'], 'Quiz already closed.')
        self.assertEqual(again.json()['data']['closed_at'], first['data']['closed_at'])
        listed = self.call(self.student, 'get', '/quiz/quizzes/').json()['data']
        self.assertEqual(listed['count'], 0)
        self.assertEnvelopeError(self.as_admin('post', '/quiz/quizzes/999999/close/'), 404,
                                 'NOT_FOUND')

    def test_close_preserves_existing_attempt_expiry(self):
        quiz, questions = make_quiz('Running', points=(2,))
        (question, correct, _), = questions
        attempt, _ = lifecycle.start_attempt(quiz.pk, self.student)
        response = self.as_admin('post', f'/quiz/quizzes/{quiz.pk}/close/')
        self.assertEqual(response.status_code, 200, response.content)
        refreshed = Attempt.objects.get(pk=attempt.pk)
        self.assertEqual((refreshed.expires_at, refreshed.status), (attempt.expires_at,
                                                                    'in_progress'))
        submission, created = lifecycle.finalize_attempt(attempt.pk, self.student,
                                                         {str(question.pk): correct.pk})
        self.assertTrue(created)
        self.assertEqual(submission.score, 2)
        with self.assertRaises(QuizUnavailable):  # no new attempts after closing
            lifecycle.start_attempt(quiz.pk, self.other_student)


class MetadataCorrectionTests(AuthoringTestCase):

    def setUp(self):
        super().setUp()
        self.quiz, questions = make_quiz('Original title', points=(2, 3))
        (q1, c1, _), (q2, _, w2) = questions
        self.submission = self.submission(self.quiz, self.student,
                                          {str(q1.pk): c1.pk, str(q2.pk): w2[0].pk})
        self.snapshot = Submission.objects.get(pk=self.submission.pk).grading_snapshot
        self.path = f'/quiz/quizzes/{self.quiz.pk}/'

    def test_title_and_description_corrections_after_publish_and_close(self):
        response = self.as_admin('patch', self.path, {'title': 'Corrected', 'description': 'Fixed'})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertEqual((data['title'], data['description'], data['status']),
                         ('Corrected', 'Fixed', 'published'))
        lifecycle.close_quiz(self.quiz.pk)
        response = self.as_admin('patch', self.path, {'description': 'After close'})
        self.assertEqual(response.status_code, 200, response.content)
        stored = Submission.objects.get(pk=self.submission.pk)
        self.assertEqual(stored.grading_snapshot, self.snapshot)
        self.assertEqual(stored.grading_snapshot['quiz_title'], 'Original title')
        self.assertEqual((stored.score, stored.total_points), (2, 5))
        quiz = Quiz.objects.get(pk=self.quiz.pk)
        self.assertEqual((quiz.title, quiz.description, quiz.time_limit, quiz.deadline),
                         ('Corrected', 'After close', self.quiz.time_limit, self.quiz.deadline))

    def test_grading_and_timing_fields_are_409(self):
        event = Event.objects.create(title='E', category='c', tags='t', speakers='s',
                                     location='l', start_time=timezone.now(),
                                     end_time=timezone.now() + timedelta(hours=1))
        cases = {
            'time_limit': 60,
            'deadline': iso(future(30)),
            'related_event': event.pk,
            'type': 'general',
        }
        for name, value in cases.items():
            with self.subTest(name):
                body = self.assertEnvelopeError(
                    self.as_admin('patch', self.path, {'title': 'Sneaky', name: value}), 409,
                    'QUIZ_IMMUTABLE')
                self.assertEqual(set(body['data']['fields']), {name})
        quiz = Quiz.objects.get(pk=self.quiz.pk)
        self.assertEqual((quiz.title, quiz.time_limit, quiz.deadline, quiz.related_event_id),
                         ('Original title', 30, self.quiz.deadline, None))
        # Echoing an unchanged value is not a change.
        response = self.as_admin('patch', self.path, {'title': 'Same timing', 'time_limit': 30,
                                                   'status': 'published', 'is_active': True})
        self.assertEqual(response.status_code, 200, response.content)

    def test_published_question_content_is_409(self):
        question = self.quiz.question_set.order_by('order').first()
        path = f'/quiz/quizzes/{self.quiz.pk}/questions/'
        self.assertEnvelopeError(self.as_admin('post', path, question_body(order=9)), 409,
                                 'QUIZ_IMMUTABLE')
        for method, body in (('patch', {'points': 9}), ('patch', {'text': 'Changed'}),
                             ('patch', {'order': 7}),
                             ('patch', {'options': option_bodies(correct=0)}),
                             ('delete', None)):
            with self.subTest(method=method, body=body):
                self.assertEnvelopeError(self.as_admin(method, f'/quiz/question/{question.pk}/',
                                                    body), 409, 'QUIZ_IMMUTABLE')
        question.refresh_from_db()
        self.assertEqual((question.points, question.order), (2, 1))
        self.assertEqual(Question.objects.filter(quiz=self.quiz).count(), 2)
        self.assertEnvelopeError(self.as_admin('delete', self.path), 409, 'RESOURCE_REFERENCED')


class ContentLockWithHistoryTests(AuthoringTestCase):
    """A draft with any attempt/submission (legacy included) has frozen content (M4)."""

    def setUp(self):
        super().setUp()
        self.quiz_id = self.create_draft()['id']
        self.question = self.add_question(self.quiz_id)
        # A legacy submission (migrated history) on a draft: only the migration
        # can create one, so insert it with the history trigger off in this
        # (rolled back) test transaction.
        with connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
            cursor.execute('ALTER TABLE quiz_submission DISABLE TRIGGER quiz_submission_history_guard')
            cursor.execute("INSERT INTO quiz_submission (quiz_id, student_id, answers, score, "
                           "is_legacy, submitted_at) VALUES (%s, %s, '{}', 1, true, now())",
                           [self.quiz_id, self.student.pk])
            cursor.execute('ALTER TABLE quiz_submission ENABLE TRIGGER quiz_submission_history_guard')
            cursor.execute('SET CONSTRAINTS ALL DEFERRED')

    def test_content_writes_are_409_but_metadata_and_publication_work(self):
        detail = self.as_admin('get', f'/quiz/quizzes/{self.quiz_id}/').json()['data']
        self.assertEqual((detail['status'], detail['content_editable']), ('draft', False))
        path = f"/quiz/question/{self.question['id']}/"
        self.assertEnvelopeError(self.as_admin('post', f'/quiz/quizzes/{self.quiz_id}/questions/',
                                            question_body(order=2)), 409, 'QUIZ_IMMUTABLE')
        self.assertEnvelopeError(self.as_admin('patch', path, {'points': 5}), 409, 'QUIZ_IMMUTABLE')
        self.assertEnvelopeError(self.as_admin('delete', path), 409, 'QUIZ_IMMUTABLE')
        self.assertEnvelopeError(self.as_admin('delete', f'/quiz/quizzes/{self.quiz_id}/'), 409,
                                 'RESOURCE_REFERENCED')
        response = self.as_admin('patch', f'/quiz/quizzes/{self.quiz_id}/',
                              {'title': 'Renamed', 'time_limit': 40})
        self.assertEqual(response.status_code, 200, response.content)
        response = self.as_admin('post', f'/quiz/quizzes/{self.quiz_id}/publish/')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Question.objects.get(pk=self.question['id']).points, 2)


class ListPaginationTests(AuthoringTestCase):

    def setUp(self):
        super().setUp()
        self.drafts = [Quiz.objects.create(title=f'Draft {i}', description='d') for i in range(23)]
        self.published = [make_quiz(f'Live {i}', points=(1, 2))[0] for i in range(3)]
        self.closed = make_quiz('Closed', points=(1,))[0]
        lifecycle.close_quiz(self.closed.pk)
        # Published while its deadline was ahead; that deadline has now passed.
        self.expired = make_quiz('Expired', points=(1,),
                                 deadline=timezone.now() - timedelta(minutes=1),
                                 published_now=timezone.now() - timedelta(minutes=2))[0]
        self.all_ids = sorted(Quiz.objects.values_list('pk', flat=True), reverse=True)

    def page(self, user, query=''):
        response = self.call(user, 'get', f'/quiz/quizzes/{query}')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def test_admin_pagination_order_and_filter(self):
        first = self.page(self.admin)
        self.assertEqual(first['count'], len(self.all_ids))
        self.assertEqual([row['id'] for row in first['results']], self.all_ids[:20])
        self.assertIsNone(first['previous'])
        self.assertIn('page=2', first['next'])
        second = self.page(self.admin, '?page=2')
        self.assertEqual([row['id'] for row in second['results']], self.all_ids[20:])
        self.assertEqual(len(self.page(self.admin, '?page_size=500')['results']),
                         len(self.all_ids))  # under the 100 maximum
        small = self.page(self.admin, '?page_size=5&page=2')
        self.assertEqual([row['id'] for row in small['results']], self.all_ids[5:10])
        drafts = self.page(self.admin, '?status=draft&page_size=100')
        self.assertEqual(drafts['count'], 23)
        self.assertEqual({row['status'] for row in drafts['results']}, {'draft'})
        published = self.page(self.admin, '?status=published')
        self.assertEqual(published['count'], 4)
        self.assertEqual(self.page(self.admin, '?status=closed')['count'], 1)
        row = published['results'][-1]
        self.assertEqual((row['question_count'], row['total_points']), (2, 3))
        for key in ('status', 'published_at', 'created_at', 'updated_at', 'related_event'):
            self.assertIn(key, row)
        self.assertNotIn('questions', row)
        self.assertFields(self.call(self.admin, 'get', '/quiz/quizzes/?status=live'), 'status')
        self.assertEnvelopeError(self.call(self.admin, 'get', '/quiz/quizzes/?page=99'), 404,
                                 'NOT_FOUND')

    def test_max_page_size_is_100(self):
        Quiz.objects.bulk_create([Quiz(title=f'Bulk {i}', description='d') for i in range(90)])
        data = self.page(self.admin, '?page_size=999')
        self.assertEqual(len(data['results']), 100)

    def test_student_list_is_available_quizzes_only(self):
        data = self.page(self.student)
        ids = [row['id'] for row in data['results']]
        self.assertEqual(ids, sorted(q.pk for q in self.published))
        self.assertEqual(data['count'], 3)
        self.assertEqual(self.page(self.student, '?status=draft')['count'], 0)
        self.assertNoAnswerKeys(self.call(self.student, 'get', '/quiz/quizzes/'))


class PermissionTests(AuthoringTestCase):

    def setUp(self):
        super().setUp()
        self.draft, draft_questions = make_quiz('Draft', active=False, points=(1,))
        self.draft_question = draft_questions[0][0]
        self.live, _ = make_quiz('Live', points=(1,))

    def requests(self):
        quiz, question = self.draft.pk, self.draft_question.pk
        return [
            ('get', '/quiz/quizzes/?status=draft', None),
            ('post', '/quiz/quizzes/', {'title': 'x'}),
            ('patch', f'/quiz/quizzes/{quiz}/', {'title': 'x'}),
            ('patch', f'/quiz/quizzes/{quiz}', {'title': 'x'}),
            ('put', f'/quiz/quizzes/{quiz}/', {'title': 'x'}),
            ('delete', f'/quiz/quizzes/{quiz}/', None),
            ('post', f'/quiz/quizzes/{quiz}/questions/', question_body(order=5)),
            ('post', '/quiz/question/', question_body(quiz=quiz, order=6)),
            ('get', '/quiz/question/', None),
            ('patch', f'/quiz/question/{question}/', {'points': 3}),
            ('patch', f'/quiz/question/{question}', {'points': 3}),
            ('delete', f'/quiz/question/{question}/', None),
            ('post', f'/quiz/quizzes/{quiz}/publish/', None),
            ('post', f'/quiz/quizzes/{self.live.pk}/close/', None),
        ]

    def state(self):
        return (list(Quiz.objects.order_by('pk').values_list('pk', 'title', 'status')),
                list(Question.objects.order_by('pk').values_list('pk', 'points')),
                Option.objects.count())

    def test_non_admins_cannot_author(self):
        before = self.state()
        users = {'anonymous': (None, 401, 'AUTHENTICATION_REQUIRED'),
                 'student': (self.student, 403, 'PERMISSION_DENIED'),
                 'staff student': (self.staff_student, 403, 'PERMISSION_DENIED'),
                 'inactive admin': (self.inactive_admin, 403, None)}
        for label, (user, status_code, code) in users.items():
            for method, path, body in self.requests():
                if label in ('student', 'staff student') and method == 'get' and \
                        path.startswith('/quiz/quizzes/'):
                    continue  # students list available quizzes (filtered below)
                with self.subTest(user=label, method=method, path=path):
                    response = self.call(user, method, path, body)
                    self.assertEqual(response.status_code, status_code, response.content)
                    if code:
                        self.assertEqual(response.json()['data']['code'], code)
        self.assertEqual(self.state(), before)
        listed = self.call(self.student, 'get', '/quiz/quizzes/?status=draft').json()['data']
        self.assertEqual(listed['count'], 0)
        # Students cannot see a draft quiz or its question at all.
        for path in (f'/quiz/quizzes/{self.draft.pk}/', f'/quiz/question/{self.draft_question.pk}/'):
            self.assertEnvelopeError(self.call(self.student, 'get', path), 404, 'NOT_FOUND')

    def test_admin_can_do_everything(self):
        for method, path, body in self.requests():
            if method == 'delete':
                continue
            with self.subTest(method=method, path=path):
                response = self.call(self.admin, method, path, body)
                self.assertIn(response.status_code, (200, 201), response.content)


class StudentKeyLeakageTests(AuthoringTestCase):

    def test_authored_and_published_quiz_never_leaks_keys_to_students(self):
        quiz_id = self.create_draft()['id']
        question = self.add_question(quiz_id)
        self.as_admin('post', f'/quiz/quizzes/{quiz_id}/publish/')
        responses = [
            self.call(self.student, 'get', '/quiz/quizzes/'),
            self.call(self.student, 'get', f'/quiz/quizzes/{quiz_id}/'),
            self.call(self.student, 'get', f'/quiz/quizzes/{quiz_id}'),
            self.call(self.student, 'get', f"/quiz/question/{question['id']}/"),
            self.call(self.student, 'post', f'/quiz/quizzes/{quiz_id}/publish/'),
            self.call(self.student, 'post', f'/quiz/quizzes/{quiz_id}/questions/', question_body()),
            self.call(self.student, 'get', '/quiz/question/'),
        ]
        for response in responses:
            with self.subTest(path=response.request['PATH_INFO']):
                self.assertNoAnswerKeys(response)
                self.assertNotIn('is_correct', response.content.decode())
        detail = responses[1].json()['data']
        self.assertEqual([o['id'] for o in detail['questions'][0]['options']],
                         [o['id'] for o in question['options']])
        admin_detail = self.as_admin('get', f'/quiz/quizzes/{quiz_id}/').json()['data']
        self.assertTrue(KEY_FIELDS.intersection(walk_keys(admin_detail)))


def json_schema(node):
    """OpenAPI 3.0 ``nullable`` as JSON Schema (Draft 4 has no ``nullable``)."""
    if isinstance(node, list):
        return [json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    converted = {key: json_schema(value) for key, value in node.items() if key != 'nullable'}
    if node.get('nullable'):
        return {'anyOf': [converted, {'type': 'null'}]}
    return converted


class SchemaWireContractTests(AuthoringTestCase):
    """The OpenAPI document describes the real enveloped wire payloads and examples."""

    PATHS = {
        'list': ('/quiz/quizzes/', 'get', '200'),
        'create': ('/quiz/quizzes/', 'post', '201'),
        'detail': ('/quiz/quizzes/{quiz_id}/', 'get', '200'),
        'patch': ('/quiz/quizzes/{quiz_id}/', 'patch', '200'),
        'question': ('/quiz/quizzes/{quiz_id}/questions/', 'post', '201'),
        'question patch': ('/quiz/question/{question_id}/', 'patch', '200'),
        'publish': ('/quiz/quizzes/{quiz_id}/publish/', 'post', '200'),
        'close': ('/quiz/quizzes/{quiz_id}/close/', 'post', '200'),
        'publish invalid': ('/quiz/quizzes/{quiz_id}/publish/', 'post', '400'),
        'immutable': ('/quiz/quizzes/{quiz_id}/', 'patch', '409'),
        'publish conflict': ('/quiz/quizzes/{quiz_id}/publish/', 'post', '409'),
        'close conflict': ('/quiz/quizzes/{quiz_id}/close/', 'post', '409'),
        'question invalid': ('/quiz/quizzes/{quiz_id}/questions/', 'post', '400'),
        'question locked': ('/quiz/quizzes/{quiz_id}/questions/', 'post', '409'),
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from drf_spectacular.generators import SchemaGenerator
        cls.schema = json_schema(SchemaGenerator().get_schema(public=True))

    def validator(self, name):
        from jsonschema import Draft4Validator, RefResolver
        path, method, status_code = self.PATHS[name]
        content = self.schema['paths'][path][method]['responses'][status_code]['content']
        media = content['application/json']
        schema = media['schema']
        if name == 'detail':
            # The role-specific oneOf cannot discriminate (an admin payload is a
            # superset of the student one); check the admin component itself.
            data = schema['properties']['data']
            if '$ref' in data:
                data = self.schema['components']['schemas'][data['$ref'].rsplit('/', 1)[1]]
            self.assertEqual({ref['$ref'] for ref in data['oneOf']},
                             {'#/components/schemas/AdminQuiz',
                              '#/components/schemas/StudentQuiz'})
            schema = {**schema, 'properties': {**schema['properties'], 'data': {
                '$ref': '#/components/schemas/AdminQuiz'}}}
        return Draft4Validator(schema, resolver=RefResolver.from_schema(self.schema)), media

    def assertWire(self, name, response):
        validator, _ = self.validator(name)
        validator.validate(response.json())

    def test_wire_payloads_match_schema(self):
        created = self.as_admin('post', '/quiz/quizzes/', {
            'title': 'Wire', 'description': 'd', 'time_limit': 30, 'deadline': iso(future())})
        self.assertWire('create', created)
        quiz_id = created.json()['data']['id']
        self.assertWire('publish invalid', self.as_admin('post', f'/quiz/quizzes/{quiz_id}/publish/'))
        question = self.as_admin('post', f'/quiz/quizzes/{quiz_id}/questions/', question_body())
        self.assertWire('question', question)
        self.assertWire('question patch', self.as_admin(
            'patch', f"/quiz/question/{question.json()['data']['id']}/", {'points': 3}))
        self.assertWire('patch', self.as_admin('patch', f'/quiz/quizzes/{quiz_id}/',
                                               {'title': 'Wire 2'}))
        self.assertWire('detail', self.as_admin('get', f'/quiz/quizzes/{quiz_id}/'))
        self.assertWire('publish', self.as_admin('post', f'/quiz/quizzes/{quiz_id}/publish/'))
        self.assertWire('immutable', self.as_admin('patch', f'/quiz/quizzes/{quiz_id}/',
                                                   {'time_limit': 5}))
        self.assertWire('list', self.as_admin('get', '/quiz/quizzes/'))
        self.assertWire('list', self.call(self.student, 'get', '/quiz/quizzes/'))
        self.assertWire('publish conflict', self.as_admin('post', f'/quiz/quizzes/{quiz_id}/publish/'))
        self.assertWire('question locked', self.as_admin(
            'post', f'/quiz/quizzes/{quiz_id}/questions/', question_body(order=2)))
        self.assertWire('question invalid', self.as_admin(
            'post', f'/quiz/quizzes/{quiz_id}/questions/', question_body(points=0)))
        self.assertWire('close', self.as_admin('post', f'/quiz/quizzes/{quiz_id}/close/'))

    def test_schema_examples_are_enveloped_and_valid(self):
        checked = 0
        for name in self.PATHS:
            validator, media = self.validator(name)
            for example in media.get('examples', {}).values():
                with self.subTest(name=name, example=example.get('summary')):
                    validator.validate(example['value'])
                    self.assertEqual(set(example['value']), {'success', 'data', 'message'})
                    checked += 1
        self.assertGreaterEqual(checked, 12)
        # Request examples carry no option IDs (lead decision) and no envelope.
        request = self.schema['paths']['/quiz/quizzes/{quiz_id}/questions/']['post']['requestBody']
        for example in request['content']['application/json']['examples'].values():
            self.assertNotIn('success', example['value'])
            self.assertTrue(all('id' not in option for option in example['value']['options']))
