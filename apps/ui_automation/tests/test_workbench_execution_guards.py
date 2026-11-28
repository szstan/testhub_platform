import uuid
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.ui_automation import tasks
from apps.ui_automation.models import (
    Element,
    LocatorStrategy,
    TestCase as UiTestCase,
    TestCaseExecution as UiTestCaseExecution,
    TestExecution as UiTestExecution,
    TestSuite as UiTestSuite,
    TestSuiteTestCase as UiTestSuiteTestCase,
    UiProject,
)
from apps.ui_automation.suite_runner import _finish_suite

User = get_user_model()


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _case_and_suite(name):
    user = User.objects.create_user(username=f'{name}-owner')
    project = UiProject.objects.create(
        name=name,
        base_url=f'https://{name}.example.test',
        owner=user,
    )
    case = UiTestCase.objects.create(project=project, name=f'{name}-case', created_by=user)
    suite = UiTestSuite.objects.create(project=project, name=f'{name}-suite')
    UiTestSuiteTestCase.objects.create(test_suite=suite, test_case=case)
    return user, project, case, suite


@pytest.mark.django_db
def test_case_rerun_from_suite_keeps_safe_origin_id_without_wrong_fk(monkeypatch):
    user, project, case, suite = _case_and_suite('rerun-origin')
    original = UiTestExecution.objects.create(
        project=project,
        test_suite=suite,
        status='FAILED',
        result_data={'test_cases': [{'test_case_id': case.pk, 'success': False}]},
    )
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', Mock())
    client = _client(user)

    response = client.post(
        f'/api/ui-automation/test-cases/{case.pk}/run/',
        {
            'async': True,
            'client_request_id': str(uuid.uuid4()),
            'project_id': project.pk,
            'engine': 'playwright',
            'browser': 'chrome',
            'headless': True,
            'rerun_of': original.pk,
            'rerun_kind': 'suite',
        },
        format='json',
    )

    assert response.status_code == 202
    execution = UiTestCaseExecution.objects.get(pk=response.data['execution_id'])
    assert execution.rerun_of_id is None
    assert execution.rerun_origin_id == original.pk
    assert execution.rerun_kind == 'suite'
    detail = client.get(f'/api/ui-automation/test-case-executions/{execution.pk}/')
    assert detail.status_code == 200
    assert detail.data['rerun_of'] == original.pk
    assert detail.data['rerun_kind'] == 'suite'
    assert 'rerun_origin_id' not in detail.data


@pytest.mark.django_db
def test_suite_abort_endpoint_cancels_parent_and_children_and_fences_finish():
    user, project, case, suite = _case_and_suite('suite-abort')
    claim = uuid.uuid4()
    execution = UiTestExecution.objects.create(
        project=project,
        test_suite=suite,
        executed_by=user,
        status='RUNNING',
        dispatch_claim=claim,
        request_snapshot={'config': {}, 'cases': []},
    )
    child = UiTestCaseExecution.objects.create(
        test_case=case,
        project=project,
        test_suite=suite,
        parent_execution=execution,
        created_by=user,
        status='running',
    )
    client = _client(user)

    fetched = client.get(f'/api/ui-automation/test-executions/{execution.pk}/')
    assert fetched.status_code == 200
    assert not {'dispatch_claim', 'cancel_requested_at', 'request_snapshot'} & set(fetched.data)
    aborted = client.post(f'/api/ui-automation/test-executions/{execution.pk}/abort/')

    assert aborted.status_code == 200
    assert aborted.data['status'] == 'ABORTED'
    execution.refresh_from_db()
    child.refresh_from_db()
    assert execution.status == 'ABORTED'
    assert execution.cancel_requested_at is not None
    assert child.status == 'aborted'
    assert child.cancel_requested_at is not None
    assert _finish_suite(execution.pk, str(claim), status='SUCCESS') is False

    outsider = User.objects.create_user(username='suite-abort-outsider')
    denied = _client(outsider).post(f'/api/ui-automation/test-executions/{execution.pk}/abort/')
    assert denied.status_code == 404


@pytest.mark.django_db
def test_async_suite_claim_marks_suite_running_and_uses_snapshot_base_url(monkeypatch):
    from apps.ui_automation.suite_runner import execute_suite_execution

    user, project, _case, suite = _case_and_suite('suite-snapshot-target')
    project.base_url = 'https://current.example.test'
    project.save(update_fields=['base_url'])
    snapshot_url = 'https://accepted.example.test/start'
    claim = uuid.uuid4()
    execution = UiTestExecution.objects.create(
        project=project,
        test_suite=suite,
        executed_by=user,
        status='PENDING',
        dispatch_claim=claim,
        request_snapshot={
            'config': {'engine': 'playwright', 'browser': 'chrome', 'headless': True},
            'base_url': snapshot_url,
            'cases': [{'id': 1, 'name': 'snapshot case', 'project_id': project.pk, 'steps': []}],
        },
    )
    captured = {}

    monkeypatch.setattr('apps.ui_automation.suite_runner._check_environment', lambda *_args: (True, ''))

    class FakeExecutor:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            suite.refresh_from_db()
            captured['suite_status_at_start'] = suite.execution_status

    monkeypatch.setattr('apps.ui_automation.test_executor.TestExecutor', FakeExecutor)

    assert execute_suite_execution(execution.pk, str(claim)) is True
    assert captured['base_url'] == snapshot_url
    assert captured['suite_status_at_start'] == 'running'
    execution.refresh_from_db()
    assert execution.status == 'RUNNING'


@pytest.mark.django_db
def test_element_pagination_keeps_twenty_default_and_allows_one_hundred():
    user = User.objects.create_user(username='element-pagination-owner')
    project = UiProject.objects.create(
        name='element-pagination',
        base_url='https://element-pagination.example.test',
        owner=user,
    )
    strategy = LocatorStrategy.objects.create(name='css')
    Element.objects.bulk_create([
        Element(
            project=project,
            name=f'element-{index}',
            locator_strategy=strategy,
            locator_value=f'#element-{index}',
            created_by=user,
        )
        for index in range(101)
    ])
    client = _client(user)

    default_page = client.get('/api/ui-automation/elements/', {'project': project.pk})
    hundred_page = client.get('/api/ui-automation/elements/', {
        'project': project.pk,
        'page_size': 100,
    })

    assert default_page.status_code == 200
    assert default_page.data['count'] == 101
    assert len(default_page.data['results']) == 20
    assert default_page.data['next']
    assert hundred_page.status_code == 200
    assert len(hundred_page.data['results']) == 100
    assert hundred_page.data['next']
