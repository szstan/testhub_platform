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
            'checks': {'database': 'ok', 'cache': 'ok'},
        })

    @patch('apps.core.health.connection')
    def test_readiness_returns_503_when_database_is_unavailable(self, connection):
        connection.cursor.side_effect = RuntimeError('database unavailable')

        response = self.client.get('/readiness/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'unready')
        self.assertEqual(response.json()['checks']['database'], 'error')
        self.assertEqual(response.json()['checks']['cache'], 'ok')
