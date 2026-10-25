from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.monitor.models import MonitorCheckLog, MonitorTarget
from apps.monitor.serializers import MonitorCheckLogSerializer
from apps.monitor.utils.probes import ProbeResult


User = get_user_model()


class MonitorAuthorizationBoundaryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='monitor-owner', password='pass')
        self.outsider = User.objects.create_user(username='monitor-outsider', password='pass')
        self.target = MonitorTarget.objects.create(
            name='private-monitor-target',
            type='HTTP',
            url='https://example.com/health',
            check_config={'password': 'secret-value'},
            created_by=self.owner,
        )
        self.client = APIClient()

    def test_outsider_cannot_read_or_list_target(self):
        self.client.force_authenticate(user=self.outsider)
        detail = self.client.get(f'/api/monitor/targets/{self.target.id}/')
        listing = self.client.get('/api/monitor/targets/')
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(listing.data['count'], 0)

    def test_login_token_is_redacted_before_checklog_persistence(self):
        from unittest.mock import patch

        with patch(
            'apps.monitor.utils.probes.run_probe',
            return_value=ProbeResult(
                True,
                '登录成功',
                detail={'token': 'super-secret-token', 'token_len': 18},
            ),
        ):
            log = self.target.run_check()

        self.assertEqual(log.detail['token'], '******')
        self.assertNotIn('super-secret-token', repr(log.detail))

    def test_checklog_serializer_redacts_legacy_bearer_data(self):
        log = MonitorCheckLog.objects.create(
            target=self.target,
            detail={'token': 'legacy-secret-token'},
            error_message='Authorization: Bearer legacy-secret-token',
        )

        data = MonitorCheckLogSerializer(log).data
        self.assertNotIn('legacy-secret-token', repr(data))
        self.assertEqual(data['detail']['token'], '******')
        self.assertIn('[REDACTED]', data['error_message'])
