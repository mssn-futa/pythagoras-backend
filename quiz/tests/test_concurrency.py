"""Concurrent submissions/attempts by one student (PostgreSQL row locking)."""
import threading
from datetime import timedelta
from unittest import mock

from django.db import connection, connections, transaction
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from quiz import lifecycle
from quiz.errors import AttemptExpired, SubmissionConflict
from quiz.models import Attempt, Submission
from quiz.services import QuizService

from .helpers import QuizAPITestMixin, make_quiz

THREADS = 8


@skipUnlessDBFeature('has_select_for_update')
class ConcurrentSubmissionTests(QuizAPITestMixin, TransactionTestCase):
    """Threaded API requests by one student (real HTTP stack, one connection each)."""

    def setUp(self):
        super().setUp()
        self.assertEqual(connection.vendor, 'postgresql')
        self.quiz, questions = make_quiz('Concurrent', points=(2, 3))
        (q1, c1, w1), (q2, c2, _) = questions
        self.answer_maps = [
            {str(q1.pk): c1.pk, str(q2.pk): c2.pk},
            {str(q1.pk): w1[0].pk},
        ]

    def run_clients(self, request):
        barrier = threading.Barrier(THREADS)
        results = [None] * THREADS

        def worker(index):
            try:
                client = self.client_for(self.student)
                barrier.wait(timeout=10)
                response = request(client, index)
                results[index] = (response.status_code, response.json(), index % 2)
            except Exception as exc:  # surfaced by the assertions below
                results[index] = ('error', repr(exc), index % 2)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertTrue(all(result is not None for result in results), results)
        return results

    def test_same_student_concurrent_submits_create_exactly_one_result(self):
        attempt, _ = lifecycle.start_attempt(self.quiz.pk, self.student)
        original_grade = QuizService.grade

        def slow_grade(quiz, answers):
            # Widen the window between the existence check and the insert so
            # an unlocked implementation reliably creates duplicates.
            threading.Event().wait(0.3)
            return original_grade(quiz, answers)

        with mock.patch.object(QuizService, 'grade', side_effect=slow_grade):
            results = self.run_clients(lambda client, index: client.post(
                '/quiz/submissions/', {'attempt_id': attempt.pk,
                                       'answers': self.answer_maps[index % 2]}, format='json'))

        statuses = sorted(result[0] for result in results)
        self.assertEqual(Submission.objects.filter(quiz=self.quiz, student=self.student).count(), 1,
                         statuses)
        created = [result for result in results if result[0] == 201]
        self.assertEqual(len(created), 1, statuses)
        winner = Submission.objects.get()
        winning_map = created[0][2]
        self.assertEqual(winner.answers, self.answer_maps[winning_map])
        for status_code, body, answer_map in results:
            if status_code == 201:
                continue
            if answer_map == winning_map:
                self.assertEqual(status_code, 200, body)
                self.assertEqual(body['data'], created[0][1]['data'])
            else:
                self.assertEqual(status_code, 409, body)
                self.assertEqual(body['data']['code'], 'SUBMISSION_CONFLICT')
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.finalized_at), ('submitted', winner.submitted_at))

    def test_same_student_concurrent_starts_create_one_attempt(self):
        results = self.run_clients(lambda client, index: client.post(
            f'/quiz/quizzes/{self.quiz.pk}/attempts/', {}, format='json'))
        statuses = sorted(result[0] for result in results)
        self.assertEqual(statuses, [200] * (THREADS - 1) + [201], results)
        attempt = Attempt.objects.get()
        self.assertEqual({result[1]['data']['id'] for result in results}, {attempt.pk})
        self.assertEqual({result[1]['data']['expires_at'] for result in results},
                         {result[1]['data']['expires_at'] for result in results[:1]})

    def test_grading_failure_under_concurrency_leaves_no_partial_result(self):
        attempt, _ = lifecycle.start_attempt(self.quiz.pk, self.student)
        with mock.patch.object(QuizService, 'grade', side_effect=RuntimeError('boom')):
            results = self.run_clients(lambda client, index: client.post(
                '/quiz/submissions/', {'attempt_id': attempt.pk,
                                       'answers': self.answer_maps[0]}, format='json'))
        self.assertEqual({result[0] for result in results}, {500})
        self.assertFalse(Submission.objects.exists())
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'in_progress')


@skipUnlessDBFeature('has_select_for_update')
class LateArrivalLockOrderTests(QuizAPITestMixin, TransactionTestCase):
    """The lifecycle doc's open edge: an on-time arrival queued behind a late one.

    The late request (arrival at/after expiry) obtains the attempt lock first.
    It must not persist ``expired``: the earlier, on-time arrival that then gets
    the lock is accepted at its arrival instant.
    """

    def test_late_request_holding_the_lock_does_not_reject_an_earlier_arrival(self):
        self.assertEqual(connection.vendor, 'postgresql')
        quiz, questions = make_quiz('Lock order', points=(1,))
        (question, correct, _), = questions
        attempt, _ = lifecycle.start_attempt(quiz.pk, self.student)
        answers = {str(question.pk): correct.pk}
        on_time = attempt.expires_at - timedelta(seconds=1)
        late = attempt.expires_at + timedelta(seconds=1)
        results = {}
        holding = threading.Event()
        release = threading.Event()

        def hold_lock():
            try:
                with transaction.atomic():
                    Attempt.objects.select_for_update().get(pk=attempt.pk)
                    holding.set()
                    release.wait(timeout=20)
            finally:
                connections.close_all()

        def finalize(name, now):
            try:
                results[name] = lifecycle.finalize_attempt(attempt.pk, self.student, answers,
                                                           now=now)
            except Exception as exc:  # asserted below
                results[name] = exc
            finally:
                connections.close_all()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.assertTrue(holding.wait(timeout=10))
        late_thread = threading.Thread(target=finalize, args=('late', late))
        late_thread.start()
        threading.Event().wait(0.5)  # the late request queues for the row lock first
        on_time_thread = threading.Thread(target=finalize, args=('on_time', on_time))
        on_time_thread.start()
        threading.Event().wait(0.5)
        release.set()
        for thread in (holder, late_thread, on_time_thread):
            thread.join(timeout=30)

        self.assertIsInstance(results['late'], AttemptExpired)
        self.assertIsInstance(results['on_time'], tuple, results)
        submission, created = results['on_time']
        self.assertTrue(created)
        self.assertEqual(submission.submitted_at, on_time)
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.finalized_at), ('submitted', on_time))


@skipUnlessDBFeature('has_select_for_update')
class ConcurrentAttemptTests(QuizAPITestMixin, TransactionTestCase):
    """Lifecycle primitives under contention: one attempt, one finalization."""

    def setUp(self):
        super().setUp()
        self.assertEqual(connection.vendor, 'postgresql')
        self.quiz, questions = make_quiz('Concurrent attempts', points=(2,))
        (q1, c1, w1), = questions
        self.answer_maps = [{str(q1.pk): c1.pk}, {str(q1.pk): w1[0].pk}]

    def run_threads(self, target):
        barrier = threading.Barrier(THREADS)
        results = [None] * THREADS

        def worker(index):
            try:
                barrier.wait(timeout=10)
                results[index] = target(index)
            except Exception as exc:  # surfaced by the assertions below
                results[index] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return results

    def test_concurrent_starts_create_one_attempt(self):
        results = self.run_threads(lambda i: lifecycle.start_attempt(self.quiz.pk, self.student))
        self.assertTrue(all(isinstance(result, tuple) for result in results), results)
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual({attempt.pk for attempt, _ in results},
                         set(Attempt.objects.values_list('pk', flat=True)))
        self.assertEqual(Attempt.objects.count(), 1)

    def test_concurrent_finalizations_create_one_submission(self):
        attempt, _ = lifecycle.start_attempt(self.quiz.pk, self.student)
        results = self.run_threads(lambda i: lifecycle.finalize_attempt(
            attempt.pk, self.student, self.answer_maps[i % 2]))
        created = [r for r in results if isinstance(r, tuple) and r[1]]
        self.assertEqual(len(created), 1, results)
        winner = created[0][0]
        winning = self.answer_maps.index(winner.answers)
        for index, result in enumerate(results):
            if isinstance(result, tuple):
                self.assertEqual(result[0].pk, winner.pk)
                self.assertEqual(index % 2, winning)
            else:
                self.assertIsInstance(result, SubmissionConflict)
                self.assertNotEqual(index % 2, winning)
        self.assertEqual(Submission.objects.count(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')


@skipUnlessDBFeature('has_select_for_update')
class SharedDeadlineTests(QuizAPITestMixin, TransactionTestCase):
    """Reviewer scenario (M1): many students submit on time near one shared deadline.

    Each request arrives (its transaction starts) well before the deadline, but
    grading takes long enough that serializing them on an exclusive quiz lock
    would judge the later ones after the deadline. With the shared quiz lock
    and arrival-time judgement, every on-time submission is accepted.
    """

    STUDENTS = 8
    GRADING_DELAY = 0.5  # seconds; STUDENTS * delay well exceeds the time left
    WINDOW = timedelta(seconds=3)

    def test_on_time_submissions_near_a_shared_deadline_are_accepted(self):
        self.assertEqual(connection.vendor, 'postgresql')
        students = [self.make_user(f'deadline{i}@example.test') for i in range(self.STUDENTS)]
        deadline = timezone.now() + self.WINDOW
        quiz, questions = make_quiz('Shared deadline', points=(1,), deadline=deadline)
        (question, correct, _), = questions
        attempts = [lifecycle.start_attempt(quiz.pk, student)[0] for student in students]
        self.assertTrue(all(attempt.expires_at == deadline for attempt in attempts))

        original_grade = QuizService.grade

        def slow_grade(quiz, answers):
            threading.Event().wait(self.GRADING_DELAY)
            return original_grade(quiz, answers)

        barrier = threading.Barrier(self.STUDENTS)
        results = [None] * self.STUDENTS

        def worker(index):
            try:
                barrier.wait(timeout=10)
                results[index] = lifecycle.finalize_attempt(
                    attempts[index].pk, students[index], {str(question.pk): correct.pk})
            except Exception as exc:  # surfaced by the assertions below
                results[index] = exc
            finally:
                connections.close_all()

        remaining = (deadline - timezone.now()).total_seconds()
        self.assertGreater(remaining, 1.5, 'fixture setup too slow for this scenario')
        self.assertGreater(self.STUDENTS * self.GRADING_DELAY, remaining)
        with mock.patch.object(QuizService, 'grade', side_effect=slow_grade):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(self.STUDENTS)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        self.assertTrue(all(isinstance(result, tuple) and result[1] for result in results),
                        results)
        self.assertEqual(Submission.objects.filter(quiz=quiz).count(), self.STUDENTS)
        self.assertEqual(Attempt.objects.filter(quiz=quiz, status='submitted').count(),
                         self.STUDENTS)
        # Arrival-time judgement: each submission is stamped before the deadline.
        self.assertTrue(all(submission.submitted_at < deadline
                            for submission in Submission.objects.filter(quiz=quiz)))
