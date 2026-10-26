from unittest.mock import patch

from django.test import TestCase


class HealthEndpointTests(TestCase):
    def test_health_is_a_dependency_free_liveness_probe(self):
        response = self.client.get('/health/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_readiness_checks_database_and_cache(self):
        response = self.client.get('/readiness/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'status': 'ok',
            'checks': {
                'database': 'ok',
                'cache': 'ok',
                'worker': 'skipped',
                'channel': 'skipped',
                'migrations': 'skipped',
                'frontend_assets': 'skipped',
            },
            'required': {
                'worker': False,
                'channel': False,
                'migrations': False,
                'frontend_assets': False,
            },
        })

    @patch('apps.core.health.connection')
    def test_readiness_returns_503_when_database_is_unavailable(self, connection):
        connection.cursor.side_effect = RuntimeError('database unavailable')

        response = self.client.get('/readiness/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'unready')
        self.assertEqual(response.json()['checks']['database'], 'error')
        self.assertEqual(response.json()['checks']['cache'], 'ok')

    @patch('apps.core.health._check_worker', return_value='error')
    def test_readiness_returns_503_when_required_worker_is_unavailable(self, worker):
        with self.settings(READINESS_REQUIRE_WORKER=True):
            response = self.client.get('/readiness/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'unready')
        self.assertEqual(response.json()['checks']['worker'], 'error')
        worker.assert_called_once_with()
