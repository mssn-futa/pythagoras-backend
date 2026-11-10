"""Legacy direct submission: server-owned identity/score, validation, replay."""
from unittest import mock

from django.test import TestCase

from quiz.models import Question, Quiz, Submission
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

    def post(self, body, user=None):
        return self.call(user or self.student, 'post', '/quiz/submissions/', body)

    def answers(self):
        return {str(self.q1.pk): self.q1_correct.pk, str(self.q2.pk): self.q2_wrong[0].pk}

    # -- happy path and server authority ------------------------------------

    def test_submission_is_graded_on_the_server_with_weighted_points(self):
        response = self.post({'quiz': self.quiz.pk, 'answers': self.answers()})
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()['data']
        self.assertEqual(data['score'], 2)  # q1 (2 points) right, q2 (3 points) wrong
        self.assertEqual(data['quiz'], self.quiz.pk)
        self.assertEqual(data['quiz_title'], 'Arabic week 1')
        self.assertIs(data['is_graded'], True)
        self.assertTrue(data['submitted_at'].endswith('Z'))
        self.assertNotIn('student', data)
        saved = Submission.objects.get(pk=data['id'])
        self.assertEqual(saved.student, self.student)
        self.assertEqual(saved.score, 2)
        self.assertIsNotNone(saved.graded_at)
        self.assertEqual(saved.answers, self.answers())

    def test_all_correct_and_missing_answers(self):
        cases = [
            ({str(self.q1.pk): self.q1_correct.pk, str(self.q2.pk): self.q2_correct.pk}, 5),
            ({str(self.q2.pk): self.q2_correct.pk}, 3),
            ({}, 0),  # missing answers score zero; no fabricated result for other students
        ]
        for index, (answers, score) in enumerate(cases):
            student = self.make_user(f'case{index}@example.test')
            self._tokens[student.pk] = self.access(student)
            with self.subTest(answers=answers):
                response = self.post({'quiz': self.quiz.pk, 'answers': answers}, user=student)
                self.assertEqual(response.status_code, 201, response.content)
                self.assertEqual(response.json()['data']['score'], score)

    def test_client_identity_and_score_are_rejected(self):
        forged = [
            {'student': self.other_student.pk},
            {'score': 999},
            {'graded_at': '2026-01-01T00:00:00Z'},
            {'submitted_at': '2026-01-01T00:00:00Z'},
            {'total_points': 5},
            {'id': 77},
        ]
        for extra in forged:
            with self.subTest(extra=extra):
                body = {'quiz': self.quiz.pk, 'answers': self.answers(), **extra}
                response = self.post(body)
                body = self.assertEnvelopeError(response, 400, 'VALIDATION_ERROR')
                self.assertEqual(set(body['data']['fields']), set(extra))
        self.assertFalse(Submission.objects.exists())

    # -- malformed payloads --------------------------------------------------

    def assertRejected(self, body, field, key=None):
        response = self.post(body)
        payload = self.assertEnvelopeError(response, 400, 'VALIDATION_ERROR')
        self.assertIn(field, payload['data']['fields'], payload)
        if key is not None:
            self.assertIn(key, payload['data']['fields'][field], payload)
        self.assertFalse(Submission.objects.exists())

    def test_malformed_answer_structures(self):
        for answers in ([], [self.q1_correct.pk], 'B', 5, None, True):
            with self.subTest(answers=answers):
                self.assertRejected({'quiz': self.quiz.pk, 'answers': answers}, 'answers')
        self.assertRejected({'quiz': self.quiz.pk}, 'answers')

    def test_malformed_question_keys(self):
        q1 = self.q1.pk
        foreign_question = self.other_questions[0][0]
        for key in (f'0{q1}', f' {q1}', f'+{q1}', f'{q1}.0', f'{q1} ', f'-{q1}', 'abc', '0', '',
                    '999999', str(foreign_question.pk)):
            with self.subTest(key=key):
                self.assertRejected({'quiz': self.quiz.pk,
                                     'answers': {key: self.q1_correct.pk}}, 'answers', key)

    def test_malformed_answer_values(self):
        key = str(self.q1.pk)
        foreign_same_quiz = self.q2_correct.pk
        foreign_other_quiz = self.other_questions[0][1].pk
        for value in (True, False, str(self.q1_correct.pk), float(self.q1_correct.pk),
                      [self.q1_correct.pk], {'id': self.q1_correct.pk}, None, 'B',
                      foreign_same_quiz, foreign_other_quiz, 999999, -1, 0):
            with self.subTest(value=value):
                self.assertRejected({'quiz': self.quiz.pk, 'answers': {key: value}}, 'answers', key)

    def test_legacy_question_types_cannot_be_answered(self):
        legacy_option = self.legacy_question.option_set.get()
        self.assertRejected({'quiz': self.quiz.pk,
                             'answers': {str(self.legacy_question.pk): legacy_option.pk}},
                            'answers', str(self.legacy_question.pk))

    def test_unavailable_or_malformed_quiz(self):
        hidden, _ = make_quiz('Hidden', active=False, points=(1,))
        for quiz in (hidden.pk, 999999, None, 'abc', True, 0, [self.quiz.pk]):
            with self.subTest(quiz=quiz):
                self.assertRejected({'quiz': quiz, 'answers': {}}, 'quiz')
        hidden_response = self.post({'quiz': hidden.pk, 'answers': {}}).json()
        missing_response = self.post({'quiz': 999999, 'answers': {}}).json()
        # A hidden quiz is indistinguishable from a missing one.
        self.assertEqual(hidden_response, missing_response)

    def test_non_object_bodies(self):
        for body in ([], ['quiz'], 'quiz'):
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEnvelopeError(response, 400, 'VALIDATION_ERROR')
        response = self.client_for(self.student).post(
            '/quiz/submissions/', data='{"quiz": ', content_type='application/json')
        self.assertEnvelopeError(response, 400, 'MALFORMED_REQUEST')
        self.assertFalse(Submission.objects.exists())

    def test_score_capacity_is_checked_before_saving(self):
        Question.objects.filter(pk=self.q1.pk).update(points=32767)
        self.assertRejected({'quiz': self.quiz.pk, 'answers': self.answers()}, 'quiz')

    # -- one submission per student and quiz ---------------------------------

    def test_identical_replay_returns_saved_result(self):
        first = self.post({'quiz': self.quiz.pk, 'answers': self.answers()})
        self.assertEqual(first.status_code, 201)
        replay = self.post({'quiz': self.quiz.pk, 'answers': self.answers()})
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json()['data'], first.json()['data'])
        self.assertEqual(Submission.objects.count(), 1)
        # Replay still succeeds after the quiz is no longer available.
        Quiz.objects.filter(pk=self.quiz.pk).update(is_active=False)
        late = self.post({'quiz': self.quiz.pk, 'answers': self.answers()})
        self.assertEqual(late.status_code, 200, late.content)
        self.assertEqual(late.json()['data']['id'], first.json()['data']['id'])

    def test_changed_answers_conflict_and_never_overwrite(self):
        first = self.post({'quiz': self.quiz.pk, 'answers': self.answers()}).json()['data']
        changed = {str(self.q1.pk): self.q1_correct.pk, str(self.q2.pk): self.q2_correct.pk}
        for answers in (changed, {str(self.q1.pk): True, str(self.q2.pk): self.q2_wrong[0].pk}):
            with self.subTest(answers=answers):
                response = self.post({'quiz': self.quiz.pk, 'answers': answers})
                self.assertEnvelopeError(response, 409, 'SUBMISSION_CONFLICT')
        saved = Submission.objects.get()
        self.assertEqual((saved.pk, saved.score, saved.answers),
                         (first['id'], 2, self.answers()))

    def test_other_students_submit_independently(self):
        self.assertEqual(self.post({'quiz': self.quiz.pk, 'answers': self.answers()}).status_code, 201)
        response = self.post({'quiz': self.quiz.pk, 'answers': {}}, user=self.other_student)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Submission.objects.filter(quiz=self.quiz).count(), 2)

    def test_grading_failure_rolls_back(self):
        with mock.patch.object(QuizService, 'grade_answers', side_effect=RuntimeError('boom')):
            response = self.post({'quiz': self.quiz.pk, 'answers': self.answers()})
        self.assertEnvelopeError(response, 500, 'INTERNAL_ERROR')
        self.assertNotIn('boom', response.content.decode())
        self.assertFalse(Submission.objects.exists())


class AnswerValidationServiceTests(TestCase):
    """Reusable service boundary for ASSESS-04."""

    def setUp(self):
        self.quiz, self.questions = make_quiz('Service', points=(2, 3))

    def test_validate_and_grade(self):
        (q1, c1, w1), (q2, c2, _) = self.questions
        answers = QuizService.validate_answers(self.quiz, {str(q1.pk): c1.pk, str(q2.pk): c2.pk})
        self.assertEqual(QuizService.grade_answers(self.quiz, answers), 5)
        self.assertEqual(QuizService.grade_answers(self.quiz, {str(q1.pk): w1[0].pk}), 0)
        self.assertEqual(QuizService.available_points(self.quiz), 5)

    def test_question_with_several_correct_options_awards_nothing(self):
        (q1, c1, w1), _ = self.questions
        w1[0].is_correct = True
        w1[0].save()
        self.assertEqual(QuizService.grade_answers(self.quiz, {str(q1.pk): c1.pk}), 0)

    def test_invalid_answers_collects_every_error(self):
        (q1, _, _), (q2, _, _) = self.questions
        with self.assertRaises(InvalidAnswers) as caught:
            QuizService.validate_answers(self.quiz, {str(q1.pk): 'x', str(q2.pk): [1], 'nope': 1})
        self.assertEqual(set(caught.exception.errors), {str(q1.pk), str(q2.pk), 'nope'})
