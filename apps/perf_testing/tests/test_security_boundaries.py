from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.perf_testing.models import PerfExecution, PerfProject, PerfScenario


User = get_user_model()


class PerformanceAuthorizationBoundaryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='perf-owner', password='pass')
        self.outsider = User.objects.create_user(username='perf-outsider', password='pass')
        self.project = PerfProject.objects.create(name='private-perf', owner=self.owner)
        self.scenario = PerfScenario.objects.create(
            project=self.project,
            created_by=self.owner,
            name='private-scenario',
            engine='BUILTIN',
        )
        self.execution = PerfExecution.objects.create(
            scenario=self.scenario,
            project=self.project,
            execution_no='SECURITY-1',
            executed_by=self.owner,
            status='COMPLETED',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.outsider)

    def test_outsider_cannot_read_project_scenario_or_execution(self):
        for path in (
            f'/api/perf-testing/projects/{self.project.id}/',
            f'/api/perf-testing/scenarios/{self.scenario.id}/',
            f'/api/perf-testing/executions/{self.execution.id}/',
        ):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_outsider_cannot_list_private_resources(self):
        self.assertEqual(self.client.get('/api/perf-testing/projects/').data['count'], 0)
        self.assertEqual(self.client.get('/api/perf-testing/scenarios/').data['count'], 0)
        self.assertEqual(self.client.get('/api/perf-testing/executions/').data['count'], 0)
