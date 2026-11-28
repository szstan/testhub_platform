"""Report aggregations stay correct without a query for each run/case."""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import include, path
from django.utils import timezone
from rest_framework.test import APIClient

from apps.executions.models import TestPlan as Plan, TestRun as Run, TestRunCase as RunCase
from apps.projects.models import Project
from apps.testcases.models import TestCase as Case
from apps.users.models import User

urlpatterns = [path('api/reports/', include('apps.reports.urls'))]
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_urls(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def data():
    owner = User.objects.create_user(username='report-scaling-owner')
    project = Project.objects.create(name='Report scaling', owner=owner)
    plan = Plan.objects.create(name='Scaling plan', creator=owner)
    plan.projects.add(project)
    case = Case.objects.create(project=project, author=owner, title='Report case', expected_result='ok')
    run = Run.objects.create(name='First run', test_plan=plan, project=project, assignee=owner, creator=owner)
    row = RunCase.objects.create(test_run=run, testcase=case, status='passed', executed_at=timezone.now(), defects=[1, 2])
    client = APIClient()
    client.force_authenticate(owner)
    return locals()


def test_dashboard_query_count_does_not_grow_per_run(data):
    url = '/api/reports/reports/dashboard/'
    with CaptureQueriesContext(connection) as small:
        first = data['client'].get(url)
    assert first.status_code == 200, first.data
    assert first.data['plan_progress'] == 100
    assert first.data['pass_rate'] == 100
    assert first.data['total_defects'] == 2
    for index in range(25):
        run = Run.objects.create(name=f'Run {index}', test_plan=data['plan'], project=data['project'], assignee=data['owner'], creator=data['owner'])
        RunCase.objects.create(test_run=run, testcase=data['case'], status='failed')
    with CaptureQueriesContext(connection) as large:
        response = data['client'].get(url)
    assert response.status_code == 200, response.data
    assert response.data['plan_progress'] == 100
    assert response.data['pass_rate'] == 0  # the most recent ten runs failed
    assert response.data['total_defects'] == 2
    assert len(large) <= len(small) + 1, [query['sql'] for query in large]
    assert len(large) <= 8


def test_dashboard_handles_run_without_cases_and_excludes_other_project_runs(data):
    empty = Run.objects.create(name='Empty run', test_plan=data['plan'], project=data['project'], assignee=data['owner'], creator=data['owner'])
    outsider = User.objects.create_user(username='report-scaling-outsider')
    other = Project.objects.create(name='Hidden project', owner=outsider)
    data['plan'].projects.add(other)
    other_case = Case.objects.create(project=other, author=outsider, title='Hidden case', expected_result='ok')
    hidden = Run.objects.create(name='Hidden run', test_plan=data['plan'], project=other, assignee=outsider, creator=outsider)
    RunCase.objects.create(test_run=hidden, testcase=other_case, status='failed', defects=[9, 10, 11])
    response = data['client'].get('/api/reports/reports/dashboard/')
    assert response.status_code == 200, response.data
    assert response.data['plan_progress'] == 50
    assert response.data['pass_rate'] == 100
    assert response.data['total_defects'] == 2


@pytest.mark.parametrize('days', ['abc', '0', '-1', '367', '999999999999999999999999'])
def test_trend_rejects_invalid_ranges_before_querying_executions(data, days):
    with CaptureQueriesContext(connection) as queries:
        response = data['client'].get('/api/reports/reports/execution_trend/', {'days': days})
    assert response.status_code == 400
    assert not any('test_run_cases' in query['sql'] for query in queries)


def test_trend_groups_by_local_date_in_database_and_excludes_future_rows(data, settings):
    settings.TIME_ZONE = 'Asia/Shanghai'
    now = timezone.make_aware(datetime(2026, 5, 2, 12), timezone.get_current_timezone())
    data['row'].executed_at = now
    data['row'].save(update_fields=['executed_at'])
    other_case = Case.objects.create(project=data['project'], author=data['owner'], title='Future case', expected_result='ok')
    RunCase.objects.create(test_run=data['run'], testcase=other_case, status='passed', executed_at=now + timedelta(days=10))
    with patch('apps.reports.views.timezone.now', return_value=now), CaptureQueriesContext(connection) as queries:
        response = data['client'].get('/api/reports/reports/execution_trend/', {'days': 2})
    assert response.status_code == 200, response.data
    assert response.data == [{'date': '2026-05-01', 'count': 0}, {'date': '2026-05-02', 'count': 1}]
    assert any('GROUP BY' in query['sql'].upper() for query in queries)
