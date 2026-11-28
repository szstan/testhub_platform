"""Legacy run results must remain scoped, version-consistent and auditable."""
from unittest.mock import patch

import pytest
from django.urls import include, path
from rest_framework.test import APIClient

from apps.executions.models import TestPlan as Plan, TestRun as Run, TestRunCase as RunCase
from apps.projects.models import Project, ProjectMember
from apps.testcases.models import TestCase as Case
from apps.users.models import User
from apps.versions.models import Version

urlpatterns = [path('api/executions/', include('apps.executions.urls'))]
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_urls(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def data():
    owner = User.objects.create_user(username='legacy-repair-owner')
    viewer = User.objects.create_user(username='legacy-repair-viewer')
    project = Project.objects.create(name='Legacy project', owner=owner)
    other = Project.objects.create(name='Other legacy project', owner=owner)
    ProjectMember.objects.create(project=project, user=viewer, role='viewer')
    case = Case.objects.create(project=project, author=owner, title='Audited case', expected_result='ok')
    plan = Plan.objects.create(name='Audit plan', creator=owner)
    plan.projects.add(project)
    run = Run.objects.create(name='Audit run', test_plan=plan, project=project, creator=owner, assignee=owner)
    run_case = RunCase.objects.create(test_run=run, testcase=case)
    version = Version.objects.create(name='Local version', created_by=owner)
    version.projects.add(project)
    foreign = Version.objects.create(name='Foreign version', created_by=owner)
    foreign.projects.add(other)
    client = APIClient()
    client.force_authenticate(owner)
    return locals()


@pytest.mark.parametrize('method,suffix', [('patch', ''), ('put', ''), ('patch', 'update_status/')])
def test_generic_and_dedicated_updates_write_same_audit_identity(data, method, suffix):
    row = data['run_case']
    payload = {'status': 'passed', 'actual_result': 'Verified', 'comments': 'Audit me',
               'executed_by': data['viewer'].pk, 'executed_at': '2000-01-01T00:00:00Z'}
    if method == 'put':
        payload.update(test_run=data['run'].pk, testcase=data['case'].pk, priority='medium')
    response = getattr(data['client'], method)(f'/api/executions/run_cases/{row.pk}/{suffix}', payload, format='json')
    assert response.status_code == 200, response.data
    row.refresh_from_db()
    history = row.history.get()
    assert row.status == history.status == 'passed'
    assert row.executed_by_id == history.executed_by_id == data['owner'].pk
    assert row.executed_at == history.executed_at
    assert row.executed_at.year > 2000
    assert history.actual_result == 'Verified'


@pytest.mark.parametrize('suffix', ['', 'update_status/'])
def test_audit_insert_failure_rolls_back_status_update(data, suffix):
    row = data['run_case']
    with patch('apps.executions.views.TestRunCaseHistory.objects.create', side_effect=RuntimeError('audit unavailable')):
        with pytest.raises(RuntimeError, match='audit unavailable'):
            data['client'].patch(f'/api/executions/run_cases/{row.pk}/{suffix}', {'status': 'passed'}, format='json')
    row.refresh_from_db()
    assert row.status == 'untested'
    assert row.executed_by is None
    assert row.history.count() == 0


@pytest.mark.parametrize('suffix', ['', 'update_status/'])
def test_viewer_cannot_change_execution_results(data, suffix):
    data['client'].force_authenticate(data['viewer'])
    response = data['client'].patch(f"/api/executions/run_cases/{data['run_case'].pk}/{suffix}", {'status': 'passed'}, format='json')
    assert response.status_code == 403
    data['run_case'].refresh_from_db()
    assert data['run_case'].status == 'untested'
    assert not data['run_case'].history.exists()


def test_invalid_status_does_not_create_history(data):
    response = data['client'].patch(f"/api/executions/run_cases/{data['run_case'].pk}/", {'status': 'invented'}, format='json')
    assert response.status_code == 400
    assert not data['run_case'].history.exists()


def test_result_on_create_is_audited(data):
    case = Case.objects.create(project=data['project'], author=data['owner'], title='New case', expected_result='ok')
    response = data['client'].post('/api/executions/run_cases/', {
        'test_run': data['run'].pk, 'testcase': case.pk, 'status': 'failed', 'actual_result': 'Observed',
    }, format='json')
    assert response.status_code == 201, response.data
    row = RunCase.objects.get(pk=response.data['id'])
    assert row.history.get().executed_by_id == data['owner'].pk


@pytest.mark.parametrize('bad_version', ['foreign', 'missing', 'malformed'])
def test_plan_create_rejects_unrelated_or_invalid_version_before_persisting(data, bad_version):
    value = {'foreign': data['foreign'].pk, 'missing': 9999999, 'malformed': 'not-an-id'}[bad_version]
    before = Plan.objects.count()
    response = data['client'].post('/api/executions/plans/', {
        'name': 'Invalid version plan', 'projects': [data['project'].pk], 'version': value,
    }, format='json')
    assert response.status_code == 400, response.data
    assert Plan.objects.count() == before


@pytest.mark.parametrize('method', ['post', 'patch'])
def test_plan_version_must_cover_every_target_project(data, method):
    payload = {'name': 'Partial version plan', 'projects': [data['project'].pk, data['other'].pk], 'version': data['version'].pk}
    url = '/api/executions/plans/' if method == 'post' else f"/api/executions/plans/{data['plan'].pk}/"
    response = getattr(data['client'], method)(url, payload, format='json')
    assert response.status_code == 400, response.data
    data['plan'].refresh_from_db()
    assert data['plan'].name == 'Audit plan'
    assert list(data['plan'].projects.values_list('pk', flat=True)) == [data['project'].pk]


def test_shared_version_is_propagated_to_all_generated_runs(data):
    data['version'].projects.add(data['other'])
    response = data['client'].post('/api/executions/plans/', {
        'name': 'Covered plan', 'projects': [data['project'].pk, data['other'].pk],
        'version': data['version'].pk, 'testcases': [data['case'].pk],
    }, format='json')
    assert response.status_code == 201, response.data
    plan = Plan.objects.get(pk=response.data['id'])
    assert plan.test_runs.count() == 2
    assert set(plan.test_runs.values_list('version_id', flat=True)) == {data['version'].pk}


def test_plan_child_failure_rolls_back_parent_creation(data):
    before = Plan.objects.count()
    with patch('apps.executions.views.TestRun.objects.create', side_effect=RuntimeError('child failure')):
        with pytest.raises(RuntimeError, match='child failure'):
            data['client'].post('/api/executions/plans/', {
                'name': 'Rolled back', 'projects': [data['project'].pk], 'version': data['version'].pk,
            }, format='json')
    assert Plan.objects.count() == before


def test_viewer_cannot_create_test_plan_for_visible_project(data):
    data['client'].force_authenticate(data['viewer'])
    response = data['client'].post('/api/executions/plans/', {'name': 'Viewer plan', 'projects': [data['project'].pk]}, format='json')
    assert response.status_code == 403
