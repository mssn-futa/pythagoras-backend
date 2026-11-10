"""Shared fixtures for assessment API tests (PostgreSQL, no hard-coded PKs)."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from quiz import lifecycle
from quiz.models import Option, Question, Quiz

User = get_user_model()

# Any of these keys in a student payload is an answer-key leak (D0 A12).
KEY_FIELDS = {
    'is_correct', 'correct', 'correct_option', 'correct_options', 'correct_option_id',
    'correct_option_ids', 'correct_answer', 'correct_answers', 'answer_key', 'answer_keys',
}


def walk_keys(value):
    """Every dict key at any depth of a decoded JSON value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_keys(item)


def raw_update(table, pk, **columns):
    """UPDATE one row with raw SQL, bypassing every Python guard (DB-layer tests
    and states that no supported write path can produce)."""
    assignments = ', '.join(f'"{name}" = %s' for name in columns)
    with connection.cursor() as cursor:
        cursor.execute(f'UPDATE {table} SET {assignments} WHERE id = %s', [*columns.values(), pk])


def insert_legacy_submission(quiz_id, student_id, score=1, submitted_at=None, answers='{}'):
    """A pre-ASSESS-02 (legacy) result row: only the migration creates these, so
    insert it with the history trigger off inside the (rolled back) test
    transaction. Returns its ID."""
    with connection.cursor() as cursor:
        cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
        cursor.execute('ALTER TABLE quiz_submission DISABLE TRIGGER quiz_submission_history_guard')
        cursor.execute('INSERT INTO quiz_submission (quiz_id, student_id, answers, score, '
                       'is_legacy, submitted_at) VALUES (%s, %s, %s, %s, true, %s) RETURNING id',
                       [quiz_id, student_id, answers, score, submitted_at or timezone.now()])
        pk = cursor.fetchone()[0]
        cursor.execute('ALTER TABLE quiz_submission ENABLE TRIGGER quiz_submission_history_guard')
        cursor.execute('SET CONSTRAINTS ALL DEFERRED')
    return pk


def make_quiz(title, *, active=True, points=(2, 3), legacy_text_question=False, deadline=None,
              published_now=None):
    """Quiz with one MCQ question per ``points`` entry, each with four options
    of which the second is correct. Returns ``(quiz, [(question, correct, wrong_options)])``.

    ``active`` publishes it through ``quiz.lifecycle.publish_quiz`` (deadline a
    week ahead); otherwise it stays a draft. A published quiz cannot hold a
    legacy text question through any write path, so ``legacy_text_question``
    with ``active`` simulates that pre-ASSESS-02 state with raw SQL.
    ``deadline``/``published_now`` let tests publish with a deadline that has
    already passed (publication judged at an earlier instant), since a
    published deadline can never be edited.
    """
    quiz = Quiz.objects.create(title=title, type='arabic', description='Weekly assessment',
                               time_limit=30,
                               deadline=deadline or timezone.now() + timedelta(days=7))
    questions = []
    for index, value in enumerate(points, start=1):
        question = Question.objects.create(quiz=quiz, type=Question.Type.mcq,
                                           text=f'{title} question {index}', points=value)
        options = [Option.objects.create(question=question, text=label, is_correct=label == 'B')
                   for label in 'ABCD']
        questions.append((question, options[1], [options[0], options[2], options[3]]))
    if legacy_text_question:
        legacy = Question.objects.create(quiz=quiz, type=Question.Type.text,
                                         text=f'{title} legacy text question', points=1)
        Option.objects.create(question=legacy, text='legacy answer', is_correct=True)
    if active and legacy_text_question:
        raw_update('quiz_quiz', quiz.pk, status=Quiz.Status.PUBLISHED, is_active=True,
                   published_at=timezone.now())
    elif active:
        lifecycle.publish_quiz(quiz.pk, now=published_now)
    quiz.refresh_from_db()
    return quiz, questions


class QuizAPITestMixin:
    """Users for every role and token-authenticated clients through the real
    shared JWT authentication (no login throttle involved)."""

    def setUp(self):
        super().setUp()
        cache.clear()  # per-student throttle counters
        self.admin = self.make_user('admin@example.test', role='admin')
        self.student = self.make_user('student@example.test')
        self.other_student = self.make_user('other@example.test')
        # is_staff grants Django admin access only, never product authority.
        self.staff_student = self.make_user('staff@example.test', is_staff=True)
        self.inactive_student = self.make_user('inactive@example.test')
        self.inactive_admin = self.make_user('inactive-admin@example.test', role='admin')
        tokens = {user.pk: self.access(user) for user in (
            self.admin, self.student, self.other_student, self.staff_student,
            self.inactive_student, self.inactive_admin)}
        User.objects.filter(pk__in=[self.inactive_student.pk, self.inactive_admin.pk]).update(
            is_active=False)
        self._tokens = tokens

    @staticmethod
    def make_user(email, **fields):
        fields.setdefault('first_name', 'Amina')
        fields.setdefault('last_name', 'Yusuf')
        # Unusable password: bearer tokens are minted directly, so no hashing cost.
        return User.objects.create_user(email=email, password=None, **fields)

    @staticmethod
    def access(user):
        return str(RefreshToken.for_user(user).access_token)

    def client_for(self, user=None):
        client = APIClient()
        if user is not None:
            client.credentials(HTTP_AUTHORIZATION=f'Bearer {self._tokens[user.pk]}')
        return client

    def call(self, user, method, path, body=None):
        client = self.client_for(user)
        if body is None:
            return getattr(client, method)(path)
        return getattr(client, method)(path, body, format='json')

    def add_student(self, email):
        """A further active student with a bearer token."""
        user = self.make_user(email)
        self._tokens[user.pk] = self.access(user)
        return user

    def start_api(self, user, quiz_id):
        return self.call(user, 'post', f'/quiz/quizzes/{quiz_id}/attempts/', {})

    def submit_api(self, user, attempt_id, answers):
        return self.call(user, 'post', '/quiz/submissions/',
                         {'attempt_id': attempt_id, 'answers': answers})

    def submission(self, quiz, student, answers=None):
        """A finalized result through the lifecycle services (attempt + snapshot)."""
        attempt, _ = lifecycle.start_attempt(quiz.pk, student)
        submission, _ = lifecycle.finalize_attempt(attempt.pk, student, answers or {})
        return submission

    def assertNoAnswerKeys(self, response):
        payload = response.json()
        leaked = KEY_FIELDS.intersection(walk_keys(payload))
        self.assertEqual(leaked, set(), f'answer key fields leaked: {payload}')

    def assertEnvelopeError(self, response, status_code, code):
        self.assertEqual(response.status_code, status_code, response.content)
        body = response.json()
        self.assertIs(body['success'], False)
        self.assertEqual(body['data']['code'], code, body)
        return body
