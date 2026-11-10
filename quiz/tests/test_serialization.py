"""Answer-key leakage, role-specific serializers and repaired handler dispatch."""
from django.test import TestCase

from quiz.models import Option, Question
from quiz.serializers import (
    StudentQuestionSerializer, StudentQuizSerializer, StudentSubmissionSerializer,
)

from .helpers import KEY_FIELDS, QuizAPITestMixin, make_quiz, walk_keys


class AnswerKeyLeakageTests(QuizAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Leak check', legacy_text_question=True)

    def submit(self):
        question, correct, _ = self.questions[0]
        return self.call(self.student, 'post', '/quiz/submissions/',
                         {'quiz': self.quiz.pk, 'answers': {str(question.pk): correct.pk}})

    def test_walker_detects_nested_keys(self):
        self.assertIn('is_correct', set(walk_keys({'a': [{'b': [{'is_correct': True}]}]})))

    def test_every_student_response_is_key_free(self):
        created = self.submit()
        self.assertEqual(created.status_code, 201, created.content)
        submission_id = created.json()['data']['id']
        question = self.questions[0][0]
        responses = {
            'submit': created,
            'replay': self.submit(),
            'courses': self.call(self.student, 'get', '/quiz/courses/'),
            'quiz list': self.call(self.student, 'get', '/quiz/quizzes/'),
            'quiz detail': self.call(self.student, 'get', f'/quiz/quizzes/{self.quiz.pk}/'),
            'quiz alias': self.call(self.student, 'get', f'/quiz/quizzes/{self.quiz.pk}'),
            'question detail': self.call(self.student, 'get', f'/quiz/question/{question.pk}/'),
            'question alias': self.call(self.student, 'get', f'/quiz/question/{question.pk}'),
            'history': self.call(self.student, 'get', '/quiz/submissions/'),
            'result': self.call(self.student, 'get', f'/quiz/submissions/{submission_id}/'),
            'result alias': self.call(self.student, 'get', f'/quiz/submissions/{submission_id}'),
        }
        for name, response in responses.items():
            with self.subTest(response=name):
                self.assertIn(response.status_code, (200, 201), response.content)
                self.assertNoAnswerKeys(response)

    def test_student_quiz_detail_shape(self):
        data = self.call(self.student, 'get', f'/quiz/quizzes/{self.quiz.pk}/').json()['data']
        # Only release MCQ questions; the legacy text question is admin-only.
        self.assertEqual([q['id'] for q in data['questions']], [q.pk for q, _, _ in self.questions])
        self.assertEqual(data['question_count'], 2)
        self.assertEqual(data['total_points'], 5)
        first = data['questions'][0]
        self.assertEqual(set(first), {'id', 'quiz', 'text', 'points', 'order', 'options'})
        self.assertEqual(len(first['options']), 4)
        for option in first['options']:
            self.assertEqual(set(option), {'id', 'text', 'order'})
        listed = self.call(self.student, 'get', '/quiz/quizzes/').json()['data']['results'][0]
        self.assertEqual(listed['question_count'], 2)

    def test_error_responses_do_not_echo_keys(self):
        question, correct, _ = self.questions[0]
        response = self.call(self.student, 'post', '/quiz/submissions/',
                             {'quiz': self.quiz.pk, 'answers': {str(question.pk): 'B'}})
        self.assertEqual(response.status_code, 400)
        self.assertNoAnswerKeys(response)

    def test_student_serializers_declare_no_key_fields(self):
        for serializer_class in (StudentQuizSerializer, StudentQuestionSerializer,
                                 StudentSubmissionSerializer):
            with self.subTest(serializer=serializer_class.__name__):
                self.assertFalse(KEY_FIELDS.intersection(serializer_class().fields))

    def test_admin_detail_contains_keys(self):
        data = self.call(self.admin, 'get', f'/quiz/quizzes/{self.quiz.pk}/').json()['data']
        self.assertEqual(data['question_count'], 3)
        options = data['questions'][0]['options']
        self.assertEqual([o['is_correct'] for o in options], [False, True, False, False])
        self.submit()
        submission_id = self.call(self.student, 'get', '/quiz/submissions/').json()['data'][0]['id']
        admin_result = self.call(self.admin, 'get', f'/quiz/submissions/{submission_id}/').json()['data']
        self.assertEqual(admin_result['student_email'], 'student@example.test')
        self.assertIn('is_correct', set(walk_keys(admin_result)))


class DispatchRegressionTests(QuizAPITestMixin, TestCase):
    """Handlers that previously raised NameError/TypeError/AttributeError (500)."""

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Dispatch')

    def test_question_list_get(self):
        response = self.call(self.admin, 'get', '/quiz/question/')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([q['id'] for q in response.json()['data']['results']],
                         sorted(q.pk for q, _, _ in self.questions))

    def test_question_create_and_replace_return_the_question(self):
        # Authoring targets a draft (published content is immutable since ASSESS-02).
        draft, _ = make_quiz('Dispatch draft', active=False)
        body = {'quiz': draft.pk, 'text': 'Pick B', 'points': 2,
                'options': [{'text': label, 'is_correct': label == 'B'} for label in 'ABCD']}
        response = self.call(self.admin, 'post', '/quiz/question/', body)
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()['data']
        self.assertEqual(data['type'], 'mcq')  # release default
        self.assertEqual([o['text'] for o in data['options']], list('ABCD'))
        self.assertEqual(Option.objects.filter(question_id=data['id'], is_correct=True).count(), 1)
        body['options'] = [{'text': label, 'is_correct': label == 'C'} for label in 'ABCD']
        response = self.call(self.admin, 'put', f"/quiz/question/{data['id']}/", body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([o['is_correct'] for o in response.json()['data']['options']],
                         [False, False, True, False])

    def test_authoring_rejects_non_release_question_types(self):
        for question_type in ('text', 'multi', 'MULTIPLE_ANSWER'):
            with self.subTest(type=question_type):
                response = self.call(self.admin, 'post', '/quiz/question/', {
                    'quiz': self.quiz.pk, 'type': question_type, 'text': 'Essay', 'options': []})
                body = self.assertEnvelopeError(response, 400, 'VALIDATION_ERROR')
                self.assertIn('type', body['data']['fields'])
        self.assertFalse(Question.objects.exclude(type=Question.Type.mcq).exists())

    def test_submission_detail_dispatches_pk_on_both_routes(self):
        question, correct, _ = self.questions[0]
        created = self.call(self.student, 'post', '/quiz/submissions/',
                            {'quiz': self.quiz.pk, 'answers': {str(question.pk): correct.pk}})
        submission_id = created.json()['data']['id']
        for path in (f'/quiz/submissions/{submission_id}/', f'/quiz/submissions/{submission_id}'):
            for user in (self.student, self.admin):
                with self.subTest(path=path, user=user.email):
                    response = self.call(user, 'get', path)
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertEqual(response.json()['data']['id'], submission_id)


class LegacyQuestionConversionTests(QuizAPITestMixin, TestCase):
    """Legacy text/multi questions are never turned into student-visible MCQs."""

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Conversion', legacy_text_question=True)
        self.legacy = self.quiz.question_set.get(type=Question.Type.text)

    def test_put_without_or_with_type_does_not_convert_legacy_question(self):
        full_options = [{'text': label, 'is_correct': label == 'A'} for label in 'ABCD']
        bodies = [
            {'quiz': self.quiz.pk, 'text': 'Edited', 'points': 1},
            {'quiz': self.quiz.pk, 'type': 'mcq', 'text': 'Edited', 'points': 1},
            {'quiz': self.quiz.pk, 'type': 'mcq', 'text': 'Edited', 'points': 1,
             'options': full_options},
        ]
        for body in bodies:
            for path in (f'/quiz/question/{self.legacy.pk}/', f'/quiz/question/{self.legacy.pk}'):
                with self.subTest(body=body, path=path):
                    response = self.call(self.admin, 'put', path, body)
                    payload = self.assertEnvelopeError(response, 400, 'VALIDATION_ERROR')
                    self.assertIn('type', payload['data']['fields'])
        self.legacy.refresh_from_db()
        self.assertEqual((self.legacy.type, self.legacy.text),
                         (Question.Type.text, 'Conversion legacy text question'))
        self.assertEqual(list(self.legacy.option_set.values_list('text', flat=True)),
                         ['legacy answer'])
        # Still hidden from students everywhere.
        self.assertEnvelopeError(self.call(self.student, 'get', f'/quiz/question/{self.legacy.pk}/'),
                                 404, 'NOT_FOUND')
        detail = self.call(self.student, 'get', f'/quiz/quizzes/{self.quiz.pk}/')
        self.assertNotIn(self.legacy.pk, [q['id'] for q in detail.json()['data']['questions']])
        self.assertNotIn('legacy answer', detail.content.decode())

    def test_put_without_type_keeps_mcq_question_mcq(self):
        # Published questions are immutable (ASSESS-02); edit a draft's question.
        draft, questions = make_quiz('Draft conversion', active=False)
        question = questions[0][0]
        response = self.call(self.admin, 'put', f'/quiz/question/{question.pk}/',
                             {'quiz': draft.pk, 'text': 'Edited MCQ', 'points': 4})
        self.assertEqual(response.status_code, 200, response.content)
        question.refresh_from_db()
        self.assertEqual((question.type, question.text, question.points),
                         (Question.Type.mcq, 'Edited MCQ', 4))
        self.assertEqual(question.option_set.count(), 4)  # options untouched when omitted


class QuizTitleFieldTests(QuizAPITestMixin, TestCase):

    def test_titles_are_present_and_null_without_relations(self):
        quiz, _ = make_quiz('No relations')
        for user, path in ((self.student, '/quiz/quizzes/'), (self.admin, '/quiz/quizzes/'),
                           (self.student, f'/quiz/quizzes/{quiz.pk}/'),
                           (self.admin, f'/quiz/quizzes/{quiz.pk}/')):
            with self.subTest(user=user.email, path=path):
                data = self.call(user, 'get', path).json()['data']
                row = data['results'][0] if 'results' in data else data
                self.assertIn('course_title', row)
                self.assertIn('event_title', row)
                self.assertIsNone(row['course_title'])
                self.assertIsNone(row['event_title'])
