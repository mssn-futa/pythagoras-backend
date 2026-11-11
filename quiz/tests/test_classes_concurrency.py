"""Concurrent enrollment, module ordering and class closing (PostgreSQL row locks).

Each worker is a real API request on its own database connection, released
together by a barrier.
"""
import threading
from unittest import mock

from django.db import connection, connections
from django.test import TransactionTestCase, skipUnlessDBFeature

from quiz import learning
from quiz.models import Course, Enrollment, LearningModule

from .learning_helpers import LearningAPITestMixin, make_class, make_module

THREADS = 8


@skipUnlessDBFeature('has_select_for_update')
class LearningConcurrencyTests(LearningAPITestMixin, TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.assertEqual(connection.vendor, 'postgresql')
        self.course = make_class(Course.Status.PUBLISHED)
        self.base = f'/quiz/classes/{self.course.pk}/'

    def run_clients(self, request, users):
        barrier = threading.Barrier(len(users))
        results = [None] * len(users)

        def worker(index):
            try:
                client = self.client_for(users[index])
                barrier.wait(timeout=10)
                response = request(client, index)
                results[index] = (response.status_code, response.json())
            except Exception as exc:  # surfaced by the assertions below
                results[index] = ('error', repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(users))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertTrue(all(result is not None for result in results), results)
        return results

    def slow(self, name):
        """Widen the window between the existence check and the write."""
        original = getattr(learning, name)

        def wrapper(*args, **kwargs):
            threading.Event().wait(0.2)
            return original(*args, **kwargs)
        return mock.patch.object(learning, name, side_effect=wrapper)

    def test_concurrent_self_enrollment_creates_one_record(self):
        results = self.run_clients(
            lambda client, index: client.post(self.base + 'enrollment/', {}, format='json'),
            [self.student] * THREADS)
        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, [200] * (THREADS - 1) + [201], results)
        self.assertEqual(len({body['data']['id'] for _, body in results}), 1)
        self.assertEqual(Enrollment.objects.filter(course=self.course).count(), 1)

    def test_concurrent_admin_roster_posts_reuse_one_record(self):
        Enrollment.objects.create(course=self.course, student=self.student,
                                  status=Enrollment.Status.WITHDRAWN,
                                  withdrawn_at=self.course.published_at)
        results = self.run_clients(
            lambda client, index: client.post(self.base + 'enrollments/',
                                              {'student_id': self.student.pk}, format='json'),
            [self.admin] * THREADS)
        self.assertEqual({status for status, _ in results}, {200}, results)
        record = Enrollment.objects.get(course=self.course, student=self.student)
        self.assertEqual(record.status, Enrollment.Status.ACTIVE)
        self.assertEqual({body['data']['id'] for _, body in results}, {record.pk})

    def test_concurrent_enroll_and_withdraw_end_consistent(self):
        users = [self.student] * THREADS

        def request(client, index):
            if index % 2:
                return client.delete(self.base + 'enrollment/')
            return client.post(self.base + 'enrollment/', {}, format='json')

        results = self.run_clients(request, users)
        self.assertTrue(all(status in (200, 201, 404) for status, _ in results), results)
        records = Enrollment.objects.filter(course=self.course, student=self.student)
        self.assertEqual(records.count(), 1)
        record = records.get()
        self.assertEqual(record.withdrawn_at is None, record.status == Enrollment.Status.ACTIVE)

    def test_close_racing_enrollments_never_admits_after_close(self):
        students = [self.add_student(f'race{i}@example.test') for i in range(THREADS - 1)]

        def request(client, index):
            if index == THREADS - 1:
                return client.post(self.base + 'close/', format='json')
            return client.post(self.base + 'enrollment/', {}, format='json')

        results = self.run_clients(request, [*students, self.admin])
        self.assertEqual(results[-1][0], 200, results[-1])
        enrolled = [status for status, _ in results[:-1]]
        self.assertTrue(set(enrolled) <= {201, 409}, results)
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.CLOSED)
        # Exactly the admitted requests left a record; refused ones left none.
        self.assertEqual(Enrollment.objects.filter(course=self.course).count(),
                         enrolled.count(201))

    def test_concurrent_reorders_apply_one_whole_permutation(self):
        modules = [make_module(self.course, n) for n in range(1, 6)]
        ids = [module.pk for module in modules]
        permutations = [ids[index:] + ids[:index] for index in range(THREADS)]
        with self.slow('server_now'):
            results = self.run_clients(
                lambda client, index: client.post(self.base + 'modules/reorder/',
                                                  {'module_ids': permutations[index]},
                                                  format='json'),
                [self.admin] * THREADS)
        self.assertEqual({status for status, _ in results}, {200}, results)
        final = list(LearningModule.objects.filter(course=self.course)
                     .order_by('order').values_list('pk', flat=True))
        self.assertIn(final, permutations)
        orders = list(LearningModule.objects.filter(course=self.course)
                      .order_by('order').values_list('order', flat=True))
        self.assertEqual(orders, [1, 2, 3, 4, 5])
        # Each response is a consistent permutation, never a mixture.
        for _, body in results:
            self.assertIn([item['id'] for item in body['data']], permutations)

    def test_concurrent_creates_with_one_order_admit_one(self):
        with self.slow('_check_order'):
            results = self.run_clients(
                lambda client, index: client.post(self.base + 'modules/',
                                                  {'title': f'M{index}', 'order': 1},
                                                  format='json'),
                [self.admin] * THREADS)
        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, [201] + [400] * (THREADS - 1), results)
        self.assertEqual(LearningModule.objects.filter(course=self.course).count(), 1)

    def test_concurrent_appends_get_distinct_orders(self):
        with self.slow('_next_order'):
            results = self.run_clients(
                lambda client, index: client.post(self.base + 'modules/', {'title': f'M{index}'},
                                                  format='json'),
                [self.admin] * THREADS)
        self.assertEqual({status for status, _ in results}, {201}, results)
        self.assertEqual(sorted(LearningModule.objects.values_list('order', flat=True)),
                         list(range(1, THREADS + 1)))
