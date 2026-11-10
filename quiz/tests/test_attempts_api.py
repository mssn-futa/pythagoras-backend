"""ASSESS-04 attempt start/resume, attempt reads, history, admin results, throttles, schema."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from quiz import lifecycle
from quiz.models import Attempt, Quiz, Submission

from .helpers import KEY_FIELDS, QuizAPITestMixin, insert_legacy_submission, make_quiz, walk_keys
from .test_authoring import iso, json_schema, question_body

ATTEMPT_FIELDS = {'id', 'quiz_id', 'quiz_title', 'status', 'started_at', 'expires_at',
                  'server_time', 'submission_id', 'questions'}
RESULT_FIELDS = {'id', 'quiz_id', 'quiz_title', 'attempt_id', 'status', 'score', 'total_points',
                 'submitted_at', 'graded_at'}
ADMIN_ROW_FIELDS = RESULT_FIELDS | {'student_id', 'student_name', 'student_email', 'is_legacy'}
ADMIN_DETAIL_FIELDS = ADMIN_ROW_FIELDS | {'answers', 'grading_snapshot', 'started_at',
                                          'expires_at'}


def past_deadline_quiz(title):
    """Published while its deadline was in the future; the deadline has now passed."""
    # A day back: TestCase runs inside one transaction whose start is the DB "now".
    deadline = timezone.now() - timedelta(days=1)
    quiz, _ = make_quiz(title, deadline=deadline, published_now=deadline - timedelta(hours=1))
    return quiz


class AttemptStartTests(QuizAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Arabic week 1', points=(2, 3),
                                              legacy_text_question=True)

    def test_start_creates_one_timed_attempt_and_repeats_return_it(self):
        before = lifecycle.server_now()
        response = self.start_api(self.student, self.quiz.pk)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['message'], 'Attempt started.')
        data = response.json()['data']
        self.assertEqual(set(data), ATTEMPT_FIELDS)
        attempt = Attempt.objects.get()
        self.assertEqual((data['id'], data['quiz_id'], data['quiz_title'], data['status'],
                          data['submission_id']),
                         (attempt.pk, self.quiz.pk, 'Arabic week 1', 'in_progress', None))
        self.assertGreaterEqual(attempt.started_at, before)
        self.assertEqual(attempt.expires_at, attempt.started_at + timedelta(minutes=30))
        for field in ('started_at', 'expires_at', 'server_time'):
            self.assertTrue(data[field].endswith('Z'), data[field])
        self.assertGreaterEqual(data['server_time'], data['started_at'])
        # Repeated start (retry, refresh, second tab): same attempt, unchanged timing.
        for _ in range(3):
            again = self.start_api(self.student, self.quiz.pk)
            self.assertEqual(again.status_code, 200, again.content)
            self.assertEqual(again.json()['message'], 'Attempt retrieved.')
            repeat = again.json()['data']
            self.assertEqual({key: repeat[key] for key in ('id', 'started_at', 'expires_at')},
                             {key: data[key] for key in ('id', 'started_at', 'expires_at')})
        self.assertEqual(Attempt.objects.count(), 1)

    def test_attempt_questions_are_mcq_only_ordered_and_key_free(self):
        data = self.start_api(self.student, self.quiz.pk).json()['data']
        self.assertEqual([q['id'] for q in data['questions']], [q.pk for q, _, _ in self.questions])
        for question, (model, correct, wrong) in zip(data['questions'], self.questions):
            self.assertEqual(set(question), {'id', 'text', 'points', 'order', 'options'})
            self.assertEqual((question['points'], question['order']), (model.points, model.order))
            self.assertEqual([o['id'] for o in question['options']],
                             [wrong[0].pk, correct.pk, wrong[1].pk, wrong[2].pk])
            for option in question['options']:
                self.assertEqual(set(option), {'id', 'text', 'order'})
        self.assertNotIn('legacy answer', str(data))
        self.assertFalse(KEY_FIELDS.intersection(walk_keys(data)))

    def test_deadline_caps_expiry(self):
        deadline = timezone.now() + timedelta(minutes=5)
        quiz, _ = make_quiz('Short', deadline=deadline)
        data = self.start_api(self.student, quiz.pk).json()['data']
        self.assertEqual(data['expires_at'], iso(Attempt.objects.get(quiz=quiz).expires_at))
        self.assertEqual(Attempt.objects.get(quiz=quiz).expires_at, deadline)

    def test_new_attempts_need_an_available_quiz(self):
        draft, _ = make_quiz('Draft', active=False)
        closed, _ = make_quiz('Closed')
        lifecycle.close_quiz(closed.pk)
        past = past_deadline_quiz('Past')
        for quiz_id in (draft.pk, 999999):
            with self.subTest(quiz=quiz_id):
                self.assertEnvelopeError(self.start_api(self.student, quiz_id), 404, 'NOT_FOUND')
        # A draft is indistinguishable from a missing quiz.
        self.assertEqual(self.start_api(self.student, draft.pk).json(),
                         self.start_api(self.student, 999999).json())
        for quiz in (closed, past):
            with self.subTest(quiz=quiz.title):
                self.assertEnvelopeError(self.start_api(self.student, quiz.pk), 409,
                                         'QUIZ_UNAVAILABLE')
        self.assertFalse(Attempt.objects.exists())

    def test_resume_after_close_deadline_and_expiry(self):
        started = self.start_api(self.student, self.quiz.pk).json()['data']
        lifecycle.close_quiz(self.quiz.pk)
        resumed = self.start_api(self.student, self.quiz.pk)
        self.assertEqual(resumed.status_code, 200, resumed.content)
        data = resumed.json()['data']
        self.assertEqual((data['id'], data['expires_at'], data['status']),
                         (started['id'], started['expires_at'], 'in_progress'))
        # Still submittable after closing (original expiry), and resumable once submitted.
        answers = {str(self.questions[0][0].pk): self.questions[0][1].pk}
        result = self.submit_api(self.student, started['id'], answers)
        self.assertEqual(result.status_code, 201, result.content)
        after = self.start_api(self.student, self.quiz.pk).json()['data']
        self.assertEqual((after['status'], after['submission_id']),
                         ('submitted', result.json()['data']['id']))
        # A new student cannot start the closed quiz.
        self.assertEnvelopeError(self.start_api(self.other_student, self.quiz.pk), 409,
                                 'QUIZ_UNAVAILABLE')

    def test_resume_of_expired_attempt_reads_expired_without_writing(self):
        attempt, _ = lifecycle.start_attempt(self.quiz.pk, self.student)
        with mock.patch('quiz.lifecycle.server_now', return_value=attempt.expires_at):
            data = self.start_api(self.student, self.quiz.pk).json()['data']
        self.assertEqual((data['id'], data['status'], data['submission_id']),
                         (attempt.pk, 'expired', None))
        self.assertEqual(data['server_time'], iso(attempt.expires_at))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'in_progress')

    def test_legacy_submission_has_used_the_attempt(self):
        insert_legacy_submission(self.quiz.pk, self.student.pk)
        self.assertEnvelopeError(self.start_api(self.student, self.quiz.pk), 409,
                                 'SUBMISSION_CONFLICT')
        self.assertFalse(Attempt.objects.exists())

    def test_draft_is_missing_even_with_a_legacy_submission(self):
        """The draft check precedes the legacy-submission check (no 409 leaks a draft)."""
        draft, _ = make_quiz('Legacy draft', active=False)
        insert_legacy_submission(draft.pk, self.student.pk)
        missing = self.start_api(self.student, 999999)
        for user in (self.student, self.other_student):
            with self.subTest(user=user.email):
                response = self.start_api(user, draft.pk)
                self.assertEnvelopeError(response, 404, 'NOT_FOUND')
                self.assertEqual(response.json(), missing.json())
        self.assertFalse(Attempt.objects.exists())

    def test_start_takes_no_body_and_only_students_start(self):
        payload = self.assertEnvelopeError(
            self.call(self.student, 'post', f'/quiz/quizzes/{self.quiz.pk}/attempts/',
                      {'student': self.other_student.pk}), 400, 'VALIDATION_ERROR')
        self.assertIn('student', payload['data']['fields'])
        self.assertEnvelopeError(self.start_api(self.admin, self.quiz.pk), 403, 'PERMISSION_DENIED')
        self.assertEnvelopeError(self.start_api(self.inactive_student, self.quiz.pk), 403,
                                 'ACCOUNT_INACTIVE')
        self.assertEnvelopeError(self.start_api(None, self.quiz.pk), 401,
                                 'AUTHENTICATION_REQUIRED')
        self.assertFalse(Attempt.objects.exists())
        # A staff (Django-admin) student is a student.
        self.assertEqual(self.start_api(self.staff_student, self.quiz.pk).status_code, 201)


class AttemptReadTests(QuizAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Read', points=(1,))
        self.attempt, _ = lifecycle.start_attempt(self.quiz.pk, self.student)
        self.path = f'/quiz/attempts/{self.attempt.pk}/'

    def test_own_attempt(self):
        response = self.call(self.student, 'get', self.path)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertEqual(set(data), ATTEMPT_FIELDS)
        self.assertEqual((data['id'], data['status'], len(data['questions'])),
                         (self.attempt.pk, 'in_progress', 1))
        self.assertNoAnswerKeys(response)

    def test_refresh_never_moves_timing(self):
        first = self.call(self.student, 'get', self.path).json()['data']
        second = self.call(self.student, 'get', self.path).json()['data']
        self.assertEqual((first['started_at'], first['expires_at']),
                         (second['started_at'], second['expires_at']))
        self.assertGreaterEqual(second['server_time'], first['server_time'])

    def test_other_students_and_missing_attempts_are_404(self):
        for user, path in ((self.other_student, self.path),
                           (self.staff_student, self.path),
                           (self.student, '/quiz/attempts/999999/')):
            with self.subTest(user=user.email, path=path):
                response = self.call(user, 'get', path)
                self.assertEnvelopeError(response, 404, 'NOT_FOUND')
                self.assertNotIn('student@example.test', response.content.decode())

    def test_non_students_are_refused(self):
        self.assertEnvelopeError(self.call(self.admin, 'get', self.path), 403, 'PERMISSION_DENIED')
        self.assertEnvelopeError(self.call(self.inactive_student, 'get', self.path), 403,
                                 'ACCOUNT_INACTIVE')
        self.assertEnvelopeError(self.call(None, 'get', self.path), 401, 'AUTHENTICATION_REQUIRED')

    def test_effective_status_at_server_time(self):
        with mock.patch('quiz.lifecycle.server_now',
                        return_value=self.attempt.expires_at - timedelta(microseconds=1)):
            self.assertEqual(self.call(self.student, 'get', self.path).json()['data']['status'],
                             'in_progress')
        with mock.patch('quiz.lifecycle.server_now', return_value=self.attempt.expires_at):
            self.assertEqual(self.call(self.student, 'get', self.path).json()['data']['status'],
                             'expired')
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, 'in_progress')  # reads never write expiry

    def test_attempt_methods_are_read_only(self):
        for method in ('post', 'put', 'patch', 'delete'):
            with self.subTest(method=method):
                self.assertEnvelopeError(self.call(self.student, method, self.path, {}), 405,
                                         'METHOD_NOT_ALLOWED')


class StudentJourneyTests(QuizAPITestMixin, TestCase):
    """Integration: draft -> publish -> start -> answer -> grade -> history/admin results."""

    def as_admin(self, method, path, body=None):
        return self.call(self.admin, method, path, body)

    def test_full_journey(self):
        quiz = self.as_admin('post', '/quiz/quizzes/', {
            'title': 'Arabic week 1', 'description': 'Weekly assessment', 'time_limit': 30,
            'deadline': iso(timezone.now() + timedelta(days=7))}).json()['data']
        q1 = self.as_admin('post', f"/quiz/quizzes/{quiz['id']}/questions/",
                           question_body(points=2, order=1)).json()['data']
        q2 = self.as_admin('post', f"/quiz/quizzes/{quiz['id']}/questions/",
                           question_body(points=3, order=2, text='Second')).json()['data']
        self.assertEqual(self.as_admin('post', f"/quiz/quizzes/{quiz['id']}/publish/").status_code,
                         200)

        listed = self.call(self.student, 'get', '/quiz/quizzes/').json()['data']['results']
        self.assertEqual([(row['id'], row['total_points']) for row in listed], [(quiz['id'], 5)])
        attempt = self.start_api(self.student, quiz['id']).json()['data']
        read = self.call(self.student, 'get', f"/quiz/attempts/{attempt['id']}/").json()['data']
        self.assertEqual(read['id'], attempt['id'])
        option_ids = {q['id']: [o['id'] for o in q['options']] for q in read['questions']}
        correct = {q['id']: next(o['id'] for o in q['options'] if o['is_correct'])
                   for q in (q1, q2)}  # the admin's view of the keys
        answers = {str(q1['id']): correct[q1['id']],
                   str(q2['id']): next(o for o in option_ids[q2['id']] if o != correct[q2['id']])}
        submitted = self.submit_api(self.student, attempt['id'], answers)
        self.assertEqual(submitted.status_code, 201, submitted.content)
        result = submitted.json()['data']
        self.assertEqual((result['score'], result['total_points'], result['attempt_id']),
                         (2, 5, attempt['id']))

        after = self.call(self.student, 'get', f"/quiz/attempts/{attempt['id']}/").json()['data']
        self.assertEqual((after['status'], after['submission_id']), ('submitted', result['id']))
        history = self.call(self.student, 'get', f"/quiz/submissions/?attempt={attempt['id']}")
        self.assertEqual(history.json()['data']['results'], [result])
        detail = self.call(self.student, 'get', f"/quiz/submissions/{result['id']}/")
        self.assertEqual(detail.json()['data'], result)

        rows = self.as_admin('get', f"/quiz/quizzes/{quiz['id']}/submissions/").json()['data']
        self.assertEqual(rows['count'], 1)
        row = rows['results'][0]
        self.assertEqual(set(row), ADMIN_ROW_FIELDS)
        self.assertEqual({key: row[key] for key in RESULT_FIELDS}, result)
        self.assertEqual((row['student_id'], row['student_name'], row['student_email'],
                          row['is_legacy']),
                         (self.student.pk, 'Amina Yusuf', 'student@example.test', False))
        admin_detail = self.as_admin('get', f"/quiz/submissions/{result['id']}/").json()['data']
        self.assertEqual(set(admin_detail), ADMIN_DETAIL_FIELDS)
        self.assertEqual(admin_detail['answers'], answers)
        self.assertEqual(admin_detail['grading_snapshot']['questions'], [
            {'question_id': q1['id'], 'order': 1, 'points': 2,
             'selected_option_id': answers[str(q1['id'])], 'awarded_points': 2},
            {'question_id': q2['id'], 'order': 2, 'points': 3,
             'selected_option_id': answers[str(q2['id'])], 'awarded_points': 0}])
        self.assertEqual((admin_detail['started_at'], admin_detail['expires_at']),
                         (attempt['started_at'], attempt['expires_at']))


class HistoryTests(QuizAPITestMixin, TestCase):
    """GET /quiz/submissions/: own results, newest first, D0-paginated, filters."""

    def setUp(self):
        super().setUp()
        self.quizzes = [make_quiz(f'Quiz {n}', points=(1,))[0] for n in range(3)]
        self.mine = [self.submission(quiz, self.student) for quiz in self.quizzes]
        self.others = self.submission(self.quizzes[0], self.other_student)
        self.legacy_quiz = make_quiz('Legacy', points=(1,))[0]
        self.legacy_id = insert_legacy_submission(
            self.legacy_quiz.pk, self.student.pk, score=4,
            submitted_at=timezone.now() - timedelta(days=400))

    def history(self, query='', user=None):
        response = self.call(user or self.student, 'get', f'/quiz/submissions/{query}')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def test_own_results_newest_first_with_legacy_rows(self):
        data = self.history()
        self.assertEqual(set(data), {'count', 'next', 'previous', 'results'})
        self.assertEqual(data['count'], 4)
        ids = [row['id'] for row in data['results']]
        self.assertEqual(ids, [s.pk for s in reversed(self.mine)] + [self.legacy_id])
        self.assertNotIn(self.others.pk, ids)
        for row in data['results']:
            self.assertEqual(set(row), RESULT_FIELDS)
        legacy = data['results'][-1]
        self.assertEqual((legacy['attempt_id'], legacy['total_points'], legacy['score'],
                          legacy['quiz_title'], legacy['status'], legacy['graded_at']),
                         (None, None, 4, 'Legacy', 'submitted', None))

    def test_same_instant_ties_break_by_id(self):
        # Rows written in one transaction share submitted_at; -id keeps the order stable.
        same = self.mine[0].submitted_at
        extra = [insert_legacy_submission(make_quiz(f'Tie {n}', points=(1,))[0].pk,
                                          self.student.pk, submitted_at=same) for n in range(2)]
        ids = [row['id'] for row in self.history('?page_size=100')['results']]
        tied = [pk for pk in ids if pk in {self.mine[0].pk, *extra}]
        self.assertEqual(tied, sorted(tied, reverse=True))

    def test_filters(self):
        target = self.mine[1]
        data = self.history(f'?attempt={target.attempt_id}')
        self.assertEqual([row['id'] for row in data['results']], [target.pk])
        data = self.history(f'?quiz={self.quizzes[2].pk}')
        self.assertEqual([row['id'] for row in data['results']], [self.mine[2].pk])
        data = self.history(f'?quiz={self.quizzes[2].pk}&attempt={target.attempt_id}')
        self.assertEqual(data['count'], 0)
        # Another student's attempt/quiz filters reveal nothing of theirs.
        self.assertEqual(self.history(f'?attempt={self.others.attempt_id}')['count'], 0)
        self.assertEqual(self.history(f'?quiz={self.quizzes[0].pk}', self.other_student)['count'], 1)
        for query in ('?attempt=abc', '?attempt=0', '?attempt=-1', '?quiz=1.5', '?quiz=',
                      '?attempt=%EF%BC%91'):
            with self.subTest(query=query):
                response = self.call(self.student, 'get', f'/quiz/submissions/{query}')
                body = self.assertEnvelopeError(response, 400, 'VALIDATION_ERROR')
                self.assertEqual(len(body['data']['fields']), 1)

    def test_pagination(self):
        first = self.history('?page_size=3')
        self.assertEqual((first['count'], len(first['results'])), (4, 3))
        self.assertIsNotNone(first['next'])
        second = self.history('?page_size=3&page=2')
        self.assertEqual(len(second['results']), 1)
        self.assertEqual(len(self.history('?page_size=1000')['results']), 4)  # capped at 100
        self.assertEnvelopeError(self.call(self.student, 'get', '/quiz/submissions/?page=9'), 404,
                                 'NOT_FOUND')

    def test_history_survives_closing(self):
        lifecycle.close_quiz(self.quizzes[0].pk)
        self.assertEqual(self.history(f'?quiz={self.quizzes[0].pk}')['count'], 1)

    def test_result_detail_ownership(self):
        own = self.call(self.student, 'get', f'/quiz/submissions/{self.mine[0].pk}/')
        self.assertEqual(set(own.json()['data']), RESULT_FIELDS)
        legacy = self.call(self.student, 'get', f'/quiz/submissions/{self.legacy_id}')
        self.assertEqual(legacy.json()['data']['total_points'], None)
        for path in (f'/quiz/submissions/{self.others.pk}/', f'/quiz/submissions/{self.others.pk}',
                     '/quiz/submissions/999999/'):
            with self.subTest(path=path):
                response = self.call(self.student, 'get', path)
                self.assertEnvelopeError(response, 404, 'NOT_FOUND')
                self.assertNotIn('other@example.test', response.content.decode())

    def test_admin_has_no_own_history(self):
        self.assertEqual(self.history(user=self.admin)['count'], 0)


class AdminResultsTests(QuizAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Results', points=(2, 3))
        (q1, c1, _), (q2, c2, _) = self.questions
        self.students = [self.add_student(f'r{n}@example.test') for n in range(5)]
        self.rows = [self.submission(self.quiz, student, {str(q1.pk): c1.pk})
                     for student in self.students]
        self.legacy_id = insert_legacy_submission(
            self.quiz.pk, self.student.pk, score=1, submitted_at=timezone.now() - timedelta(days=9))
        self.other_quiz, _ = make_quiz('Other results', points=(1,))
        self.submission(self.other_quiz, self.other_student)
        self.path = f'/quiz/quizzes/{self.quiz.pk}/submissions/'

    def test_flat_paginated_rows_newest_first(self):
        response = self.call(self.admin, 'get', f'{self.path}?page_size=4')
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertEqual((data['count'], len(data['results'])), (6, 4))
        rest = self.call(self.admin, 'get', f'{self.path}?page_size=4&page=2').json()['data']
        rows = data['results'] + rest['results']
        self.assertEqual([row['id'] for row in rows],
                         [s.pk for s in reversed(self.rows)] + [self.legacy_id])
        for row, student in zip(rows, reversed(self.students)):
            self.assertEqual(set(row), ADMIN_ROW_FIELDS)
            self.assertEqual((row['student_id'], row['student_email'], row['student_name'],
                              row['quiz_id'], row['score'], row['total_points'], row['is_legacy']),
                             (student.pk, student.email, 'Amina Yusuf', self.quiz.pk, 2, 5, False))
            self.assertIsNotNone(row['attempt_id'])
        legacy = rows[-1]
        self.assertEqual((legacy['student_id'], legacy['attempt_id'], legacy['total_points'],
                          legacy['is_legacy']), (self.student.pk, None, None, True))

    def test_admin_result_detail_including_legacy(self):
        detail = self.call(self.admin, 'get', f'/quiz/submissions/{self.rows[0].pk}/').json()['data']
        self.assertEqual(set(detail), ADMIN_DETAIL_FIELDS)
        self.assertEqual(detail['grading_snapshot']['total_points'], 5)
        legacy = self.call(self.admin, 'get', f'/quiz/submissions/{self.legacy_id}/').json()['data']
        self.assertEqual((legacy['grading_snapshot'], legacy['started_at'], legacy['expires_at'],
                          legacy['is_legacy']), (None, None, None, True))

    def test_results_route_permissions(self):
        self.assertEnvelopeError(self.call(self.admin, 'get', '/quiz/quizzes/999999/submissions/'),
                                 404, 'NOT_FOUND')
        for user, status_code, code in ((self.student, 403, 'PERMISSION_DENIED'),
                                        (self.staff_student, 403, 'PERMISSION_DENIED'),
                                        (self.inactive_admin, 403, 'ACCOUNT_INACTIVE'),
                                        (None, 401, 'AUTHENTICATION_REQUIRED')):
            with self.subTest(user=getattr(user, 'email', None)):
                self.assertEnvelopeError(self.call(user, 'get', self.path), status_code, code)

    def test_draft_quiz_has_empty_results(self):
        draft, _ = make_quiz('Draft results', active=False)
        data = self.call(self.admin, 'get', f'/quiz/quizzes/{draft.pk}/submissions/').json()['data']
        self.assertEqual((data['count'], data['results']), (0, []))


class StudentKeyLeakTests(QuizAPITestMixin, TestCase):
    """No correctness key in any student payload, success or error (A12)."""

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Leak', points=(2, 3), legacy_text_question=True)

    def assertKeyFree(self, name, response):
        payload = response.json()
        leaked = KEY_FIELDS.intersection(walk_keys(payload))
        self.assertEqual(leaked, set(), f'{name} leaked {leaked}: {payload}')
        # Errors never echo a correct option ID either.
        if name.startswith('error'):
            correct_ids = {correct.pk for _, correct, _ in self.questions}
            self.assertFalse(correct_ids & set(walk_values(payload)), payload)

    def test_every_student_attempt_and_result_payload(self):
        (q1, c1, w1), (q2, _, _) = self.questions
        start = self.start_api(self.student, self.quiz.pk)
        attempt_id = start.json()['data']['id']
        answers = {str(q1.pk): w1[0].pk}
        responses = {
            'start': start,
            'resume': self.start_api(self.student, self.quiz.pk),
            'attempt': self.call(self.student, 'get', f'/quiz/attempts/{attempt_id}/'),
            'error invalid': self.submit_api(self.student, attempt_id, {str(q2.pk): c1.pk}),
            'submit': self.submit_api(self.student, attempt_id, answers),
            'replay': self.submit_api(self.student, attempt_id, answers),
            'error conflict': self.submit_api(self.student, attempt_id, {}),
            'attempt submitted': self.call(self.student, 'get', f'/quiz/attempts/{attempt_id}/'),
            'history': self.call(self.student, 'get', '/quiz/submissions/'),
            'history filtered': self.call(self.student, 'get',
                                          f'/quiz/submissions/?attempt={attempt_id}'),
        }
        submission_id = responses['submit'].json()['data']['id']
        responses['result'] = self.call(self.student, 'get', f'/quiz/submissions/{submission_id}/')
        responses['result alias'] = self.call(self.student, 'get',
                                              f'/quiz/submissions/{submission_id}')
        late, _ = lifecycle.start_attempt(self.quiz.pk, self.other_student)
        with mock.patch('quiz.lifecycle.server_now', return_value=late.expires_at):
            responses['error expired'] = self.submit_api(self.other_student, late.pk, answers)
            responses['attempt expired'] = self.call(self.other_student, 'get',
                                                     f'/quiz/attempts/{late.pk}/')
        for name, response in responses.items():
            with self.subTest(response=name):
                self.assertLess(response.status_code, 500, response.content)
                self.assertKeyFree(name, response)
        self.assertEqual(responses['error expired'].status_code, 409)
        self.assertEqual(responses['error conflict'].status_code, 409)
        self.assertEqual(responses['error invalid'].status_code, 400)


def walk_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_values(item)
    elif isinstance(value, int) and not isinstance(value, bool):
        yield value


class ThrottleTests(QuizAPITestMixin, TestCase):
    """D5: attempt start 20/min and submissions 60/min, per student."""

    def setUp(self):
        super().setUp()
        self.quiz, self.questions = make_quiz('Throttle', points=(1,))

    def test_attempt_start_is_limited_per_student(self):
        for _ in range(20):
            self.assertLess(self.start_api(self.student, self.quiz.pk).status_code, 300)
        response = self.start_api(self.student, self.quiz.pk)
        self.assertEnvelopeError(response, 429, 'THROTTLED')
        self.assertTrue(response.has_header('Retry-After'))
        # Per student, not per IP: a classmate behind the same address is unaffected.
        self.assertEqual(self.start_api(self.other_student, self.quiz.pk).status_code, 201)
        # Reads are not throttled.
        attempt = Attempt.objects.get(student=self.student)
        self.assertEqual(self.call(self.student, 'get', f'/quiz/attempts/{attempt.pk}/')
                         .status_code, 200)

    def test_submissions_are_limited_per_student_and_history_is_not(self):
        attempt, _ = lifecycle.start_attempt(self.quiz.pk, self.student)
        question, correct, _ = self.questions[0]
        answers = {str(question.pk): correct.pk}
        statuses = [self.submit_api(self.student, attempt.pk, answers).status_code
                    for _ in range(60)]
        self.assertEqual(statuses, [201] + [200] * 59)  # legitimate replays fit the budget
        self.assertEnvelopeError(self.submit_api(self.student, attempt.pk, answers), 429,
                                 'THROTTLED')
        self.assertEqual(self.call(self.student, 'get', '/quiz/submissions/').status_code, 200)
        other, _ = lifecycle.start_attempt(self.quiz.pk, self.other_student)
        self.assertEqual(self.submit_api(self.other_student, other.pk, answers).status_code, 201)

    def test_unauthorized_requests_do_not_spend_a_budget(self):
        for _ in range(25):
            self.start_api(self.admin, self.quiz.pk)
        self.assertEqual(self.start_api(self.student, self.quiz.pk).status_code, 201)

    @override_settings(REST_FRAMEWORK={
        'DEFAULT_AUTHENTICATION_CLASSES': ['accounts.authentication.ActiveUserJWTAuthentication'],
        'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],
        'EXCEPTION_HANDLER': 'pythagoras.utils.exception_handler',
        'DEFAULT_RENDERER_CLASSES': ['pythagoras.utils.StandardizedJSONRenderer'],
        'DEFAULT_THROTTLE_RATES': {'quiz_attempt_start': '2/min'},
    })
    def test_rate_is_tunable_in_settings(self):
        self.assertEqual(self.start_api(self.student, self.quiz.pk).status_code, 201)
        self.assertEqual(self.start_api(self.student, self.quiz.pk).status_code, 200)
        self.assertEnvelopeError(self.start_api(self.student, self.quiz.pk), 429, 'THROTTLED')


class AttemptSchemaContractTests(QuizAPITestMixin, TestCase):
    """The OpenAPI document describes the real enveloped wire payloads and examples."""

    PATHS = {
        'start': ('/quiz/quizzes/{quiz_id}/attempts/', 'post', '201'),
        'resume': ('/quiz/quizzes/{quiz_id}/attempts/', 'post', '200'),
        'start conflict': ('/quiz/quizzes/{quiz_id}/attempts/', 'post', '409'),
        'attempt': ('/quiz/attempts/{attempt_id}/', 'get', '200'),
        'attempt missing': ('/quiz/attempts/{attempt_id}/', 'get', '404'),
        'submit': ('/quiz/submissions/', 'post', '201'),
        'replay': ('/quiz/submissions/', 'post', '200'),
        'submit invalid': ('/quiz/submissions/', 'post', '400'),
        'submit conflict': ('/quiz/submissions/', 'post', '409'),
        'history': ('/quiz/submissions/', 'get', '200'),
        'result': ('/quiz/submissions/{id}/', 'get', '200'),
        'admin results': ('/quiz/quizzes/{quiz_id}/submissions/', 'get', '200'),
    }
    STUDENT_ONLY = {'start', 'resume', 'attempt', 'submit', 'replay', 'history'}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from drf_spectacular.generators import SchemaGenerator
        cls.schema = json_schema(SchemaGenerator().get_schema(public=True))

    def validator(self, name):
        from jsonschema import Draft4Validator, RefResolver
        path, method, status_code = self.PATHS[name]
        media = self.schema['paths'][path][method]['responses'][status_code]['content'][
            'application/json']
        schema = media['schema']
        if name == 'result':
            # The role-specific oneOf cannot discriminate (the admin payload is a
            # superset of the student one): accept either component here; the
            # field sets are pinned by the API tests.
            data = schema['properties']['data']
            if '$ref' in data:
                data = self.schema['components']['schemas'][data['$ref'].rsplit('/', 1)[1]]
            self.assertEqual({ref['$ref'] for ref in data['oneOf']},
                             {'#/components/schemas/AdminSubmissionDetail',
                              '#/components/schemas/StudentSubmission'})
            schema = {**schema, 'properties': {**schema['properties'],
                                               'data': {'anyOf': data['oneOf']}}}
        return Draft4Validator(schema, resolver=RefResolver.from_schema(self.schema)), media

    def assertWire(self, name, response):
        validator, _ = self.validator(name)
        validator.validate(response.json())

    def test_wire_payloads_match_schema(self):
        quiz, questions = make_quiz('Wire', points=(2,))
        (question, correct, _), = questions
        start = self.start_api(self.student, quiz.pk)
        self.assertWire('start', start)
        self.assertWire('resume', self.start_api(self.student, quiz.pk))
        attempt_id = start.json()['data']['id']
        self.assertWire('attempt', self.call(self.student, 'get', f'/quiz/attempts/{attempt_id}/'))
        self.assertWire('attempt missing', self.call(self.student, 'get', '/quiz/attempts/999999/'))
        self.assertWire('submit invalid', self.submit_api(self.student, attempt_id,
                                                          {str(question.pk): 'B'}))
        submitted = self.submit_api(self.student, attempt_id, {str(question.pk): correct.pk})
        self.assertWire('submit', submitted)
        self.assertWire('replay', self.submit_api(self.student, attempt_id,
                                                  {str(question.pk): correct.pk}))
        self.assertWire('submit conflict', self.submit_api(self.student, attempt_id, {}))
        self.assertWire('history', self.call(self.student, 'get', '/quiz/submissions/'))
        submission_id = submitted.json()['data']['id']
        self.assertWire('result', self.call(self.student, 'get', f'/quiz/submissions/{submission_id}/'))
        self.assertWire('result', self.call(self.admin, 'get', f'/quiz/submissions/{submission_id}/'))
        self.assertWire('admin results', self.call(self.admin, 'get',
                                                   f'/quiz/quizzes/{quiz.pk}/submissions/'))
        lifecycle.close_quiz(quiz.pk)
        self.assertWire('start conflict', self.start_api(self.other_student, quiz.pk))

    def test_examples_are_enveloped_valid_and_student_examples_key_free(self):
        checked = 0
        for name in self.PATHS:
            validator, media = self.validator(name)
            for example in media.get('examples', {}).values():
                with self.subTest(name=name, example=example.get('summary')):
                    validator.validate(example['value'])
                    self.assertEqual(set(example['value']), {'success', 'data', 'message'})
                    if name in self.STUDENT_ONLY or name.startswith('submit'):
                        self.assertFalse(KEY_FIELDS.intersection(walk_keys(example['value'])))
                    checked += 1
        self.assertGreaterEqual(checked, 14)
        request = self.schema['paths']['/quiz/submissions/']['post']['requestBody']
        examples = request['content']['application/json']['examples']
        self.assertEqual([e['value'] for e in examples.values()],
                         [{'attempt_id': 123, 'answers': {'10': 102}}])
        # Every student result/attempt component is key-free by construction.
        for component in ('StudentSubmission', 'StudentAttemptDetail', 'AttemptQuestion',
                          'AttemptOption'):
            self.assertFalse(KEY_FIELDS.intersection(walk_keys(
                self.schema['components']['schemas'][component])), component)


class DatabaseShapeTests(QuizAPITestMixin, TestCase):

    def test_api_results_use_stored_grade_not_recomputation(self):
        quiz, questions = make_quiz('Stored', points=(2,))
        (question, correct, _), = questions
        submission = self.submission(quiz, self.student, {str(question.pk): correct.pk})
        # Even if the grader changed, a stored result is reported as graded.
        with mock.patch('quiz.services.QuizService.grade', side_effect=AssertionError):
            data = self.call(self.student, 'get', f'/quiz/submissions/{submission.pk}/').json()
        self.assertEqual((data['data']['score'], data['data']['total_points']), (2, 2))
        self.assertEqual(Submission.objects.get().grading_snapshot['score'], 2)
        self.assertEqual(Quiz.objects.get().status, 'published')
