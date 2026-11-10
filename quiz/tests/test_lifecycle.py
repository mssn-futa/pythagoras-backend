"""ASSESS-02 lifecycle, attempt timing, immutability and DB invariants (PostgreSQL)."""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from dawah.models import Event
from pythagoras.utils import APIError
from quiz import lifecycle
from quiz.errors import (
    AttemptExpired, InvalidAnswers, InvalidTransition, PublicationInvalid, QuizImmutable,
    QuizReferenced, QuizUnavailable, SubmissionConflict, SubmissionImmutable,
)
from quiz.models import MAX_STORED_POINTS, Attempt, Course, Option, Question, Quiz, Submission
from quiz.serializers import StudentAttemptSerializer

from quiz.views import domain_errors

from .helpers import KEY_FIELDS, QuizAPITestMixin, make_quiz, raw_update, walk_keys

T0 = datetime(2026, 10, 12, 10, 0, tzinfo=dt_timezone.utc)


def immediate_constraints():
    """Check deferred constraints now, as a commit would (TestCase never commits).

    Restores deferral afterwards: SET CONSTRAINTS lasts for the whole outer
    transaction. ALL DEFERRED affects only deferrable constraints.
    """
    with connection.cursor() as cursor:
        cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
        cursor.execute('SET CONSTRAINTS ALL DEFERRED')


class ExpiryComputationTests(SimpleTestCase):

    def test_duration_before_deadline(self):
        self.assertEqual(lifecycle.compute_expires_at(T0, 30, T0 + timedelta(hours=2)),
                         T0 + timedelta(minutes=30))

    def test_deadline_before_duration_wins(self):
        deadline = T0 + timedelta(minutes=10)
        self.assertEqual(lifecycle.compute_expires_at(T0, 30, deadline), deadline)

    def test_deadline_equal_to_duration(self):
        deadline = T0 + timedelta(minutes=30)
        self.assertEqual(lifecycle.compute_expires_at(T0, 30, deadline), deadline)

    def test_exact_expiry_boundary(self):
        attempt = Attempt(started_at=T0, expires_at=T0 + timedelta(minutes=30))
        self.assertFalse(lifecycle.is_expired(attempt, attempt.expires_at - timedelta(microseconds=1)))
        self.assertTrue(lifecycle.is_expired(attempt, attempt.expires_at))
        self.assertTrue(lifecycle.is_expired(attempt, attempt.expires_at + timedelta(seconds=1)))
        self.assertEqual(lifecycle.effective_attempt_status(attempt, attempt.expires_at), 'expired')
        self.assertEqual(lifecycle.effective_attempt_status(
            attempt, attempt.expires_at - timedelta(microseconds=1)), 'in_progress')

    def test_invalid_inputs(self):
        for limit, deadline in ((0, T0 + timedelta(hours=1)), (None, T0 + timedelta(hours=1)),
                                (30, None), (30, T0), (30, T0 - timedelta(minutes=1))):
            with self.subTest(limit=limit, deadline=deadline), self.assertRaises(ValueError):
                lifecycle.compute_expires_at(T0, limit, deadline)


def past_quiz(title):
    """A quiz published (judged an hour earlier) whose deadline has passed."""
    deadline = timezone.now() - timedelta(hours=1)
    quiz, _ = make_quiz(title, deadline=deadline, published_now=deadline - timedelta(hours=1))
    return quiz


def draft_quiz(title='Draft', **kwargs):
    quiz, questions = make_quiz(title, active=False, **kwargs)
    return quiz, questions


class PublicationTests(TestCase):

    def test_publish_sets_state_and_derived_is_active(self):
        quiz, _ = draft_quiz()
        self.assertEqual((quiz.status, quiz.is_active, quiz.published_at), ('draft', False, None))
        published, changed = lifecycle.publish_quiz(quiz.pk)
        self.assertTrue(changed)
        published.refresh_from_db()
        self.assertEqual((published.status, published.is_active), ('published', True))
        self.assertIsNotNone(published.published_at)
        again, changed = lifecycle.publish_quiz(quiz.pk)
        self.assertFalse(changed)
        self.assertEqual(again.published_at, published.published_at)

    def test_close_is_idempotent_and_terminal(self):
        quiz, _ = make_quiz('Close me')
        closed, changed = lifecycle.close_quiz(quiz.pk)
        self.assertTrue(changed)
        closed.refresh_from_db()
        self.assertEqual((closed.status, closed.is_active), ('closed', False))
        self.assertIsNotNone(closed.closed_at)
        _, changed = lifecycle.close_quiz(quiz.pk)
        self.assertFalse(changed)
        with self.assertRaises(InvalidTransition):
            lifecycle.publish_quiz(quiz.pk)
        for status in ('draft', 'published'):
            closed.status = status
            with self.subTest(status=status), self.assertRaises(InvalidTransition):
                closed.save()

    def test_forbidden_transitions(self):
        quiz, _ = draft_quiz()
        with self.assertRaises(InvalidTransition):
            lifecycle.close_quiz(quiz.pk)  # draft -> closed
        published, _ = lifecycle.publish_quiz(quiz.pk)
        published.status = Quiz.Status.DRAFT
        with self.assertRaises(InvalidTransition):
            published.save()  # published -> draft
        with self.assertRaises(InvalidTransition):
            Quiz.objects.create(title='x', description='y', status=Quiz.Status.PUBLISHED)

    def test_publication_validation(self):
        past = timezone.now() - timedelta(minutes=1)
        cases = {
            'no deadline': ({'deadline': None}, 'deadline'),
            'past deadline': ({'deadline': past}, 'deadline'),
            'no time limit': ({'time_limit': None}, 'time_limit'),
            'time limit too long': ({'time_limit': 181}, 'time_limit'),
        }
        for name, (fields, error) in cases.items():
            quiz, _ = draft_quiz(name)
            Quiz.objects.filter(pk=quiz.pk).update(**fields)
            with self.subTest(name), self.assertRaises(PublicationInvalid) as caught:
                lifecycle.publish_quiz(quiz.pk)
            self.assertIn(error, caught.exception.fields)
            self.assertEqual(Quiz.objects.get(pk=quiz.pk).status, 'draft')

    def test_question_graph_validation(self):
        empty = Quiz.objects.create(title='Empty', description='d', time_limit=10,
                                    deadline=timezone.now() + timedelta(days=1))
        with self.assertRaises(PublicationInvalid) as caught:
            lifecycle.publish_quiz(empty.pk)
        self.assertIn('questions', caught.exception.fields)

        def broken(name, mutate):
            quiz, questions = draft_quiz(name, points=(1,))
            mutate(quiz, questions[0])
            with self.assertRaises(PublicationInvalid) as caught:
                lifecycle.publish_quiz(quiz.pk)
            return caught.exception.fields['questions'][-1][str(questions[0][0].pk)]

        def two_correct(quiz, row):
            Option.objects.filter(pk=row[2][0].pk).update(is_correct=True)

        def three_options(quiz, row):
            row[2][0].delete()

        def duplicate_text(quiz, row):
            Option.objects.filter(pk=row[2][0].pk).update(text='  b ')  # same as "B"

        def zero_points(quiz, row):
            Question.objects.filter(pk=row[0].pk).update(points=0)

        def legacy_type(quiz, row):
            Question.objects.filter(pk=row[0].pk).update(type=Question.Type.text)

        self.assertIn('Exactly one option must be correct.', broken('two', two_correct))
        self.assertIn('Exactly 4 options are required.', broken('three', three_options))
        self.assertIn('Options must be distinct.', broken('dup', duplicate_text))
        self.assertIn('Points must be a positive integer.', broken('zero', zero_points))
        self.assertIn('Only multiple-choice questions can be published.', broken('text', legacy_type))

    def test_derived_is_active_enforced_by_database(self):
        quiz, _ = make_quiz('Derived')
        for fields in ({'is_active': False}, {'status': 'draft'}, {'deadline': None},
                       {'status': 'closed'}, {'status': 'bogus', 'is_active': False}):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), \
                    transaction.atomic():
                raw_update('quiz_quiz', quiz.pk, **fields)

    def test_available_quizzes(self):
        live, _ = make_quiz('Live')
        draft, _ = draft_quiz()
        closed, _ = make_quiz('Closed')
        lifecycle.close_quiz(closed.pk)
        past_quiz('Past deadline')
        self.assertEqual(list(lifecycle.available_quizzes().values_list('pk', flat=True)), [live.pk])


class ImmutabilityTests(QuizAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Frozen')
        self.question, self.correct, self.wrong = self.questions[0]

    def test_metadata_corrections_allowed(self):
        self.quiz.title = 'Renamed'
        self.quiz.description = 'Fixed typo'
        self.quiz.save()
        lifecycle.close_quiz(self.quiz.pk)
        quiz = Quiz.objects.get(pk=self.quiz.pk)
        quiz.title = 'Renamed after closing'
        quiz.save()

    def test_locked_quiz_fields(self):
        event = Event.objects.create(title='E', category='c', tags='t', speakers='s', location='l',
                                     start_time=T0, end_time=T0 + timedelta(hours=1))
        course = Course.objects.create(code='ARB1', title='Arabic', department='', level='100',
                                       semester='1')
        changes = {'time_limit': 45, 'deadline': self.quiz.deadline + timedelta(days=1),
                   'related_event': event, 'related_course': course, 'type': 'other'}
        for name, value in changes.items():
            quiz = Quiz.objects.get(pk=self.quiz.pk)
            setattr(quiz, name, value)
            with self.subTest(field=name), self.assertRaises(QuizImmutable) as caught:
                quiz.save()
            self.assertIn(name, caught.exception.fields)

    def test_questions_and_options_frozen(self):
        operations = {
            'question edit': lambda: (setattr(self.question, 'points', 9), self.question.save()),
            'question delete': lambda: self.question.delete(),
            'question add': lambda: Question.objects.create(quiz=self.quiz, type='mcq', text='n'),
            'option edit': lambda: (setattr(self.wrong[0], 'is_correct', True),
                                    self.wrong[0].save()),
            'option delete': lambda: self.correct.delete(),
            'option add': lambda: Option.objects.create(question=self.question, text='E'),
        }
        for name, operation in operations.items():
            with self.subTest(name), self.assertRaises(QuizImmutable), transaction.atomic():
                operation()
        self.assertEqual(Question.objects.get(pk=self.question.pk).points, 2)
        self.assertEqual(self.question.option_set.count(), 4)

    def test_moving_a_draft_question_into_a_published_quiz_is_refused(self):
        draft, questions = draft_quiz()
        moving = questions[0][0]
        moving.quiz = self.quiz
        with self.assertRaises(QuizImmutable):
            moving.save()

    def test_api_quiz_put(self):
        path = f'/quiz/quizzes/{self.quiz.pk}/'
        body = {'title': 'Corrected', 'type': 'arabic', 'description': 'Fixed',
                'time_limit': self.quiz.time_limit,
                'deadline': self.quiz.deadline.isoformat().replace('+00:00', 'Z')}
        response = self.call(self.admin, 'put', path, body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['title'], 'Corrected')
        response = self.call(self.admin, 'put', path, {**body, 'time_limit': 60})
        payload = self.assertEnvelopeError(response, 409, 'QUIZ_IMMUTABLE')
        self.assertIn('time_limit', payload['data']['fields'])
        self.assertEqual(Quiz.objects.get(pk=self.quiz.pk).time_limit, 30)

    def test_api_status_and_is_active_are_not_writable(self):
        for body in ({'is_active': True}, {'status': 'published'}):
            response = self.call(self.admin, 'post', '/quiz/quizzes/',
                                 {'title': 'New', 'description': 'd', **body})
            with self.subTest(body=body):
                payload = self.assertEnvelopeError(response, 400, 'VALIDATION_ERROR')
                self.assertEqual(set(payload['data']['fields']), set(body))
        # Round-tripping the current value is accepted (admin edit forms send it back).
        response = self.call(self.admin, 'post', '/quiz/quizzes/',
                             {'title': 'New', 'description': 'd', 'is_active': False,
                              'status': 'draft'})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['data']['type'], 'arabic')  # release default
        self.assertEqual(response.json()['data']['status'], 'draft')

    def test_api_question_writes_on_published_quiz(self):
        body = {'quiz': self.quiz.pk, 'text': 'New', 'points': 1,
                'options': [{'text': label, 'is_correct': label == 'A'} for label in 'ABCD']}
        before = Question.objects.count()
        self.assertEnvelopeError(self.call(self.admin, 'post', '/quiz/question/', body),
                                 409, 'QUIZ_IMMUTABLE')
        path = f'/quiz/question/{self.question.pk}/'
        self.assertEnvelopeError(self.call(self.admin, 'put', path, body), 409, 'QUIZ_IMMUTABLE')
        self.assertEnvelopeError(self.call(self.admin, 'delete', path), 409, 'QUIZ_IMMUTABLE')
        self.assertEqual(Question.objects.count(), before)
        self.assertEqual(Option.objects.filter(question=self.question).count(), 4)

    def test_quiz_deletion_rules(self):
        self.assertEnvelopeError(self.call(self.admin, 'delete', f'/quiz/quizzes/{self.quiz.pk}/'),
                                 409, 'QUIZ_IMMUTABLE')
        with self.assertRaises(QuizImmutable):
            Quiz.objects.get(pk=self.quiz.pk).delete()
        draft, _ = draft_quiz()
        lifecycle.delete_draft_quiz(draft.pk)
        self.assertFalse(Quiz.objects.filter(pk=draft.pk).exists())
        lifecycle.start_attempt(self.quiz.pk, self.student)
        with self.assertRaises(QuizReferenced):
            lifecycle.delete_draft_quiz(self.quiz.pk)


class OrderingTests(QuizAPITestMixin, TestCase):

    def test_persisted_order_drives_every_payload(self):
        quiz, questions = draft_quiz('Ordered', points=(1, 2, 3))
        (q1, _, _), (q2, _, _), (q3, _, _) = questions
        self.assertEqual([q.order for q in (q1, q2, q3)], [1, 2, 3])  # appended
        # Reverse the order (deferred unique constraint permits the swap).
        with transaction.atomic():
            for question, order in ((q1, 3), (q3, 1)):
                question.order = order
                question.save()
            immediate_constraints()
        options = list(q2.option_set.order_by('pk'))
        self.assertEqual([o.order for o in options], [1, 2, 3, 4])
        with transaction.atomic():
            for option, order in zip(options, (4, 3, 2, 1)):
                option.order = order
                option.save()
            immediate_constraints()
        lifecycle.publish_quiz(quiz.pk)
        expected = [q3.pk, q2.pk, q1.pk]
        for user in (self.student, self.admin):
            data = self.call(user, 'get', f'/quiz/quizzes/{quiz.pk}/').json()['data']
            with self.subTest(user=user.email):
                self.assertEqual([q['id'] for q in data['questions']], expected)
                self.assertEqual([o['id'] for o in data['questions'][1]['options']],
                                 [o.pk for o in reversed(options)])
        submission, _ = lifecycle.finalize_attempt(
            lifecycle.start_attempt(quiz.pk, self.student)[0].pk, self.student, {})
        self.assertEqual([row['question_id'] for row in submission.grading_snapshot['questions']],
                         expected)

    def test_duplicate_order_rejected_by_database(self):
        quiz, questions = draft_quiz('Dup order')
        (q1, _, _), (q2, _, _) = questions
        with self.assertRaises(IntegrityError), transaction.atomic():
            Question.objects.filter(pk=q2.pk).update(order=q1.order)
            immediate_constraints()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Question.objects.filter(pk=q2.pk).update(order=0)

    def test_api_order_validation_and_defaults(self):
        quiz, questions = draft_quiz('API order')
        body = {'quiz': quiz.pk, 'text': 'Third', 'points': 1,
                'options': [{'text': 'A', 'is_correct': True, 'order': 2}, {'text': 'B'},
                            {'text': 'C', 'order': 1}, {'text': 'D'}]}
        response = self.call(self.admin, 'post', '/quiz/question/', body)
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()['data']
        self.assertEqual(data['order'], 3)
        self.assertEqual([(o['text'], o['order']) for o in data['options']],
                         [('C', 1), ('A', 2), ('B', 3), ('D', 4)])
        clash = self.call(self.admin, 'post', '/quiz/question/', {**body, 'order': 1})
        self.assertIn('order', self.assertEnvelopeError(clash, 400, 'VALIDATION_ERROR')['data']['fields'])
        dup = self.call(self.admin, 'post', '/quiz/question/', {
            **body, 'options': [{'text': 'A', 'order': 1}, {'text': 'B', 'order': 1}]})
        self.assertIn('options', self.assertEnvelopeError(dup, 400, 'VALIDATION_ERROR')['data']['fields'])


class AttemptTests(QuizAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Timed', points=(2, 3))
        (self.q1, self.c1, self.w1), (self.q2, self.c2, self.w2) = self.questions
        self.answers = {str(self.q1.pk): self.c1.pk, str(self.q2.pk): self.w2[0].pk}

    def start(self, student=None, **kwargs):
        return lifecycle.start_attempt(self.quiz.pk, student or self.student, **kwargs)

    def test_start_uses_server_time_and_is_idempotent(self):
        before = lifecycle.server_now()
        attempt, created = self.start()
        self.assertTrue(created)
        self.assertEqual(attempt.status, 'in_progress')
        self.assertGreaterEqual(attempt.started_at, before)
        self.assertEqual(attempt.expires_at, attempt.started_at + timedelta(minutes=30))
        again, created = self.start()
        self.assertFalse(created)
        self.assertEqual((again.pk, again.started_at, again.expires_at),
                         (attempt.pk, attempt.started_at, attempt.expires_at))
        # Closing never shortens or erases an existing attempt.
        lifecycle.close_quiz(self.quiz.pk)
        after_close, created = self.start()
        self.assertFalse(created)
        self.assertEqual(after_close.expires_at, attempt.expires_at)

    def test_deadline_before_duration_caps_expiry(self):
        deadline = lifecycle.server_now() + timedelta(minutes=5)
        quiz, _ = make_quiz('Short deadline', deadline=deadline)
        attempt, _ = lifecycle.start_attempt(quiz.pk, self.student)
        self.assertEqual(attempt.expires_at, deadline)

    def test_start_requires_available_quiz_and_eligible_student(self):
        draft, _ = draft_quiz()
        closed, _ = make_quiz('Closed')
        lifecycle.close_quiz(closed.pk)
        past = past_quiz('Past')
        for quiz in (draft, closed, past):
            with self.subTest(quiz=quiz.title), self.assertRaises(QuizUnavailable):
                lifecycle.start_attempt(quiz.pk, self.student)
        for user in (self.admin, self.inactive_student):
            user.refresh_from_db()
            with self.subTest(user=user.email), self.assertRaises(QuizUnavailable):
                self.start(user)
        with self.assertRaises(Quiz.DoesNotExist):
            lifecycle.start_attempt(999999, self.student)
        self.assertFalse(Attempt.objects.exists())

    def test_database_rejects_second_attempt(self):
        attempt, _ = self.start()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Attempt.objects.create(quiz=self.quiz, student=self.student,
                                   started_at=attempt.started_at, expires_at=attempt.expires_at)
        self.assertEqual(Attempt.objects.count(), 1)

    def test_finalize_grades_with_snapshot(self):
        attempt, _ = self.start()
        submission, created = lifecycle.finalize_attempt(attempt.pk, self.student, self.answers)
        self.assertTrue(created)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertEqual((submission.score, submission.total_points), (2, 5))
        self.assertEqual(submission.submitted_at, attempt.finalized_at)
        self.assertEqual(submission.graded_at, attempt.finalized_at)
        self.assertFalse(submission.is_legacy)
        self.assertEqual(submission.grading_snapshot, {
            'version': 1, 'quiz_id': self.quiz.pk, 'quiz_title': 'Timed', 'score': 2,
            'total_points': 5, 'questions': [
                {'question_id': self.q1.pk, 'order': 1, 'points': 2,
                 'selected_option_id': self.c1.pk, 'awarded_points': 2},
                {'question_id': self.q2.pk, 'order': 2, 'points': 3,
                 'selected_option_id': self.w2[0].pk, 'awarded_points': 0},
            ]})
        self.assertFalse(KEY_FIELDS.intersection(walk_keys(submission.grading_snapshot)))
        # Metadata correction never rewrites the snapshot.
        quiz = Quiz.objects.get(pk=self.quiz.pk)
        quiz.title = 'Renamed'
        quiz.save()
        submission.refresh_from_db()
        self.assertEqual(submission.grading_snapshot['quiz_title'], 'Timed')

    def test_exact_expiry_boundary(self):
        attempt, _ = self.start()
        just_before = attempt.expires_at - timedelta(microseconds=1)
        other, _ = self.start(self.other_student)
        submission, created = lifecycle.finalize_attempt(
            other.pk, self.other_student, self.answers, now=other.expires_at - timedelta(microseconds=1))
        self.assertTrue(created)
        with self.assertRaises(AttemptExpired):
            lifecycle.finalize_attempt(attempt.pk, self.student, self.answers, now=attempt.expires_at)
        attempt.refresh_from_db()
        # ASSESS-04: a late request reads expiry but never persists it, so it
        # cannot reject an earlier arrival still waiting for the attempt lock.
        self.assertEqual(attempt.status, 'in_progress')
        self.assertEqual(lifecycle.effective_attempt_status(attempt, attempt.expires_at), 'expired')
        self.assertIsNone(attempt.finalized_at)
        self.assertFalse(Submission.objects.filter(attempt=attempt).exists())  # no invented grade
        # An arrival before expiry (e.g. one that waited behind the late request) is on time.
        submission, created = lifecycle.finalize_attempt(attempt.pk, self.student, self.answers,
                                                         now=just_before)
        self.assertTrue(created)
        self.assertEqual(submission.submitted_at, just_before)

    def test_replay_after_expiry_and_conflict(self):
        attempt, _ = self.start()
        first, _ = lifecycle.finalize_attempt(attempt.pk, self.student, self.answers)
        late = attempt.expires_at + timedelta(hours=1)
        lifecycle.close_quiz(self.quiz.pk)
        replay, created = lifecycle.finalize_attempt(attempt.pk, self.student, self.answers, now=late)
        self.assertFalse(created)
        self.assertEqual(replay.pk, first.pk)
        with self.assertRaises(SubmissionConflict):
            lifecycle.finalize_attempt(attempt.pk, self.student, {str(self.q1.pk): self.w1[0].pk},
                                       now=late)
        self.assertEqual(Submission.objects.get().score, 2)

    def test_closing_does_not_block_an_unexpired_attempt(self):
        attempt, _ = self.start()
        lifecycle.close_quiz(self.quiz.pk)
        submission, created = lifecycle.finalize_attempt(attempt.pk, self.student, self.answers)
        self.assertTrue(created)

    def test_ownership_and_invalid_answers(self):
        attempt, _ = self.start()
        with self.assertRaises(Attempt.DoesNotExist):
            lifecycle.finalize_attempt(attempt.pk, self.other_student, self.answers)
        with self.assertRaises(InvalidAnswers):
            lifecycle.finalize_attempt(attempt.pk, self.student, {str(self.q1.pk): self.c2.pk})
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'in_progress')  # rejected input never consumes it
        self.assertFalse(Submission.objects.exists())

    def test_mark_expired(self):
        attempt, _ = self.start()
        self.assertEqual(lifecycle.mark_expired(attempt.pk).status, 'in_progress')
        self.assertEqual(lifecycle.mark_expired(attempt.pk, now=attempt.expires_at).status, 'expired')

    def test_student_attempt_contract(self):
        attempt, _ = self.start()
        data = StudentAttemptSerializer(attempt, context={'now': attempt.started_at}).data
        self.assertEqual(set(data), {'id', 'quiz_id', 'quiz_title', 'status', 'started_at',
                                     'expires_at', 'server_time', 'submission_id'})
        self.assertEqual((data['id'], data['quiz_id'], data['quiz_title'], data['status'],
                          data['submission_id']),
                         (attempt.pk, self.quiz.pk, 'Timed', 'in_progress', None))
        self.assertEqual(data['server_time'], data['started_at'])
        self.assertTrue(data['server_time'].endswith('Z'))
        self.assertFalse(KEY_FIELDS.intersection(walk_keys(data)))
        late = StudentAttemptSerializer(attempt, context={'now': attempt.expires_at}).data
        self.assertEqual(late['status'], 'expired')
        submission, _ = lifecycle.finalize_attempt(attempt.pk, self.student, self.answers)
        attempt.refresh_from_db()
        done = StudentAttemptSerializer(attempt).data  # server_time from the DB clock
        self.assertEqual((done['status'], done['submission_id']), ('submitted', submission.pk))
        self.assertIsNotNone(done['server_time'])


class HistoryInvariantTests(QuizAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('History')
        self.attempt, _ = lifecycle.start_attempt(self.quiz.pk, self.student)
        self.submission, _ = lifecycle.finalize_attempt(self.attempt.pk, self.student, {})

    def test_submissions_are_insert_only(self):
        self.submission.score = 99
        with self.assertRaises(SubmissionImmutable):
            self.submission.save()
        with self.assertRaises(SubmissionImmutable):
            self.submission.delete()
        with self.assertRaises(SubmissionImmutable):
            Submission.objects.filter(pk=self.submission.pk).update(score=99)
        with self.assertRaises(SubmissionImmutable):
            Submission.objects.all().delete()
        self.assertEqual(Submission.objects.get().score, 0)

    def test_finalized_attempts_are_frozen(self):
        attempt = Attempt.objects.get(pk=self.attempt.pk)
        for field, value in (('status', 'in_progress'), ('status', 'expired'),
                             ('expires_at', attempt.expires_at + timedelta(minutes=5))):
            attempt = Attempt.objects.get(pk=self.attempt.pk)
            setattr(attempt, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(QuizImmutable):
                attempt.save()
        with self.assertRaises(SubmissionImmutable):
            attempt.delete()
        with self.assertRaises(SubmissionImmutable):
            Attempt.objects.all().delete()

    def test_in_progress_timing_is_fixed(self):
        attempt, _ = lifecycle.start_attempt(self.quiz.pk, self.other_student)
        attempt.expires_at += timedelta(minutes=10)
        with self.assertRaises(QuizImmutable):
            attempt.save()

    def test_database_constraints(self):
        other = self.make_user('db@example.test')
        attempt, _ = lifecycle.start_attempt(self.quiz.pk, other)
        base = {'quiz': self.quiz, 'student': other, 'answers': {}, 'score': 0,
                'graded_at': timezone.now()}
        cases = {
            'release row without attempt': {**base},
            'release row without snapshot': {**base, 'attempt': attempt, 'total_points': 0},
            'score above total': {**base, 'attempt': attempt, 'total_points': 1, 'score': 2,
                                  'grading_snapshot': {}},
        }
        for name, fields in cases.items():
            with self.subTest(name), self.assertRaises(IntegrityError), transaction.atomic():
                Submission.objects.create(**fields)
        # Constraints that only legacy-shaped rows can reach: proven with the
        # quiz.0007 guard disabled inside this (rolled back) transaction.
        legacy_sql = ('INSERT INTO quiz_submission (quiz_id, student_id, attempt_id, answers, '
                      'score, is_legacy, submitted_at) VALUES (%s, %s, %s, %s, 0, true, now())')
        for name, params in {
            'legacy row with attempt': [self.quiz.pk, other.pk, attempt.pk, '{}'],
            'second submission for student/quiz': [self.quiz.pk, self.student.pk, None, '{}'],
        }.items():
            with self.subTest(name), self.assertRaises(IntegrityError), transaction.atomic():
                immediate_constraints()  # ALTER TABLE needs no pending FK checks
                with connection.cursor() as cursor:
                    cursor.execute('ALTER TABLE quiz_submission '
                                   'DISABLE TRIGGER quiz_submission_history_guard')
                    cursor.execute(legacy_sql, params)
        # A second submission for the same attempt violates the one-to-one key.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Submission.objects.create(quiz=self.quiz, student=self.student, attempt=self.attempt,
                                      answers={}, score=0, total_points=0, grading_snapshot={},
                                      graded_at=timezone.now())
        # Finalization at/after expiry is rejected by the database as well.
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_update('quiz_attempt', attempt.pk, status='submitted',
                       finalized_at=attempt.expires_at)

    def test_score_storage_is_wide(self):
        attempt, _ = lifecycle.start_attempt(self.quiz.pk, self.other_student)
        row = Submission.objects.create(
            quiz=self.quiz, student=self.other_student, attempt=attempt, answers={},
            score=MAX_STORED_POINTS, total_points=MAX_STORED_POINTS, grading_snapshot={},
            graded_at=timezone.now())
        row.refresh_from_db()
        self.assertEqual((row.score, row.total_points), (2147483647, 2147483647))


class ProtectedDeletionTests(QuizAPITestMixin, TestCase):
    """Event/course/user/quiz deletion never cascades assessment history (D0)."""

    def setUp(self):
        super().setUp()
        self.event = Event.objects.create(title='E', category='c', tags='t', speakers='s',
                                          location='l', start_time=T0,
                                          end_time=T0 + timedelta(hours=1))
        self.course = Course.objects.create(code='ARB1', title='Arabic', department='',
                                            level='100', semester='1')
        self.quiz = Quiz.objects.create(title='Linked', description='d', time_limit=30,
                                        deadline=timezone.now() + timedelta(days=1),
                                        related_event=self.event, related_course=self.course)

    def test_event_and_course_referenced_by_a_draft_quiz_are_protected(self):
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.event.delete()
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.course.delete()
        self.assertTrue(Quiz.objects.filter(pk=self.quiz.pk, related_event=self.event).exists())

    def test_history_survives_every_delete_attempt(self):
        quiz, _ = make_quiz('With history')
        attempt, _ = lifecycle.start_attempt(quiz.pk, self.student)
        submission, _ = lifecycle.finalize_attempt(attempt.pk, self.student, {})
        for name, operation in {
            'quiz queryset': lambda: Quiz.objects.filter(pk=quiz.pk).delete(),
            'student': lambda: self.student.delete(),
        }.items():
            with self.subTest(name), self.assertRaises(ProtectedError), transaction.atomic():
                operation()
        self.assertTrue(Submission.objects.filter(pk=submission.pk, attempt=attempt).exists())

    def test_event_delete_api_does_not_cascade(self):
        lifecycle.publish_quiz(self.make_published_graph())
        response = self.call(self.admin, 'delete', f'/dawah/events/{self.event.pk}/')
        # The dawah delete guard (DAWAH-01) maps this to 409 RESOURCE_REFERENCED;
        # whatever the status, the quiz must survive.
        self.assertGreaterEqual(response.status_code, 400)
        self.assertTrue(Quiz.objects.filter(pk=self.quiz.pk).exists())
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())

    def make_published_graph(self):
        question = Question.objects.create(quiz=self.quiz, type='mcq', text='Q', points=1)
        for label in 'ABCD':
            Option.objects.create(question=question, text=label, is_correct=label == 'A')
        return self.quiz.pk


def sqlstate(exc):
    from quiz.errors import db_error_sqlstate
    return db_error_sqlstate(exc)


class TransitionTokenTests(TestCase):
    """M2: Quiz.save() cannot publish/close; only publish_quiz/close_quiz can."""

    def test_save_cannot_publish_even_a_valid_quiz(self):
        quiz, _ = draft_quiz()
        self.assertEqual(lifecycle.publication_errors(quiz, timezone.now()), {})
        quiz.status = Quiz.Status.PUBLISHED
        quiz.published_at = timezone.now()
        with self.assertRaises(InvalidTransition) as caught:
            quiz.save()
        self.assertIn('status', caught.exception.fields)
        self.assertEqual(Quiz.objects.get(pk=quiz.pk).status, 'draft')
        # A token for another transition does not help, and tokens are single-use.
        quiz._lifecycle_transition = (Quiz.Status.PUBLISHED, Quiz.Status.CLOSED)
        with self.assertRaises(InvalidTransition):
            quiz.save()

    def test_save_cannot_close_or_move_lifecycle_timestamps(self):
        quiz, _ = make_quiz('Published')
        for field, value in (('status', Quiz.Status.CLOSED),
                             ('published_at', quiz.published_at - timedelta(days=1)),
                             ('closed_at', timezone.now())):
            fresh = Quiz.objects.get(pk=quiz.pk)
            setattr(fresh, field, value)
            if field == 'status':
                fresh.closed_at = timezone.now()
            with self.subTest(field=field), self.assertRaises(InvalidTransition):
                fresh.save()
        self.assertEqual(Quiz.objects.get(pk=quiz.pk).status, 'published')

    def test_create_with_lifecycle_timestamps_refused(self):
        for fields in ({'published_at': timezone.now()}, {'closed_at': timezone.now()}):
            with self.subTest(fields=fields), self.assertRaises(InvalidTransition):
                Quiz.objects.create(title='x', description='y', **fields)

    def test_bulk_update_cannot_change_lifecycle_or_frozen_fields(self):
        quiz, _ = make_quiz('Bulk')
        with self.assertRaises(InvalidTransition):
            Quiz.objects.filter(pk=quiz.pk).update(status='closed', closed_at=timezone.now())
        with self.assertRaises(QuizImmutable):
            Quiz.objects.filter(pk=quiz.pk).update(time_limit=99)
        Quiz.objects.filter(pk=quiz.pk).update(title='Bulk rename')  # metadata stays editable


class BypassGuardTests(QuizAPITestMixin, TestCase):
    """M3: every bulk/raw path the reviewer listed is refused in Python and in the DB."""

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Locked')
        self.question, self.correct, self.wrong = self.questions[0]
        self.attempt, _ = lifecycle.start_attempt(self.quiz.pk, self.student)

    def test_python_bulk_paths(self):
        question, option = self.question, self.wrong[0]
        operations = {
            'question update': lambda: Question.objects.filter(pk=question.pk).update(points=9),
            'question delete': lambda: Question.objects.filter(pk=question.pk).delete(),
            'question bulk_create': lambda: Question.objects.bulk_create(
                [Question(quiz=self.quiz, type='mcq', text='n', order=99)]),
            'question bulk_update': lambda: Question.objects.bulk_update(
                [Question(pk=question.pk, quiz=self.quiz, type='mcq', text='n', order=1)],
                ['text']),
            'option update': lambda: Option.objects.filter(pk=option.pk).update(is_correct=True),
            'option delete': lambda: Option.objects.filter(question=question).delete(),
            'option bulk_create': lambda: Option.objects.bulk_create(
                [Option(question=question, text='E', order=5)]),
            'option bulk_update': lambda: Option.objects.bulk_update(
                [Option(pk=option.pk, question=question, text='Z', order=option.order)],
                ['text']),
            'related manager delete': lambda: question.option_set.all().delete(),
        }
        for name, operation in operations.items():
            with self.subTest(name), self.assertRaises(QuizImmutable), transaction.atomic():
                operation()
        with self.assertRaises(QuizImmutable):
            Attempt.objects.filter(pk=self.attempt.pk).update(
                expires_at=self.attempt.expires_at + timedelta(hours=1))
        self.assertEqual(Question.objects.get(pk=question.pk).points, 2)
        self.assertEqual(Option.objects.filter(question=question).count(), 4)

    def test_draft_content_moved_into_a_locked_quiz_is_refused(self):
        draft, questions = draft_quiz()
        with self.assertRaises(QuizImmutable):
            Question.objects.filter(pk=questions[0][0].pk).update(quiz=self.quiz)
        with self.assertRaises(QuizImmutable):
            Option.objects.filter(pk=questions[0][1].pk).update(question=self.question)

    def test_database_triggers(self):
        submission, _ = lifecycle.finalize_attempt(self.attempt.pk, self.student, {})
        statements = {
            'question update': ('UPDATE quiz_question SET points = 9 WHERE id = %s',
                                [self.question.pk]),
            'question insert': ("INSERT INTO quiz_question (quiz_id, type, text, points, \"order\") "
                                "VALUES (%s, 'mcq', 'n', 1, 99)", [self.quiz.pk]),
            'question delete': ('DELETE FROM quiz_question WHERE id = %s', [self.question.pk]),
            'option update': ('UPDATE quiz_option SET is_correct = true WHERE id = %s',
                              [self.wrong[0].pk]),
            'option insert': ("INSERT INTO quiz_option (question_id, text, is_correct, \"order\") "
                              "VALUES (%s, 'E', false, 9)", [self.question.pk]),
            'option delete': ('DELETE FROM quiz_option WHERE id = %s', [self.correct.pk]),
            'quiz time_limit': ('UPDATE quiz_quiz SET time_limit = 99 WHERE id = %s',
                                [self.quiz.pk]),
            'quiz deadline': ("UPDATE quiz_quiz SET deadline = deadline + interval '1 day' "
                              "WHERE id = %s", [self.quiz.pk]),
            'quiz published_at': ("UPDATE quiz_quiz SET published_at = "
                                  "published_at - interval '1 day' WHERE id = %s", [self.quiz.pk]),
            'quiz back to draft': ("UPDATE quiz_quiz SET status = 'draft', is_active = false "
                                   "WHERE id = %s", [self.quiz.pk]),
            'quiz delete': ('DELETE FROM quiz_quiz WHERE id = %s', [self.quiz.pk]),
            'attempt expiry': ("UPDATE quiz_attempt SET expires_at = expires_at + "
                               "interval '1 hour' WHERE id = %s", [self.attempt.pk]),
            'attempt reopen': ("UPDATE quiz_attempt SET status = 'in_progress', "
                               "finalized_at = NULL WHERE id = %s", [self.attempt.pk]),
            'attempt delete': ('DELETE FROM quiz_attempt WHERE id = %s', [self.attempt.pk]),
            'submission score': ('UPDATE quiz_submission SET score = 99 WHERE id = %s',
                                 [submission.pk]),
            'submission delete': ('DELETE FROM quiz_submission WHERE id = %s', [submission.pk]),
            'legacy insert': ("INSERT INTO quiz_submission (quiz_id, student_id, answers, score, "
                              "is_legacy, submitted_at) VALUES (%s, %s, '{}', 0, true, now())",
                              [self.quiz.pk, self.other_student.pk]),
        }
        for name, (sql, params) in statements.items():
            with self.subTest(name), self.assertRaises(IntegrityError) as caught, \
                    transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
            self.assertEqual(sqlstate(caught.exception), '23Q01', name)
        # Metadata corrections and closing remain possible in SQL too.
        raw_update('quiz_quiz', self.quiz.pk, title='SQL rename')
        lifecycle.close_quiz(self.quiz.pk)

    def test_trigger_refusal_maps_to_quiz_immutable(self):
        with self.assertRaises(APIError) as caught, transaction.atomic(), domain_errors():
            raw_update('quiz_question', self.question.pk, points=9)
        self.assertEqual(caught.exception.code, 'QUIZ_IMMUTABLE')
        self.assertEqual(caught.exception.status_code, 409)

    def test_submission_must_match_its_attempt(self):
        """L2: enforced by the model and by the database."""
        other, _ = lifecycle.start_attempt(self.quiz.pk, self.other_student)
        with self.assertRaises(ValueError):
            Submission.objects.create(quiz=self.quiz, student=self.student, attempt=other,
                                      answers={}, score=0, total_points=0, grading_snapshot={},
                                      graded_at=timezone.now())
        with self.assertRaises(IntegrityError) as caught, transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO quiz_submission (quiz_id, student_id, attempt_id, answers, score, "
                    "total_points, grading_snapshot, is_legacy, submitted_at, graded_at) VALUES "
                    "(%s, %s, %s, '{}', 0, 0, '{}', false, now(), now())",
                    [self.quiz.pk, self.student.pk, other.pk])
        self.assertEqual(sqlstate(caught.exception), '23Q02')

    def test_no_new_legacy_rows(self):
        """L3: legacy rows only come from the migration."""
        with self.assertRaises(SubmissionImmutable):
            Submission.objects.create(quiz=self.quiz, student=self.other_student, answers={},
                                      score=0, is_legacy=True)

    def test_finalize_locks_only_the_attempt_row(self):
        """L1 (and M1): FOR SHARE on the quiz, FOR UPDATE OF the attempt only."""
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            lifecycle.finalize_attempt(self.attempt.pk, self.student, {})
        sql = [q['sql'] for q in queries.captured_queries]
        self.assertTrue(any('transaction_timestamp()' in s for s in sql[:2]), sql[:2])
        self.assertTrue(any(s.endswith('FOR SHARE') and 'quiz_quiz' in s for s in sql), sql)
        attempt_locks = [s for s in sql if 'FOR UPDATE' in s]
        self.assertEqual(len(attempt_locks), 1, attempt_locks)
        self.assertIn('FOR UPDATE OF "quiz_attempt"', attempt_locks[0])

    def test_content_guard_takes_its_own_lock(self):
        """L4: the question/option guard locks the quiz row itself."""
        from django.test.utils import CaptureQueriesContext
        draft, questions = draft_quiz()
        with CaptureQueriesContext(connection) as queries:
            Question.objects.create(quiz=draft, type='mcq', text='Locked write')
        self.assertTrue(any('FOR NO KEY UPDATE' in q['sql'] for q in queries.captured_queries))
