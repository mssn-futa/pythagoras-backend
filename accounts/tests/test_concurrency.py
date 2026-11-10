import threading

from django.db import connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from .helpers import APITestMixin, make_user


class ConcurrentRefreshTests(APITestMixin, TransactionTestCase):
    """Real concurrent requests on PostgreSQL: one refresh token rotates once."""

    workers = 6

    def test_exactly_one_concurrent_refresh_succeeds(self):
        self.assertEqual(connection.vendor, 'postgresql')
        make_user()
        refresh = self.tokens()['refresh']
        barrier = threading.Barrier(self.workers)
        statuses, lock = [], threading.Lock()

        def use_token():
            from django.db import connections
            try:
                client = APIClient()
                barrier.wait(10)
                response = client.post('/accounts/refresh/', {'refresh': refresh}, format='json')
                with lock:
                    statuses.append((response.status_code, response.json()['data'].get('code')))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=use_token) for _ in range(self.workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(statuses), self.workers)
        self.assertEqual(sorted(statuses, key=str),
                         sorted([(200, None)] + [(401, 'TOKEN_INVALID')] * (self.workers - 1), key=str))
