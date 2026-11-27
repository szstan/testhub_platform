import uuid
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db.models import DateField, Value
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.perf_testing.models import (
    PerfExecution,
    PerfProject,
    PerfRequestStat,
    PerfScenario,
)

User = get_user_model()


class PerfDashboardApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username=f'dashboard-{uuid.uuid4().hex[:12]}', password='test-only')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.project = self._project(self.user, 'visible')
        self.project.members.add(*[
            User.objects.create_user(username=f'project-member-{uuid.uuid4().hex[:8]}')
            for _ in range(2)
        ])
        self.scenario = self._scenario(self.project, 'visible-scenario')

    def _project(self, owner, name):
        return PerfProject.objects.create(name=name, owner=owner)

    def _scenario(self, project, name):
        return PerfScenario.objects.create(
            project=project, created_by=self.user, name=name)

    def _execution(self, *, project=None, scenario=None, status='COMPLETED',
                   sla_result='NOT_EVALUATED', summary=None, created_at=None):
        project = project or self.project
        scenario = scenario or self.scenario
        execution = PerfExecution.objects.create(
            project=project,
            scenario=scenario,
            execution_no=f'DASH-{uuid.uuid4().hex}',
            status=status,
            sla_result=sla_result,
            summary=summary or {},
        )
        if created_at is not None:
            PerfExecution.objects.filter(pk=execution.pk).update(created_at=created_at)
            execution.created_at = created_at
        return execution

    def _stat(self, execution, name, p95_rt, method='GET'):
        return PerfRequestStat.objects.create(
            execution=execution,
            step_name=name,
            method=method,
            p95_rt=p95_rt,
        )

    def _dashboard(self, **params):
        return self.client.get('/api/perf-testing/executions/dashboard/', params)

    def test_daily_window_uses_local_midnight_excludes_future_and_limits_recent(self):
        local_tz = ZoneInfo('Asia/Shanghai')
        now = datetime(2025, 5, 31, 18, 30, tzinfo=ZoneInfo('UTC'))
        local_start = datetime(2025, 5, 26, 0, 0, tzinfo=local_tz)
        with timezone.override(local_tz), patch('django.utils.timezone.now', return_value=now):
            self._execution(created_at=local_start)
            self._execution(created_at=local_start - timedelta(microseconds=1))
            self._execution(created_at=datetime(2025, 5, 31, 12, 0, tzinfo=local_tz))
            today_executions = [
                self._execution(created_at=now - timedelta(minutes=max(index - 1, 0)))
                for index in range(12)
            ]
            self._execution(created_at=now + timedelta(microseconds=1))

            response = self._dashboard(days='7')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['total_executions'], 14)
        self.assertEqual(
            response.data['daily_executions'],
            [
                {'date': '2025-05-26', 'count': 1},
                {'date': '2025-05-27', 'count': 0},
                {'date': '2025-05-28', 'count': 0},
                {'date': '2025-05-29', 'count': 0},
                {'date': '2025-05-30', 'count': 0},
                {'date': '2025-05-31', 'count': 1},
                {'date': '2025-06-01', 'count': 12},
            ],
        )
        ordered_today = sorted(
            today_executions,
            key=lambda execution: (execution.created_at, execution.pk),
            reverse=True,
        )
        self.assertEqual(
            [item['id'] for item in response.data['recent']],
            [execution.pk for execution in ordered_today[:10]],
        )

    def test_null_date_buckets_fall_back_to_application_timezone(self):
        local_tz = ZoneInfo('Asia/Shanghai')
        now = datetime(2025, 5, 31, 18, 30, tzinfo=ZoneInfo('UTC'))
        local_start = datetime(2025, 5, 26, 0, 0, tzinfo=local_tz)
        with timezone.override(local_tz), patch('django.utils.timezone.now', return_value=now):
            boundary = self._execution(created_at=local_start)
            self._execution(created_at=local_start - timedelta(microseconds=1))
            self._execution(created_at=now)
            with patch('apps.perf_testing.views.TruncDate', return_value=Value(None, output_field=DateField())):
                response = self._dashboard(days='7')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['time_zone'], 'Asia/Shanghai')
        self.assertEqual(response.data['total_executions'], 2)
        self.assertEqual(
            [(entry['date'], entry['count']) for entry in response.data['daily_executions']],
            [(f'2025-05-{day}', int(day == 26)) for day in range(26, 32)] + [('2025-06-01', 1)],
        )
        self.assertEqual(response.data['recent'][-1]['id'], boundary.pk)
        self.assertEqual(response.data['recent'][-1]['created_at'], '2025-05-26T00:00:00+08:00')

    def test_90_day_and_default_windows_are_calendar_day_ranges(self):
        local_tz = ZoneInfo('Asia/Shanghai')
        now = datetime(2025, 5, 31, 18, 30, tzinfo=ZoneInfo('UTC'))
        today = date(2025, 6, 1)
        first_90_day = today - timedelta(days=89)
        first_90_midnight = datetime.combine(first_90_day, time.min, tzinfo=local_tz)
        with timezone.override(local_tz), patch('django.utils.timezone.now', return_value=now):
            self._execution(created_at=first_90_midnight)
            self._execution(created_at=first_90_midnight - timedelta(microseconds=1))

            response_90 = self._dashboard(days='90')
            response_default = self._dashboard()

        self.assertEqual(response_90.status_code, 200, response_90.data)
        self.assertEqual(len(response_90.data['daily_executions']), 90)
        self.assertEqual(response_90.data['daily_executions'][0], {
            'date': first_90_day.isoformat(), 'count': 1,
        })
        self.assertEqual(response_90.data['daily_executions'][-1]['date'], today.isoformat())
        self.assertEqual(response_90.data['total_executions'], 1)
        self.assertEqual(response_default.status_code, 200, response_default.data)
        self.assertEqual(len(response_default.data['daily_executions']), 30)
        self.assertEqual(
            response_default.data['daily_executions'][0]['date'],
            (today - timedelta(days=29)).isoformat(),
        )
        self.assertEqual(response_default.data['total_executions'], 0)

    def test_status_metrics_and_sla_use_their_defined_populations(self):
        self._execution(status='COMPLETED', sla_result='PASSED', summary={'tps': 10})
        self._execution(status='COMPLETED', sla_result='PASSED', summary={'tps': 20})
        self._execution(status='COMPLETED', summary={'tps': 0})
        self._execution(status='COMPLETED', summary={'tps': -1})
        self._execution(status='COMPLETED', summary={'tps': 'Infinity'})
        self._execution(status='COMPLETED', summary={})
        self._execution(status='FAILED', sla_result='FAILED', summary={'tps': 500})
        self._execution(status='STOPPED', summary={'tps': 500})
        self._execution(status='TIMEOUT', summary={'tps': 500})
        active_old = self._execution(
            status='PENDING', created_at=timezone.now() - timedelta(days=90))
        active_current = [
            self._execution(status=status)
            for status in ('PREPARING', 'RUNNING', 'STOPPING')
        ]

        response = self._dashboard(days='7')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['total_executions'], 12)
        self.assertEqual(response.data['completed'], 6)
        self.assertEqual(response.data['failed'], 2)
        self.assertEqual(response.data['running_count'], 4)
        self.assertEqual(
            {item['id'] for item in response.data['running']},
            {active_old.pk, *(execution.pk for execution in active_current)},
        )
        self.assertEqual(response.data['avg_tps'], 10)
        self.assertEqual(response.data['sla_counts'], {
            'PASSED': 2, 'FAILED': 1, 'NOT_EVALUATED': 6,
        })
        self.assertEqual(response.data['sla_pass_rate'], 66.7)
        self.assertEqual(sum(day['count'] for day in response.data['daily_executions']), 12)

    def test_project_isolation_scenario_count_and_slowest_source(self):
        member_project = self._project(User.objects.create_user(
            username=f'member-owner-{uuid.uuid4().hex[:8]}'), 'member-project')
        member_project.members.add(self.user)
        self._scenario(member_project, 'member-scenario')
        hidden_owner = User.objects.create_user(
            username=f'hidden-owner-{uuid.uuid4().hex[:8]}')
        hidden_project = self._project(hidden_owner, 'hidden-project')
        hidden_scenario = self._scenario(hidden_project, 'hidden-scenario')
        self._scenario(self.project, 'unexecuted-scenario')

        older_slow = self._execution(
            status='FAILED', sla_result='FAILED', created_at=timezone.now() - timedelta(minutes=10))
        self._stat(older_slow, 'older-slow', 99)
        selected_slow = self._execution(
            status='STOPPED', sla_result='NOT_EVALUATED',
            created_at=timezone.now() - timedelta(minutes=5))
        selected_stats = [
            self._stat(selected_slow, f'step-{p95}', p95)
            for p95 in (10, 70, 30, 60, 20, 50, 40)
        ]
        selected_stats.append(self._stat(selected_slow, 'step-70b', 70))
        no_positive_p95 = self._execution(
            status='COMPLETED', created_at=timezone.now() - timedelta(minutes=2))
        self._stat(no_positive_p95, 'no-p95', 0)
        active = self._execution(status='RUNNING', created_at=timezone.now() - timedelta(minutes=1))
        self._stat(active, 'active-slow', 1000)
        hidden = self._execution(
            project=hidden_project, scenario=hidden_scenario, status='COMPLETED',
            created_at=timezone.now())
        self._stat(hidden, 'hidden-slow', 10000)

        response = self._dashboard()
        hidden_response = self._dashboard(project=str(hidden_project.pk))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['total_scenarios'], 3)
        self.assertEqual(response.data['total_executions'], 4)
        self.assertEqual(response.data['slowest']['execution']['id'], selected_slow.pk)
        self.assertEqual(response.data['slowest']['execution']['scenario_name'], self.scenario.name)
        self.assertEqual(
            response.data['slowest']['items'],
            [
                {'step_name': 'step-70', 'p95_rt': 70, 'method': 'GET'},
                {'step_name': 'step-70b', 'p95_rt': 70, 'method': 'GET'},
                {'step_name': 'step-60', 'p95_rt': 60, 'method': 'GET'},
                {'step_name': 'step-50', 'p95_rt': 50, 'method': 'GET'},
                {'step_name': 'step-40', 'p95_rt': 40, 'method': 'GET'},
            ],
        )
        self.assertEqual(len(selected_stats), 8)
        self.assertEqual(hidden_response.status_code, 200, hidden_response.data)
        self.assertEqual(hidden_response.data['total_executions'], 0)
        self.assertEqual(hidden_response.data['total_scenarios'], 0)
        self.assertEqual(hidden_response.data['avg_tps'], None)
        self.assertEqual(hidden_response.data['sla_pass_rate'], None)
        self.assertEqual(hidden_response.data['sla_counts'], {
            'PASSED': 0, 'FAILED': 0, 'NOT_EVALUATED': 0,
        })
        self.assertEqual(hidden_response.data['daily_executions'][-1]['count'], 0)
        self.assertIsNone(hidden_response.data['slowest'])
        self.assertEqual(hidden_response.data['recent'], [])
        self.assertEqual(hidden_response.data['running'], [])

    def test_invalid_days_are_rejected(self):
        for days in ('0', '14', 'abc', ''):
            with self.subTest(days=days):
                response = self._dashboard(days=days)
                self.assertEqual(response.status_code, 400, response.data)
