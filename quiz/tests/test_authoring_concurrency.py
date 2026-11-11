"""Publication racing question writes (PostgreSQL row locks, real transactions)."""
import threading
import time
from unittest import mock

from django.db import connection, connections, transaction
from django.test import TransactionTestCase, skipUnlessDBFeature

from quiz import authoring, lifecycle
from quiz.errors import PublicationInvalid, QuizImmutable
from quiz.models import Option, Question, Quiz, assert_content_editable

from .helpers import QuizAPITestMixin, make_quiz

WAIT = 0.5  # seconds a blocked thread is given to (wrongly) finish


def option_rows(correct=0):
    return [{'text': text, 'is_correct': index == correct, 'order': index + 1}
            for index, text in enumerate('ABCD')]


def question_data(order, text='Raced'):
    return {'type': 'mcq', 'text': text, 'points': 1, 'order': order, 'options': option_rows()}


def in_thread(target):
    """Run ``target`` in a thread with its own connection; returns (thread, result box)."""
    box = {}

    def run():
        try:
            box['result'] = target()
        except Exception as exc:  # inspected by the test
            box['error'] = exc
        finally:
            connections.close_all()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


@skipUnlessDBFeature('has_select_for_update')
class PublishVersusQuestionWriteTests(QuizAPITestMixin, TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.assertEqual(connection.vendor, 'postgresql')
        self.quiz, _ = make_quiz('Race', active=False, points=(1,))

    def test_publish_waits_for_an_open_question_write_and_validates_it(self):
        """A content write that holds the lock first is seen by the publication.

        The writer inserts a three-option question (allowed in a draft by the
        model layer); publish blocks on the quiz lock, then validates the
        committed graph and refuses it.
        """
        holding, release = threading.Event(), threading.Event()

        def writer():
            with transaction.atomic():
                assert_content_editable(self.quiz.pk)
                question = Question.objects.create(quiz=self.quiz, type='mcq', text='Three',
                                                   points=1)
                for index, text in enumerate('ABC'):
                    Option.objects.create(question=question, text=text, is_correct=index == 0)
                holding.set()
                release.wait(10)
            return question.pk

        write_thread, write_box = in_thread(writer)
        self.assertTrue(holding.wait(10))
        publish_thread, publish_box = in_thread(lambda: lifecycle.publish_quiz(self.quiz.pk))
        publish_thread.join(WAIT)
        self.assertTrue(publish_thread.is_alive(), 'publish did not wait for the content lock')
        release.set()
        write_thread.join(10)
        publish_thread.join(10)
        self.assertNotIn('error', write_box)
        self.assertIsInstance(publish_box.get('error'), PublicationInvalid, publish_box)
        self.assertIn("questions", publish_box['error'].fields)
        self.assertEqual(Quiz.objects.get(pk=self.quiz.pk).status, 'draft')

    def test_question_write_waits_for_publication_then_is_refused(self):
        holding, release = threading.Event(), threading.Event()

        def publisher():
            with transaction.atomic():
                result = lifecycle.publish_quiz(self.quiz.pk)
                holding.set()
                release.wait(10)
            return result

        publish_thread, publish_box = in_thread(publisher)
        self.assertTrue(holding.wait(10))
        write_thread, write_box = in_thread(
            lambda: authoring.create_question(self.quiz.pk, question_data(order=2)))
        write_thread.join(WAIT)
        self.assertTrue(write_thread.is_alive(), 'question write did not wait for publication')
        release.set()
        publish_thread.join(10)
        write_thread.join(10)
        self.assertNotIn('error', publish_box)
        self.assertIsInstance(write_box.get('error'), QuizImmutable, write_box)
        self.assertEqual(Quiz.objects.get(pk=self.quiz.pk).status, 'published')
        self.assertEqual(Question.objects.filter(quiz=self.quiz).count(), 1)

    def test_api_race_never_adds_content_after_publication(self):
        """Concurrent API question creates and one publish: every accepted question
        committed before publication, every refused one is a 409, none sneaks in."""
        writers = 6
        barrier = threading.Barrier(writers + 1)
        results = [None] * (writers + 1)
        original = authoring._check_question_order

        def slow_check(*args, **kwargs):
            time.sleep(0.05)  # widen the window inside the locked write
            return original(*args, **kwargs)

        def worker(index):
            client = self.client_for(self.admin)
            try:
                barrier.wait(timeout=10)
                if index == writers:
                    response = client.post(f'/quiz/quizzes/{self.quiz.pk}/publish/')
                else:
                    body = {'text': f'Q{index}', 'points': 1, 'order': index + 2,
                            'options': [{'text': t, 'is_correct': t == 'A'} for t in 'ABCD']}
                    response = client.post(f'/quiz/quizzes/{self.quiz.pk}/questions/', body,
                                           format='json')
                results[index] = (response.status_code, response.json())
            except Exception as exc:  # surfaced below
                results[index] = ('error', repr(exc))
            finally:
                connections.close_all()

        with mock.patch.object(authoring, '_check_question_order', side_effect=slow_check):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(writers + 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        publish_status, publish_body = results[writers]
        self.assertEqual(publish_status, 200, publish_body)
        accepted = {body['data']['id'] for status, body in results[:writers] if status == 201}
        refused = [body for status, body in results[:writers] if status != 201]
        self.assertTrue(all(body['data']['code'] == 'QUIZ_IMMUTABLE' for body in refused),
                        results)
        self.assertEqual(len(accepted) + len(refused), writers)
        stored = set(Question.objects.filter(quiz=self.quiz).values_list('pk', flat=True))
        original_ids = stored - accepted
        self.assertEqual(len(original_ids), 1)
        # The published graph is exactly what publication validated.
        self.assertEqual(publish_body['data']['question_count'], len(stored))
        self.assertEqual({q['id'] for q in publish_body['data']['questions']}, stored)
