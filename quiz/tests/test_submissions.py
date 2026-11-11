"""POST /quiz/submissions/ (ASSESS-04): server-owned grading, validation, replay, expiry."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase

from quiz import lifecycle
from quiz.models import Attempt, Option, Question, Submission
from quiz.services import InvalidAnswers, QuizService

from .helpers import QuizAPITestMixin, make_quiz


class SubmissionTests(QuizAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Arabic week 1', points=(2, 3),
                                              legacy_text_question=True)
        self.other_quiz, self.other_questions = make_quiz('Other quiz', points=(1,))
        (self.q1, self.q1_correct, self.q1_wrong), (self.q2, self.q2_correct, self.q2_wrong) = (
            self.questions)
        self.legacy_question = self.quiz.question_set.get(type=Question.Type.text)
        self.attempt, _ = lifecycle.start_attempt(self.quiz.pk, self.student)

    def post(self, body, user=None):
        return self.call(user or self.student, 'post', '/quiz/submissions/', body)

    def body(self, answers=None, attempt=None):
        return {'attempt_id': (attempt or self.attempt).pk,
                'answers': self.answers() if answers is None else answers}

    def answers(self):
        return {str(self.q1.pk): self.q1_correct.pk, str(self.q2.pk): self.q2_wrong[0].pk}

    def assertUnconsumed(self):
        self.assertFalse(Submission.objects.exists())
        self.attempt.refresh_from_db()
        self.assertEqual((self.attempt.status, self.attempt.finalized_at), ('in_progress', None))

    # -- happy path and server authority ------------------------------------

    def test_submission_is_graded_on_the_server_with_weighted_points(self):
        response = self.post(self.body())
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body['message'], 'Result saved.')
        data = body['data']
        self.assertEqual(set(data), {'id', 'quiz_id', 'quiz_title', 'attempt_id', 'status',
                                     'score', 'total_points', 'submitted_at', 'graded_at'})
        # q1 (2 points) right, q2 (3 points) wrong; the legacy text question is not counted.
        self.assertEqual((data['score'], data['total_points']), (2, 5))
        self.assertEqual((data['quiz_id'], data['quiz_title'], data['attempt_id'], data['status']),
                         (self.quiz.pk, 'Arabic week 1', self.attempt.pk, 'submitted'))
        self.assertTrue(data['submitted_at'].endswith('Z'))
        self.assertEqual(data['graded_at'], data['submitted_at'])
        saved = Submission.objects.get(pk=data['id'])
        self.assertEqual((saved.student, saved.attempt, saved.score, saved.total_points,
                          saved.is_legacy), (self.student, self.attempt, 2, 5, False))
        self.assertEqual(saved.answers, self.answers())
        self.assertEqual(saved.grading_snapshot['score'], 2)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, 'submitted')
        self.assertEqual(self.attempt.finalized_at, saved.submitted_at)
        self.assertLess(saved.submitted_at, self.attempt.expires_at)

    def test_weighted_all_correct_partial_and_unanswered(self):
        cases = [
            ({str(self.q1.pk): self.q1_correct.pk, str(self.q2.pk): self.q2_correct.pk}, 5),
            ({str(self.q2.pk): self.q2_correct.pk}, 3),
            ({str(self.q1.pk): self.q1_wrong[1].pk}, 0),
            ({}, 0),  # every question unanswered scores zero over the full denominator
        ]
        for index, (answers, score) in enumerate(cases):
            student = self.add_student(f'case{index}@example.test')
            attempt, _ = lifecycle.start_attempt(self.quiz.pk, student)
            with self.subTest(answers=answers):
                response = self.post(self.body(answers, attempt), user=student)
                self.assertEqual(response.status_code, 201, response.content)
                data = response.json()['data']
                self.assertEqual((data['score'], data['total_points']), (score, 5))
                snapshot = Submission.objects.get(pk=data['id']).grading_snapshot
                self.assertEqual([row['selected_option_id'] for row in snapshot['questions']],
                                 [answers.get(str(self.q1.pk)), answers.get(str(self.q2.pk))])

    def test_client_identity_score_and_timing_are_rejected(self):
        forged = [
            {'student': self.other_student.pk}, {'student_id': self.other_student.pk},
            {'score': 999}, {'total_points': 5}, {'graded_at': '2026-01-01T00:00:00Z'},
            {'submitted_at': '2026-01-01T00:00:00Z'}, {'finalized_at': '2026-01-01T00:00:00Z'},
            {'id': 77}, {'status': 'submitted'},
        ]
        for extra in forged:
            with self.subTest(extra=extra):
                body = self.assertEnvelopeError(self.post({**self.body(), **extra}), 400,
                                                'VALIDATION_ERROR')
                self.assertEqual(set(body['data']['fields']), set(extra))
        self.assertUnconsumed()

    def test_retired_direct_submission_is_rejected(self):
        """ASSESS-01's {"quiz", "answers"} body cannot create/finalize an attempt."""
        newcomer = self.add_student('newcomer@example.test')
        for user in (self.student, newcomer):
            for body in ({'quiz': self.quiz.pk, 'answers': self.answers()},
                         {'quiz': self.quiz.pk, 'attempt_id': self.attempt.pk,
                          'answers': self.answers()}):
                with self.subTest(user=user.email, body=sorted(body)):
                    payload = self.assertEnvelopeError(self.post(body, user=user), 400,
                                                       'VALIDATION_ERROR')
                    self.assertIn('attempts/', payload['data']['fields']['quiz'][0])
        self.assertFalse(Attempt.objects.filter(student=newcomer).exists())
        self.assertUnconsumed()

    # -- malformed payloads --------------------------------------------------

    def assertRejected(self, body, field, key=None):
        payload = self.assertEnvelopeError(self.post(body), 400, 'VALIDATION_ERROR')
        self.assertIn(field, payload['data']['fields'], payload)
        if key is not None:
            self.assertIn(key, payload['data']['fields'][field], payload)
        self.assertUnconsumed()
        return payload

    def test_malformed_answer_structures(self):
        for answers in ([], [self.q1_correct.pk], 'B', 5, None, True):
            with self.subTest(answers=answers):
                self.assertRejected({'attempt_id': self.attempt.pk, 'answers': answers}, 'answers')
        self.assertRejected({'attempt_id': self.attempt.pk}, 'answers')

    def test_malformed_attempt_ids(self):
        for value in (None, 'abc', True, False, 0, -1, [self.attempt.pk], str(self.attempt.pk),
                      float(self.attempt.pk), {'id': self.attempt.pk}):
            with self.subTest(attempt_id=value):
                self.assertRejected({'attempt_id': value, 'answers': {}}, 'attempt_id')
        self.assertRejected({'answers': {}}, 'attempt_id')

    def test_foreign_and_missing_attempts_are_404(self):
        others, _ = lifecycle.start_attempt(self.quiz.pk, self.other_student)
        for attempt_id in (others.pk, 999999):
            with self.subTest(attempt_id=attempt_id):
                response = self.post({'attempt_id': attempt_id, 'answers': {}})
                self.assertEnvelopeError(response, 404, 'NOT_FOUND')
        # Indistinguishable: a foreign attempt reveals nothing about its owner.
        self.assertEqual(self.post({'attempt_id': others.pk, 'answers': {}}).json(),
                         self.post({'attempt_id': 999999, 'answers': {}}).json())
        others.refresh_from_db()
        self.assertEqual(others.status, 'in_progress')
        self.assertFalse(Submission.objects.exists())

    def test_malformed_question_keys(self):
        q1 = self.q1.pk
        foreign_question = self.other_questions[0][0]
        for key in (f'0{q1}', f' {q1}', f'+{q1}', f'{q1}.0', f'{q1} ', f'-{q1}', 'abc', '0', '',
                    '999999', str(foreign_question.pk)):
            with self.subTest(key=key):
                self.assertRejected(self.body({key: self.q1_correct.pk}), 'answers', key)

    def test_malformed_answer_values(self):
        key = str(self.q1.pk)
        foreign_same_quiz = self.q2_correct.pk
        foreign_other_quiz = self.other_questions[0][1].pk
        for value in (True, False, str(self.q1_correct.pk), float(self.q1_correct.pk),
                      [self.q1_correct.pk], {'id': self.q1_correct.pk}, None, 'B',
                      foreign_same_quiz, foreign_other_quiz, 999999, -1, 0):
            with self.subTest(value=value):
                self.assertRejected(self.body({key: value}), 'answers', key)

    def test_one_bad_answer_rejects_the_whole_submission(self):
        payload = self.assertRejected(self.body({str(self.q1.pk): self.q1_correct.pk,
                                                 str(self.q2.pk): self.q1_wrong[0].pk}), 'answers')
        self.assertEqual(set(payload['data']['fields']['answers']), {str(self.q2.pk)})
        # The attempt is not consumed: a corrected submission is accepted.
        self.assertEqual(self.post(self.body()).status_code, 201)

    def test_legacy_question_types_cannot_be_answered(self):
        legacy_option = self.legacy_question.option_set.get()
        self.assertRejected(self.body({str(self.legacy_question.pk): legacy_option.pk}),
                            'answers', str(self.legacy_question.pk))

    def test_non_object_bodies(self):
        for body in ([], ['attempt_id'], 'attempt_id'):
            with self.subTest(body=body):
                self.assertEnvelopeError(self.post(body), 400, 'VALIDATION_ERROR')
        response = self.client_for(self.student).post(
            '/quiz/submissions/', data='{"attempt_id": ', content_type='application/json')
        self.assertEnvelopeError(response, 400, 'MALFORMED_REQUEST')
        self.assertUnconsumed()

    def test_score_capacity_is_checked_before_saving(self):
        # Storage is integer; exercise the same check at a lowered capacity (5 > 4).
        self.enterContext(mock.patch('quiz.lifecycle.MAX_STORED_POINTS', 4))
        self.assertRejected(self.body(), 'quiz')

    def test_grading_failure_rolls_back(self):
        with mock.patch.object(QuizService, 'grade', side_effect=RuntimeError('boom')):
            response = self.post(self.body())
        self.assertEnvelopeError(response, 500, 'INTERNAL_ERROR')
        self.assertNotIn('boom', response.content.decode())
        self.assertUnconsumed()

    def test_failure_after_insert_rolls_back_submission_and_attempt(self):
        original = Attempt.save

        def failing_save(attempt, *args, **kwargs):
            if attempt.status == Attempt.Status.SUBMITTED:
                raise RuntimeError('attempt write failed')
            return original(attempt, *args, **kwargs)

        with mock.patch.object(Attempt, 'save', failing_save):
            response = self.post(self.body())
        self.assertEnvelopeError(response, 500, 'INTERNAL_ERROR')
        self.assertUnconsumed()  # no partial grade

    # -- one finalization: replay, conflict, expiry -------------------------

    def test_identical_replay_returns_saved_result(self):
        first = self.post(self.body())
        self.assertEqual(first.status_code, 201)
        # Same map, different key order: a JSON-identical replay.
        reordered = dict(reversed(list(self.answers().items())))
        replay = self.post(self.body(reordered))
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json()['message'], 'Result already saved.')
        self.assertEqual(replay.json()['data'], first.json()['data'])
        self.assertEqual(Submission.objects.count(), 1)

    def test_replay_after_closing_and_after_expiry(self):
        first = self.post(self.body()).json()['data']
        lifecycle.close_quiz(self.quiz.pk)
        closed = self.post(self.body())
        self.assertEqual((closed.status_code, closed.json()['data']), (200, first))
        late = self.attempt.expires_at + timedelta(days=30)
        with mock.patch('quiz.lifecycle.server_now', return_value=late):
            replay = self.post(self.body())
            conflict = self.post(self.body({}))
        self.assertEqual((replay.status_code, replay.json()['data']), (200, first))
        # Replay is checked before expiry: changed answers are a conflict, not "expired".
        self.assertEnvelopeError(conflict, 409, 'SUBMISSION_CONFLICT')

    def test_changed_answers_conflict_and_never_overwrite(self):
        first = self.post(self.body()).json()['data']
        changed = {str(self.q1.pk): self.q1_correct.pk, str(self.q2.pk): self.q2_correct.pk}
        for answers in (changed, {}, {str(self.q1.pk): self.q1_correct.pk},
                        {str(self.q1.pk): True, str(self.q2.pk): self.q2_wrong[0].pk},
                        {str(self.q1.pk): float(self.q1_correct.pk),
                         str(self.q2.pk): self.q2_wrong[0].pk}):
            with self.subTest(answers=answers):
                self.assertEnvelopeError(self.post(self.body(answers)), 409,
                                         'SUBMISSION_CONFLICT')
        saved = Submission.objects.get()
        self.assertEqual((saved.pk, saved.score, saved.answers), (first['id'], 2, self.answers()))

    def test_exact_expiry_boundary(self):
        """First finalization must arrive strictly before expiry (A10, no grace)."""
        late_student = self.add_student('late@example.test')
        late, _ = lifecycle.start_attempt(self.quiz.pk, late_student)
        with mock.patch('quiz.lifecycle.server_now', return_value=late.expires_at):
            response = self.post(self.body(attempt=late), user=late_student)
            attempt = self.call(late_student, 'get', f'/quiz/attempts/{late.pk}/')
        self.assertEnvelopeError(response, 409, 'ATTEMPT_EXPIRED')
        late.refresh_from_db()
        # No grade, and the expiry is read, not persisted (effective status).
        self.assertEqual((late.status, late.finalized_at), ('in_progress', None))
        self.assertFalse(Submission.objects.filter(attempt=late).exists())
        self.assertEqual(attempt.json()['data']['status'], 'expired')
        self.assertIsNone(attempt.json()['data']['submission_id'])
        # One microsecond before expiry is on time.
        just_before = self.attempt.expires_at - timedelta(microseconds=1)
        with mock.patch('quiz.lifecycle.server_now', return_value=just_before):
            response = self.post(self.body())
        self.assertEqual(response.status_code, 201, response.content)
        saved = Submission.objects.get(attempt=self.attempt)
        self.assertEqual(saved.submitted_at, just_before)

    def test_expired_attempt_rejects_valid_and_invalid_answers_alike(self):
        with mock.patch('quiz.lifecycle.server_now',
                        return_value=self.attempt.expires_at + timedelta(seconds=1)):
            for answers in (self.answers(), {'nope': 1}):
                with self.subTest(answers=answers):
                    self.assertEnvelopeError(self.post(self.body(answers)), 409,
                                             'ATTEMPT_EXPIRED')
        self.assertUnconsumed()

    def test_attempt_persisted_as_expired_stays_rejected(self):
        lifecycle.mark_expired(self.attempt.pk, now=self.attempt.expires_at)
        self.assertEnvelopeError(self.post(self.body()), 409, 'ATTEMPT_EXPIRED')
        self.assertFalse(Submission.objects.exists())

    def test_closing_does_not_block_an_unexpired_attempt(self):
        lifecycle.close_quiz(self.quiz.pk)
        response = self.post(self.body())
        self.assertEqual(response.status_code, 201, response.content)

    def test_metadata_correction_keeps_result_title(self):
        first = self.post(self.body()).json()['data']
        quiz = type(self.quiz).objects.get(pk=self.quiz.pk)
        quiz.title = 'Arabic week 1 (corrected)'
        quiz.save()
        result = self.call(self.student, 'get', f"/quiz/submissions/{first['id']}/").json()['data']
        self.assertEqual(result, first)  # the result as graded, title included
        attempt = self.call(self.student, 'get', f'/quiz/attempts/{self.attempt.pk}/').json()
        self.assertEqual(attempt['data']['quiz_title'], 'Arabic week 1 (corrected)')

    def test_other_students_submit_independently(self):
        self.assertEqual(self.post(self.body()).status_code, 201)
        others, _ = lifecycle.start_attempt(self.quiz.pk, self.other_student)
        response = self.post(self.body({}, others), user=self.other_student)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Submission.objects.filter(quiz=self.quiz).count(), 2)

    def test_only_active_students_submit(self):
        for user, code in ((self.admin, 'PERMISSION_DENIED'),
                           (self.staff_student, None),
                           (self.inactive_student, 'ACCOUNT_INACTIVE')):
            with self.subTest(user=user.email):
                response = self.post(self.body(), user=user)
                if code is None:  # a staff student is still a student: not their attempt
                    self.assertEnvelopeError(response, 404, 'NOT_FOUND')
                else:
                    self.assertEnvelopeError(response, 403, code)
        self.assertUnconsumed()


class AnswerValidationServiceTests(TestCase):
    """Reusable grading service boundary."""

    def setUp(self):
        # A draft, so a test can shape legacy-like content (published content is frozen).
        self.quiz, self.questions = make_quiz('Service', points=(2, 3), active=False)

    def test_validate_and_grade(self):
        (q1, c1, w1), (q2, c2, _) = self.questions
        answers = QuizService.validate_answers(self.quiz, {str(q1.pk): c1.pk, str(q2.pk): c2.pk})
        self.assertEqual(QuizService.grade_answers(self.quiz, answers), 5)
        self.assertEqual(QuizService.grade_answers(self.quiz, {str(q1.pk): w1[0].pk}), 0)
        self.assertEqual(QuizService.available_points(self.quiz), 5)

    def test_question_with_several_correct_options_awards_nothing(self):
        (q1, c1, w1), _ = self.questions
        # Legacy-shaped data: a published question cannot be edited any more.
        Option.objects.filter(pk=w1[0].pk).update(is_correct=True)
        self.assertEqual(QuizService.grade_answers(self.quiz, {str(q1.pk): c1.pk}), 0)

    def test_invalid_answers_collects_every_error(self):
        (q1, _, _), (q2, _, _) = self.questions
        with self.assertRaises(InvalidAnswers) as caught:
            QuizService.validate_answers(self.quiz, {str(q1.pk): 'x', str(q2.pk): [1], 'nope': 1})
        self.assertEqual(set(caught.exception.errors), {str(q1.pk), str(q2.pk), 'nope'})

    def test_direct_submission_service_is_retired(self):
        self.assertFalse(hasattr(QuizService, 'submit'))
