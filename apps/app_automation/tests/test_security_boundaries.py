import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from django.contrib.auth import get_user_model
from django.core.signing import TimestampSigner
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.app_automation.consumers import AppExecutionConsumer
from apps.app_automation.models import AppProject, AppTestCase, AppTestExecution
from apps.app_automation.views.device_views import _remote_device_allowed
from apps.app_automation.views.execution_views import _valid_report_token


User = get_user_model()


class AppAuthorizationBoundaryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='app-owner', password='pass')
        self.outsider = User.objects.create_user(username='app-outsider', password='pass')
        self.project = AppProject.objects.create(name='owned-app', owner=self.owner)
        self.test_case = AppTestCase.objects.create(
            project=self.project, name='private-case', created_by=self.owner,
        )
        self.execution = AppTestExecution.objects.create(
            test_case=self.test_case, user=self.owner, status='pending',
        )
        self.client = APIClient()

    def test_outsider_cannot_read_case_or_execution(self):
        self.client.force_authenticate(user=self.outsider)
        case_response = self.client.get(
            f'/api/app-automation/test-cases/{self.test_case.id}/')
        execution_response = self.client.get(
            f'/api/app-automation/executions/{self.execution.id}/')
        self.assertEqual(case_response.status_code, 404)
        self.assertEqual(execution_response.status_code, 404)

    def test_report_token_is_execution_bound(self):
        token = TimestampSigner().sign(str(self.execution.id))
        self.assertTrue(_valid_report_token(self.execution.id, token))
        self.assertFalse(_valid_report_token(self.execution.id + 1, token))
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, 'index.html').write_text('<h1>ok</h1>', encoding='utf-8')
            self.execution.report_path = temp_dir
            self.execution.save(update_fields=['report_path'])
            good = self.client.get(
                f'/api/app-automation/executions/{self.execution.id}/report/{token}/')
            bad = self.client.get(
                f'/api/app-automation/executions/{self.execution.id}/report/bad-token/')
            self.assertEqual(good.status_code, 200)
            self.assertEqual(bad.status_code, 403)
            good.close()
            bad.close()

    def test_app_websocket_rejects_anonymous_clients(self):
        consumer = AppExecutionConsumer({
            'type': 'websocket',
            'url_route': {'kwargs': {'execution_id': str(self.execution.id)}},
            'user': None,
        })
        consumer.channel_layer = AsyncMock()
        consumer.channel_name = 'security-test'
        consumer.close = AsyncMock()
        asyncio.run(consumer.connect())
        consumer.close.assert_awaited_once()


class AppDeviceNetworkBoundaryTests(TestCase):
    @override_settings(APP_DEVICE_ALLOWED_CIDRS=['192.168.10.0/24'])
    def test_remote_device_requires_explicit_allowlist(self):
        self.assertTrue(_remote_device_allowed('192.168.10.109'))
        self.assertFalse(_remote_device_allowed('10.0.0.1'))
