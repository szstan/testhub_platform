from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.monitor.models import MonitorTarget


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

    def test_owner_receives_masked_secret(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.get(f'/api/monitor/targets/{self.target.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['check_config']['password'], '******')
