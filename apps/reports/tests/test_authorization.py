import pytest
from django.urls import include, path
from rest_framework.test import APIClient

from apps.executions.models import (
    ExecutionJob,
    TestPlan as PlanModel,
    TestRun as RunModel,
)
from apps.projects.models import Project
from apps.reports.models import TestReport as ReportModel
from apps.users.models import User


urlpatterns = [path('api/reports/', include('apps.reports.urls'))]


@pytest.fixture(autouse=True)
def isolated_urls(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def report_boundary_data(db):
    owner = User.objects.create_user(username='report-boundary-owner', password='pass')
    outsider = User.objects.create_user(username='report-boundary-outsider', password='pass')
    project = Project.objects.create(name='Report Boundary Project', owner=owner)
    other_project = Project.objects.create(name='Other Report Boundary Project', owner=outsider)
    plan = PlanModel.objects.create(name='Report boundary plan', creator=owner)
    plan.projects.add(project)
    other_plan = PlanModel.objects.create(name='Other report boundary plan', creator=outsider)
    other_plan.projects.add(other_project)
    execution = RunModel.objects.create(
        name='Report boundary run',
        test_plan=plan,
        project=project,
        assignee=owner,
        creator=owner,
    )
    other_execution = RunModel.objects.create(
        name='Other report boundary run',
        test_plan=other_plan,
        project=other_project,
        assignee=outsider,
        creator=outsider,
    )
    job = ExecutionJob.objects.create(project=project, requested_by=owner, source_type='api')
    other_job = ExecutionJob.objects.create(project=other_project, requested_by=outsider, source_type='api')
    return locals()


def report_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_report_create_update_require_project_access_and_consistent_relations(report_boundary_data):
    data = report_boundary_data
    client = report_client(data['owner'])

    created = client.post(
        '/api/reports/reports/',
        {
            'project': data['project'].id,
            'name': 'same-project-report',
            'report_type': 'execution',
            'execution': data['execution'].id,
            'execution_job': data['job'].id,
            'summary': {'passed': 1},
            'content': {'cases': []},
        },
        format='json',
    )
    assert created.status_code == 201, created.data
    report_id = created.data['id']

    valid_update = client.patch(
        f'/api/reports/reports/{report_id}/',
        {'name': 'renamed-report'},
        format='json',
    )
    assert valid_update.status_code == 200, valid_update.data

    mismatch_job = client.post(
        '/api/reports/reports/',
        {
            'project': data['project'].id,
            'name': 'cross-job-report',
            'report_type': 'execution',
            'execution_job': data['other_job'].id,
        },
        format='json',
    )
    assert mismatch_job.status_code == 400, mismatch_job.data

    mismatch_execution = client.post(
        '/api/reports/reports/',
        {
            'project': data['project'].id,
            'name': 'cross-execution-report',
            'report_type': 'execution',
            'execution': data['other_execution'].id,
        },
        format='json',
    )
    assert mismatch_execution.status_code == 400, mismatch_execution.data

    denied_project = client.post(
        '/api/reports/reports/',
        {
            'project': data['other_project'].id,
            'name': 'unauthorized-report',
            'report_type': 'summary',
        },
        format='json',
    )
    assert denied_project.status_code == 403, denied_project.data

    denied_update = client.patch(
        f'/api/reports/reports/{report_id}/',
        {'project': data['other_project'].id},
        format='json',
    )
    assert denied_update.status_code == 403, denied_update.data

    mismatch_update = client.patch(
        f'/api/reports/reports/{report_id}/',
        {'execution_job': data['other_job'].id},
        format='json',
    )
    assert mismatch_update.status_code == 400, mismatch_update.data

    assert ReportModel.objects.filter(pk=report_id, project=data['project']).exists()
