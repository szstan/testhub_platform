from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.api_testing.models import ScheduledTask
from apps.core.scheduler import ScheduleClaimLost, claim_due_task, load_claimed_task, renew_claim
from apps.users.models import User


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

        owner = User.objects.create_user(username='health-scheduler-owner', password='pass')
        now = timezone.now()
        task = ScheduledTask.objects.create(
            name='health-due-task',
            task_type='API_REQUEST',
            trigger_type='INTERVAL',
            interval_seconds=60,
            status='ACTIVE',
            next_run_time=now - timedelta(seconds=1),
            created_by=owner,
        )
        first = claim_due_task(ScheduledTask, task.pk, due_field='next_run_time', now=now)
        self.assertIsNotNone(first)
        renewed = renew_claim(ScheduledTask, task.pk, first.token, now=now + timedelta(seconds=1))
        self.assertGreater(renewed.expires_at, now + timedelta(seconds=300))
        self.assertIsNone(claim_due_task(ScheduledTask, task.pk, due_field='next_run_time', now=now))
        self.assertEqual(load_claimed_task(ScheduledTask, task.pk, first.token, require_active=True).pk, task.pk)
        second = claim_due_task(
            ScheduledTask,
            task.pk,
            due_field='next_run_time',
            now=now + timedelta(seconds=301),
        )
        self.assertIsNotNone(second)
        self.assertNotEqual(second.token, first.token)
        with self.assertRaises(ScheduleClaimLost):
            load_claimed_task(ScheduledTask, task.pk, first.token, require_active=True)

        with patch('apps.core.health._check_worker', return_value='error') as worker:
            with self.settings(READINESS_REQUIRE_WORKER=True):
                worker_response = self.client.get('/readiness/')
        self.assertEqual(worker_response.status_code, 503)
        self.assertEqual(worker_response.json()['checks']['worker'], 'error')
        worker.assert_called_once_with()

    @patch('apps.core.health.connection')
    def test_readiness_returns_503_when_database_is_unavailable(self, connection):
        connection.cursor.side_effect = RuntimeError('database unavailable')

        response = self.client.get('/readiness/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'unready')
        self.assertEqual(response.json()['checks']['database'], 'error')
        self.assertEqual(response.json()['checks']['cache'], 'ok')
