"""Upgrade rehearsal of representative legacy quiz data through quiz.0004-0006."""
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import ProtectedError
from django.test import TransactionTestCase
from django.utils import timezone

from dawah.models import Event
from quiz import lifecycle
from quiz.errors import (
    PublicationInvalid, QuizImmutable, SubmissionConflict, db_error_sqlstate,
)
from quiz.models import Attempt, Option, Question, Quiz, Submission
from quiz.tests.helpers import QuizAPITestMixin, make_quiz

BEFORE = [('quiz', '0003_alter_quiz_related_course_alter_quiz_related_event_and_more')]
AFTER = [('quiz', '0007_assessment_history_triggers')]
SUBMITTED = datetime(2026, 3, 1, 9, 30, tzinfo=dt_timezone.utc)


class AssessmentMigrationTests(TransactionTestCase):

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def applied(self):
        executor = MigrationExecutor(connection)
        return {name for app, name in executor.recorder.applied_migrations() if app == 'quiz'}

    def tearDown(self):
        try:
            self.migrate(AFTER)
        finally:
            super().tearDown()

    def legacy_fixture(self):
        apps = self.migrate(BEFORE)
        # Accounts are at their latest schema (only quiz is rolled back), so
        # users come from the current model and are linked to old rows by ID.
        User = get_user_model()
        Event = apps.get_model('dawah', 'Event')
        OldQuiz = apps.get_model('quiz', 'Quiz')
        OldQuestion = apps.get_model('quiz', 'Question')
        OldOption = apps.get_model('quiz', 'Option')
        OldSubmission = apps.get_model('quiz', 'Submission')
        users = [User.objects.create_user(email=f'legacy{i}@example.test', password=None)
                 for i in range(3)]
        event = Event.objects.create(title='E', category='c', tags='t', speakers='s',
                                     location='l', start_time=SUBMITTED, end_time=SUBMITTED)
        active = OldQuiz.objects.create(title='Active', type='TEXT', description='d',
                                        time_limit=30, is_active=True, related_event=event)
        inactive = OldQuiz.objects.create(title='Inactive', type='arabic', description='d',
                                          time_limit=None, is_active=False)
        # Interleaved IDs: per-quiz numbering must follow ID order within each quiz.
        a1 = OldQuestion.objects.create(quiz=active, type='mcq', text='a1', points=2)
        b1 = OldQuestion.objects.create(quiz=inactive, type='mcq', text='b1', points=1)
        a2 = OldQuestion.objects.create(quiz=active, type='text', text='a2', points=1)
        options = [OldOption.objects.create(question=a1, text=t, is_correct=t == 'B')
                   for t in 'ABCD']
        OldOption.objects.create(question=a2, text='free text key', is_correct=True)
        # A fully valid legacy MCQ quiz with a legacy result: publishable after
        # the upgrade, but content-locked by its history (lead decision M4).
        graded = OldQuiz.objects.create(title='Graded', type='arabic', description='d',
                                        time_limit=20, is_active=True)
        g1 = OldQuestion.objects.create(quiz=graded, type='mcq', text='g1', points=1)
        for t in 'ABCD':
            OldOption.objects.create(question=g1, text=t, is_correct=t == 'A')
        first = OldSubmission.objects.create(quiz=active, student_id=users[0].pk, score=30000,
                                             answers={str(a1.pk): 'B'})
        second = OldSubmission.objects.create(quiz=active, student_id=users[1].pk, score=0, answers={})
        duplicate = OldSubmission.objects.create(quiz=active, student_id=users[0].pk, score=1,
                                                 answers={'x': 1})
        graded_result = OldSubmission.objects.create(quiz=graded, student_id=users[0].pk, score=1,
                                                     answers={str(g1.pk): 'A'})
        # Legacy times are whatever was stored; pin them to compare after the upgrade.
        OldSubmission.objects.update(submitted_at=SUBMITTED)
        return {'apps': apps, 'users': users, 'event': event, 'active': active,
                'inactive': inactive, 'questions': (a1, b1, a2), 'options': options,
                'submissions': (first, second, duplicate), 'graded': graded, 'g1': g1,
                'graded_result': graded_result}

    def test_upgrade_blocks_on_duplicates_then_preserves_and_reverses(self):
        data = self.legacy_fixture()
        first, second, duplicate = data['submissions']
        active, inactive = data['active'], data['inactive']
        OldSubmission = data['apps'].get_model('quiz', 'Submission')
        expected_line = (f'duplicate submissions: quiz {active.pk} student {data["users"][0].pk}: '
                         f'submission IDs {first.pk}, {duplicate.pk}')

        # Preflight report: IDs only, nonzero exit, nothing changed.
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command('assessment_preflight', stdout=out)
        self.assertIn(expected_line, out.getvalue())
        self.assertIn(f'quiz IDs {active.pk}', out.getvalue())
        self.assertNotIn('legacy0@', out.getvalue())

        # The migration refuses before changing anything.
        with self.assertRaisesMessage(Exception, expected_line):
            self.migrate(AFTER)
        self.assertNotIn('0004_assessment_lifecycle_schema', self.applied())
        self.assertEqual(OldSubmission.objects.count(), 4)

        # Lead-approved manual resolution (fixture stand-in: the duplicate is
        # reattributed, not deleted), then the upgrade proceeds.
        OldSubmission.objects.filter(pk=duplicate.pk).update(student_id=data['users'][2].pk)
        out = StringIO()
        call_command('assessment_preflight', stdout=out)
        self.assertIn('No duplicate submissions found.', out.getvalue())
        self.migrate(AFTER)

        quizzes = {q.pk: q for q in Quiz.objects.all()}
        self.assertEqual(
            {pk: (q.status, q.is_active, q.legacy_is_active, q.created_at, q.published_at)
             for pk, q in quizzes.items()},
            {active.pk: ('draft', False, True, None, None),
             inactive.pk: ('draft', False, False, None, None),
             data['graded'].pk: ('draft', False, True, None, None)})
        self.assertEqual(quizzes[active.pk].type, 'TEXT')  # legacy metadata retained
        a1, b1, a2 = data['questions']
        self.assertEqual(dict(Question.objects.values_list('pk', 'order')),
                         {a1.pk: 1, a2.pk: 2, b1.pk: 1, data['g1'].pk: 1})
        self.assertEqual(Question.objects.get(pk=a2.pk).type, 'text')  # preserved, unpublishable
        self.assertEqual([o.order for o in Option.objects.filter(question_id=a1.pk).order_by('pk')],
                         [1, 2, 3, 4])
        rows = {s.pk: s for s in Submission.objects.exclude(quiz_id=data['graded'].pk)}
        self.assertEqual(
            {pk: (s.student_id, s.score, s.answers, s.is_legacy, s.attempt_id, s.total_points,
                  s.grading_snapshot, s.submitted_at) for pk, s in rows.items()},
            {first.pk: (data['users'][0].pk, 30000, {str(a1.pk): 'B'}, True, None, None, None,
                        SUBMITTED),
             second.pk: (data['users'][1].pk, 0, {}, True, None, None, None, SUBMITTED),
             duplicate.pk: (data['users'][2].pk, 1, {'x': 1}, True, None, None, None, SUBMITTED)})
        self.assertFalse(Attempt.objects.exists())  # nothing fabricated

        # Upgraded legacy quizzes cannot publish their unsupported question and
        # the referenced event is now protected.
        Quiz.objects.filter(pk=active.pk).update(deadline=timezone.now() + timedelta(days=1))
        with self.assertRaises(PublicationInvalid):
            lifecycle.publish_quiz(active.pk)
        with self.assertRaises(ProtectedError):
            Event.objects.get(pk=data['event'].pk).delete()

        # Reverse to the pre-ASSESS-02 schema restores is_active and keeps grades.
        apps = self.migrate(BEFORE)
        OldQuiz = apps.get_model('quiz', 'Quiz')
        OldSubmission = apps.get_model('quiz', 'Submission')
        self.assertEqual(dict(OldQuiz.objects.values_list('pk', 'is_active')),
                         {active.pk: True, inactive.pk: False, data['graded'].pk: True})
        self.assertEqual(dict(OldSubmission.objects.values_list('pk', 'score')),
                         {first.pk: 30000, second.pk: 0, duplicate.pk: 1,
                          data['graded_result'].pk: 1})

    def test_legacy_history_locks_content_and_uses_the_attempt(self):
        data = self.legacy_fixture()
        OldSubmission = data['apps'].get_model('quiz', 'Submission')
        OldSubmission.objects.filter(pk=data['submissions'][2].pk).update(
            student_id=data['users'][2].pk)
        self.migrate(AFTER)
        graded, inactive = data['graded'], data['inactive']
        question = Question.objects.get(pk=data['g1'].pk)
        option = question.option_set.order_by('order').first()
        self.assertEqual(Quiz.objects.get(pk=graded.pk).status, 'draft')
        # M4: a draft with (legacy) history is content-locked on every path.
        operations = {
            'question edit': lambda: (setattr(question, 'text', 'edited'), question.save()),
            'question add': lambda: Question.objects.create(quiz_id=graded.pk, type='mcq',
                                                            text='new'),
            'question delete': lambda: question.delete(),
            'question reorder': lambda: Question.objects.filter(pk=question.pk).update(order=2),
            'option edit': lambda: (setattr(option, 'is_correct', False), option.save()),
            'option delete': lambda: Option.objects.filter(pk=option.pk).delete(),
        }
        for name, operation in operations.items():
            with self.subTest(name), self.assertRaises(QuizImmutable):
                operation()
        for sql in ('UPDATE quiz_question SET text = %s WHERE id = %s',
                    'UPDATE quiz_option SET text = %s WHERE id = %s'):
            target = question.pk if 'question' in sql else option.pk
            with self.subTest(sql=sql), self.assertRaises(IntegrityError) as caught:
                with connection.cursor() as cursor:
                    cursor.execute(sql, ['edited', target])
            self.assertEqual(db_error_sqlstate(caught.exception), '23Q01')
        self.assertEqual(Question.objects.get(pk=question.pk).text, 'g1')
        # A draft without history stays editable.
        Question.objects.create(quiz_id=inactive.pk, type='mcq', text='new draft question')

        # Publishing is still possible; the legacy result is that student's attempt.
        Quiz.objects.filter(pk=graded.pk).update(deadline=timezone.now() + timedelta(days=1))
        lifecycle.publish_quiz(graded.pk)
        User = get_user_model()
        legacy_student = User.objects.get(pk=data['users'][0].pk)
        with self.assertRaises(SubmissionConflict):
            lifecycle.start_attempt(graded.pk, legacy_student)
        attempt, created = lifecycle.start_attempt(graded.pk, User.objects.get(pk=data['users'][1].pk))
        self.assertTrue(created)
        self.assertEqual(Attempt.objects.filter(pk=attempt.pk).count(), 1)

    def test_rollback_refused_once_release_history_exists(self):
        self.migrate(AFTER)
        student = QuizAPITestMixin.make_user('rollback@example.test')
        quiz, _ = make_quiz('Rollback')
        lifecycle.start_attempt(quiz.pk, student)
        with self.assertRaisesMessage(Exception, 'Refusing to unapply quiz.0007: 1 attempts'):
            self.migrate(BEFORE)
        self.assertIn('0007_assessment_history_triggers', self.applied())

        self.assertEqual(Attempt.objects.count(), 1)
