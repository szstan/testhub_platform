from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.app_automation.models import AppProject, AppScheduledTask


User = get_user_model()


class AppScheduledTaskPauseTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='scheduled-task-owner', password='pass'
        )
        self.outsider = User.objects.create_user(
            username='scheduled-task-outsider', password='pass'
        )
        self.project = AppProject.objects.create(
            name='scheduled-task-project', owner=self.owner
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def create_task(self, **kwargs):
        defaults = {
            'project': self.project,
            'name': 'nightly-app-task',
            'task_type': 'TEST_CASE',
            'trigger_type': 'INTERVAL',
            'interval_seconds': 60,
            'created_by': self.owner,
            'next_run_time': timezone.now() + timedelta(hours=1),
        }
        defaults.update(kwargs)
        return AppScheduledTask.objects.create(**defaults)

    def pause_url(self, task):
        return f'/api/app-automation/scheduled-tasks/{task.id}/pause/'

    def resume_url(self, task):
        return f'/api/app-automation/scheduled-tasks/{task.id}/resume/'

    def test_pause_changes_active_task_and_preserves_next_run_time(self):
        task = self.create_task()
        next_run_time = task.next_run_time

        response = self.client.post(self.pause_url(task))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['success'])
        task.refresh_from_db()
        self.assertEqual(task.status, 'PAUSED')
        self.assertEqual(task.next_run_time, next_run_time)

    def test_pause_rejects_repeated_pause_and_terminal_states(self):
        paused_task = self.create_task(status='PAUSED')
        paused_next_run_time = paused_task.next_run_time

        response = self.client.post(self.pause_url(paused_task))

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data['success'])
        paused_task.refresh_from_db()
        self.assertEqual(paused_task.status, 'PAUSED')
        self.assertEqual(paused_task.next_run_time, paused_next_run_time)

        for terminal_status in ('COMPLETED', 'FAILED'):
            with self.subTest(status=terminal_status):
                terminal_task = self.create_task(status=terminal_status)
                response = self.client.post(self.pause_url(terminal_task))

                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.data['success'])
                terminal_task.refresh_from_db()
                self.assertEqual(terminal_task.status, terminal_status)

    def test_pause_is_project_scoped(self):
        task = self.create_task()
        self.client.force_authenticate(user=self.outsider)

        response = self.client.post(self.pause_url(task))

        self.assertEqual(response.status_code, 404)
        task.refresh_from_db()
        self.assertEqual(task.status, 'ACTIVE')

    def test_pause_requires_authentication(self):
        task = self.create_task()
        self.client.force_authenticate(user=None)

        response = self.client.post(self.pause_url(task))

        self.assertEqual(response.status_code, 401)
        task.refresh_from_db()
        self.assertEqual(task.status, 'ACTIVE')

    def test_resume_after_pause_restores_active_state_and_recalculates_schedule(self):
        task = self.create_task()
        self.client.post(self.pause_url(task))
        expected_next_run_time = timezone.now() + timedelta(minutes=10)

        with patch.object(
            AppScheduledTask,
            'calculate_next_run',
            return_value=expected_next_run_time,
        ):
            response = self.client.post(self.resume_url(task))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['success'])
        task.refresh_from_db()
        self.assertEqual(task.status, 'ACTIVE')
        self.assertEqual(task.next_run_time, expected_next_run_time)
