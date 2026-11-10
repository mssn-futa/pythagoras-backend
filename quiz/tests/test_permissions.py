"""Role and object permission matrix for every legacy quiz path and alias."""
from django.test import TestCase

from quiz.models import Course, Option, Question, Quiz, Submission

from .helpers import QuizAPITestMixin, make_quiz


class PermissionMatrixTests(QuizAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Active', legacy_text_question=True)
        self.hidden_quiz, self.hidden_questions = make_quiz('Hidden', active=False, points=(1,))
        self.question = self.questions[0][0]
        self.legacy_question = self.quiz.question_set.get(type=Question.Type.text)
        self.own = self.submission(self.quiz, self.student)
        self.others = self.submission(self.quiz, self.other_student)

    def question_body(self):
        return {'quiz': self.quiz.pk, 'type': 'mcq', 'text': 'New question', 'points': 1,
                'options': [{'text': label, 'is_correct': label == 'A'} for label in 'ABCD']}

    def quiz_body(self):
        # ASSESS-02: is_active/status are derived lifecycle state, not writable.
        return {'title': 'Changed', 'type': 'arabic', 'description': 'd', 'time_limit': 10}

    def submit_body(self):
        question, correct, _ = self.questions[0]
        return {'quiz': self.quiz.pk, 'answers': {str(question.pk): correct.pk}}

    def detail_paths(self, base, pk):
        """Canonical slash route and the legacy no-slash alias."""
        return [f'{base}{pk}/', f'{base}{pk}']

    def snapshot(self):
        return (
            list(Course.objects.values_list('pk', 'title')),
            list(Quiz.objects.order_by('pk').values_list('pk', 'title', 'is_active')),
            list(Question.objects.order_by('pk').values_list('pk', 'text', 'type')),
            list(Option.objects.order_by('pk').values_list('pk', 'text', 'is_correct')),
            list(Submission.objects.order_by('pk').values_list('pk', 'student_id', 'score', 'answers')),
        )

    def write_requests(self):
        requests = [
            ('post', '/quiz/courses/', {'code': 'ARB101', 'title': 'Arabic', 'department': 'Arabic',
                                        'level': '100', 'semester': 'First'}),
            ('post', '/quiz/question/', self.question_body()),
            ('post', '/quiz/quizzes/', self.quiz_body()),
        ]
        for path in self.detail_paths('/quiz/question/', self.question.pk):
            requests += [('put', path, self.question_body()), ('delete', path, None)]
        for path in self.detail_paths('/quiz/quizzes/', self.quiz.pk):
            requests += [('put', path, self.quiz_body()), ('delete', path, None)]
        return requests

    def read_requests(self):
        requests = [
            ('get', '/quiz/courses/', None),
            ('get', '/quiz/quizzes/', None),
            ('get', '/quiz/submissions/', None),
        ]
        for path in self.detail_paths('/quiz/question/', self.question.pk):
            requests.append(('get', path, None))
        for path in self.detail_paths('/quiz/quizzes/', self.quiz.pk):
            requests.append(('get', path, None))
        for path in self.detail_paths('/quiz/submissions/', self.own.pk):
            requests.append(('get', path, None))
        return requests

    def admin_only_reads(self):
        return [('get', '/quiz/question/', None),
                ('get', f'/quiz/quizzes/{self.quiz.pk}/submissions/', None)]

    def all_requests(self):
        return (self.read_requests() + self.admin_only_reads() + self.write_requests()
                + [('post', '/quiz/submissions/', self.submit_body())])

    def test_anonymous_is_rejected_everywhere(self):
        before = self.snapshot()
        for method, path, body in self.all_requests():
            with self.subTest(method=method, path=path):
                self.assertEnvelopeError(self.call(None, method, path, body), 401,
                                         'AUTHENTICATION_REQUIRED')
        self.assertEqual(self.snapshot(), before)

    def test_inactive_accounts_are_rejected_everywhere(self):
        before = self.snapshot()
        for user in (self.inactive_student, self.inactive_admin):
            for method, path, body in self.all_requests():
                with self.subTest(user=user.email, method=method, path=path):
                    self.assertEnvelopeError(self.call(user, method, path, body), 403,
                                             'ACCOUNT_INACTIVE')
        self.assertEqual(self.snapshot(), before)

    def test_students_cannot_author_or_read_admin_paths(self):
        """Also a staff (Django-admin) student: is_staff grants no product authority."""
        before = self.snapshot()
        for user in (self.student, self.staff_student):
            for method, path, body in self.write_requests() + self.admin_only_reads():
                with self.subTest(user=user.email, method=method, path=path):
                    self.assertEnvelopeError(self.call(user, method, path, body), 403,
                                             'PERMISSION_DENIED')
        self.assertEqual(self.snapshot(), before)

    def test_student_reads(self):
        for method, path, body in self.read_requests():
            with self.subTest(path=path):
                response = self.call(self.student, method, path, body)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertIs(response.json()['success'], True)
                self.assertNoAnswerKeys(response)

    def test_student_cannot_see_hidden_or_legacy_objects(self):
        hidden_question = self.hidden_questions[0][0]
        paths = (self.detail_paths('/quiz/quizzes/', self.hidden_quiz.pk)
                 + self.detail_paths('/quiz/question/', hidden_question.pk)
                 + self.detail_paths('/quiz/question/', self.legacy_question.pk)
                 + self.detail_paths('/quiz/quizzes/', 999999))
        for path in paths:
            with self.subTest(path=path):
                self.assertEnvelopeError(self.call(self.student, 'get', path), 404, 'NOT_FOUND')
        listed = self.call(self.student, 'get', '/quiz/quizzes/').json()['data']
        self.assertEqual([row['id'] for row in listed], [self.quiz.pk])

    def test_student_cannot_read_another_students_submission(self):
        for path in self.detail_paths('/quiz/submissions/', self.others.pk):
            with self.subTest(path=path):
                response = self.call(self.student, 'get', path)
                self.assertEnvelopeError(response, 404, 'NOT_FOUND')
                self.assertNotIn('other@example.test', response.content.decode())

    def test_history_contains_only_own_submissions(self):
        response = self.call(self.student, 'get', '/quiz/submissions/')
        self.assertEqual([row['id'] for row in response.json()['data']], [self.own.pk])
        response = self.call(self.admin, 'get', '/quiz/submissions/')
        self.assertEqual(response.json()['data'], [])

    def test_admin_cannot_submit_as_a_student(self):
        self.assertEnvelopeError(self.call(self.admin, 'post', '/quiz/submissions/',
                                           self.submit_body()), 403, 'PERMISSION_DENIED')
        self.assertEqual(Submission.objects.filter(student=self.admin).count(), 0)

    def test_no_manual_grade_mutation_on_submission_detail(self):
        before = self.snapshot()
        for user in (self.student, self.admin):
            for path in self.detail_paths('/quiz/submissions/', self.own.pk):
                for method in ('patch', 'put', 'delete'):
                    with self.subTest(user=user.email, method=method, path=path):
                        response = self.call(user, method, path, {'score': 100})
                        self.assertEnvelopeError(response, 405, 'METHOD_NOT_ALLOWED')
        self.assertEqual(self.snapshot(), before)

    def test_admin_reads_everything_including_hidden(self):
        reads = self.read_requests() + self.admin_only_reads() + [
            ('get', path, None) for path in
            self.detail_paths('/quiz/quizzes/', self.hidden_quiz.pk)
            + self.detail_paths('/quiz/question/', self.legacy_question.pk)
            + self.detail_paths('/quiz/submissions/', self.others.pk)]
        for method, path, body in reads:
            with self.subTest(path=path):
                response = self.call(self.admin, method, path, body)
                self.assertEqual(response.status_code, 200, response.content)

    def test_admin_writes(self):
        response = self.call(self.admin, 'post', '/quiz/courses/', {
            'code': 'ARB101', 'title': 'Arabic', 'department': 'Arabic', 'level': '100',
            'semester': 'First'})
        self.assertEqual(response.status_code, 201, response.content)
        response = self.call(self.admin, 'post', '/quiz/quizzes/', self.quiz_body())
        self.assertEqual(response.status_code, 201, response.content)
        created_quiz = response.json()['data']['id']
        for path in self.detail_paths('/quiz/quizzes/', created_quiz):
            response = self.call(self.admin, 'put', path, self.quiz_body())
            self.assertEqual(response.status_code, 200, response.content)
        # Questions are authored in a draft quiz (the fixture quiz is published).
        question_body = {**self.question_body(), 'quiz': created_quiz}
        response = self.call(self.admin, 'post', '/quiz/question/', question_body)
        self.assertEqual(response.status_code, 201, response.content)
        created_question = response.json()['data']['id']
        for path in self.detail_paths('/quiz/question/', created_question):
            response = self.call(self.admin, 'put', path, question_body)
            self.assertEqual(response.status_code, 200, response.content)
        response = self.call(self.admin, 'delete', f'/quiz/question/{created_question}')
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b'')
        self.assertFalse(Question.objects.filter(pk=created_question).exists())
        response = self.call(self.admin, 'delete', f'/quiz/quizzes/{created_quiz}/')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Quiz.objects.filter(pk=created_quiz).exists())

    def test_quiz_with_submissions_is_not_deleted(self):
        for path in self.detail_paths('/quiz/quizzes/', self.quiz.pk):
            with self.subTest(path=path):
                self.assertEnvelopeError(self.call(self.admin, 'delete', path), 409,
                                         'RESOURCE_REFERENCED')
        self.assertTrue(Quiz.objects.filter(pk=self.quiz.pk).exists())
        self.assertEqual(Submission.objects.filter(quiz=self.quiz).count(), 2)

    def test_admin_result_rows_include_student_identity(self):
        response = self.call(self.admin, 'get', f'/quiz/quizzes/{self.quiz.pk}/submissions/')
        rows = response.json()['data']
        self.assertEqual({row['student_email'] for row in rows},
                         {'student@example.test', 'other@example.test'})
        self.assertEqual({row['student_name'] for row in rows}, {'Amina Yusuf'})
        self.assertEnvelopeError(self.call(self.admin, 'get', '/quiz/quizzes/999999/submissions/'),
                                 404, 'NOT_FOUND')
