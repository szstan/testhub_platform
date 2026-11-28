import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.ui_automation import tasks, test_executor
from apps.ui_automation.models import (
    TestCase as UiTestCase,
    TestCaseExecution as UiCaseExecution,
    TestExecution as UiSuiteExecution,
    TestSuite as UiSuite,
    TestSuiteTestCase as UiSuiteCase,
    UiProject,
)


@pytest.fixture
def context(db):
    user = get_user_model().objects.create_user(username='workbench-final-review')
    project = UiProject.objects.create(name='review', base_url='https://review.example.test', owner=user)
    case = UiTestCase.objects.create(project=project, name='case', created_by=user)
    suite = UiSuite.objects.create(project=project, name='suite')
    UiSuiteCase.objects.create(test_suite=suite, test_case=case)
    client = APIClient()
    client.force_authenticate(user=user)
    return user, project, case, suite, client


class StopBeforeBrowserActions(BaseException):
    """Stop the real executor immediately after its first child lifecycle save."""


@pytest.mark.parametrize('engine', ['playwright', 'selenium'])
def test_real_suite_executor_keeps_parent_link_through_first_save_and_cancel(context, monkeypatch, engine):
    user, project, case, suite, client = context
    parent = UiSuiteExecution.objects.create(
        project=project, test_suite=suite, executed_by=user,
        status='RUNNING', dispatch_claim=uuid.uuid4(),
    )
    executor = test_executor.TestExecutor(suite, engine=engine, executed_by=user, execution=parent)
    executor.test_cases = [case]
    monkeypatch.setattr(test_executor, 'sync_playwright', lambda: nullcontext(SimpleNamespace()))
    monkeypatch.setattr('apps.ui_automation.playwright_engine.PlaywrightTestEngine.check_execution_environment_sync', lambda *_: (True, ''))
    monkeypatch.setattr(executor, 'create_selenium_driver', lambda: SimpleNamespace(quit=lambda: None))
    original_save = UiCaseExecution.save
    reached = []

    def save_and_cancel(instance, *args, **kwargs):
        if instance._state.adding or instance.status != 'running':
            return original_save(instance, *args, **kwargs)
        original_save(instance, *args, **kwargs)
        stored = UiCaseExecution.objects.get(pk=instance.pk)
        assert stored.parent_execution_id == parent.pk
        assert instance.parent_execution_id == parent.pk
        reached.append(instance.pk)
        aborted = client.post(f'/api/ui-automation/test-executions/{parent.pk}/abort/')
        assert aborted.status_code == 200
        instance.status = 'passed'
        original_save(instance)
        stored.refresh_from_db()
        assert stored.parent_execution_id == parent.pk
        assert stored.status == 'aborted'
        raise StopBeforeBrowserActions()

    monkeypatch.setattr(UiCaseExecution, 'save', save_and_cancel)
    with pytest.raises(StopBeforeBrowserActions):
        getattr(executor, f'run_with_{engine}')()
    assert len(reached) == 1


@pytest.mark.parametrize('mutation', ['empty', 'case_project_changed'])
def test_suite_replay_precedes_mutable_membership_preflight(context, monkeypatch, mutation):
    user, project, case, suite, client = context
    dispatch = Mock()
    monkeypatch.setattr(tasks.execute_workbench_suite, 'apply_async', dispatch)
    payload = {'client_request_id': str(uuid.uuid4()), 'engine': 'playwright', 'browser': 'chrome', 'headless': True}
    url = f'/api/ui-automation/test-suites/{suite.pk}/run_suite/'
    first = client.post(url, payload, format='json')
    assert first.status_code == 202
    if mutation == 'empty':
        changed = client.delete(f'/api/ui-automation/test-suites/{suite.pk}/remove_test_case/', {'test_case_id': case.pk}, format='json')
        assert changed.status_code == 204
    else:
        other = UiProject.objects.create(name='moved', base_url='https://moved.example.test', owner=user)
        changed = client.patch(f'/api/ui-automation/test-cases/{case.pk}/', {'project': other.pk}, format='json')
        assert changed.status_code == 200

    replay = client.post(url, payload, format='json')
    assert replay.status_code == 202
    assert replay.data['execution_id'] == first.data['execution_id']
    assert replay.data['replayed'] is True
    assert replay.data['test_case_count'] == 1
    conflict = client.post(url, {**payload, 'browser': 'firefox'}, format='json')
    assert conflict.status_code == 409
    new_request = client.post(url, {**payload, 'client_request_id': str(uuid.uuid4())}, format='json')
    assert new_request.status_code == 400
    assert UiSuiteExecution.objects.count() == 1
    assert dispatch.call_count == 1


@pytest.mark.parametrize('kind', ['case', 'suite', 'suite_child'])
def test_worker_owned_execution_patch_rejects_synthetic_state(context, kind):
    user, project, case, suite, client = context
    if kind == 'suite':
        row = UiSuiteExecution.objects.create(project=project, test_suite=suite, executed_by=user, status='PENDING', dispatch_claim=uuid.uuid4())
        url = f'/api/ui-automation/test-executions/{row.pk}/'
        terminal = 'SUCCESS'
    elif kind == 'suite_child':
        parent = UiSuiteExecution.objects.create(project=project, test_suite=suite, status='RUNNING', dispatch_claim=uuid.uuid4())
        row = UiCaseExecution.objects.create(test_case=case, project=project, test_suite=suite, parent_execution=parent, created_by=user, status='running')
        url = f'/api/ui-automation/test-case-executions/{row.pk}/'
        terminal = 'passed'
    else:
        row = UiCaseExecution.objects.create(test_case=case, project=project, created_by=user, status='pending', dispatch_claim=uuid.uuid4())
        url = f'/api/ui-automation/test-case-executions/{row.pk}/'
        terminal = 'passed'
    previous = row.status
    patched = client.patch(url, {'status': terminal}, format='json')
    assert patched.status_code == 400
    row.refresh_from_db()
    assert row.status == previous
    assert client.get(url).data['status'] == previous


@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_legacy_execution_patch_remains_compatible_without_worker_ownership(context, kind):
    user, project, case, suite, client = context
    if kind == 'suite':
        row = UiSuiteExecution.objects.create(project=project, test_suite=suite, executed_by=user, status='PENDING')
        url = f'/api/ui-automation/test-executions/{row.pk}/'
        terminal = 'SUCCESS'
    else:
        row = UiCaseExecution.objects.create(test_case=case, project=project, created_by=user, status='pending')
        url = f'/api/ui-automation/test-case-executions/{row.pk}/'
        terminal = 'passed'
    patched = client.patch(url, {'status': terminal}, format='json')
    assert patched.status_code == 200
    assert patched.data['status'] == terminal
    row.refresh_from_db()
    assert row.status == terminal


def test_explicit_empty_steps_removes_the_last_saved_step(context):
    _user, _project, case, _suite, client = context
    case.steps.create(step_number=1, action_type='wait', wait_time=1000)
    response = client.patch(f'/api/ui-automation/test-cases/{case.pk}/', {'steps': []}, format='json')
    assert response.status_code == 200
    assert response.data['steps'] == []
    assert case.steps.count() == 0


def test_metadata_only_case_patch_preserves_existing_steps(context):
    _user, _project, case, _suite, client = context
    old = case.steps.create(step_number=1, action_type='wait', wait_time=1000, description='original')
    response = client.patch(f'/api/ui-automation/test-cases/{case.pk}/', {'name': 'renamed'}, format='json')
    assert response.status_code == 200
    assert list(case.steps.values_list('pk', flat=True)) == [old.pk]


def test_failed_case_step_update_rolls_back_steps_and_metadata(context):
    _user, _project, case, _suite, client = context
    old = case.steps.create(step_number=1, action_type='wait', wait_time=1000, description='original')
    response = client.patch(f'/api/ui-automation/test-cases/{case.pk}/', {
        'name': 'must roll back',
        'steps': [{'action_type': 'wait', 'wait_time': 10}, {'action_type': 'wait', 'wait_time': 'not-a-number'}],
    }, format='json')
    assert response.status_code == 400
    case.refresh_from_db()
    assert case.name == 'case'
    assert list(case.steps.values_list('pk', flat=True)) == [old.pk]
    old.refresh_from_db()
    assert old.description == 'original'


@pytest.mark.parametrize('steps', [None, {}])
def test_malformed_step_collections_are_rejected_without_saving_metadata(context, steps):
    _user, _project, case, _suite, client = context
    response = client.patch(f'/api/ui-automation/test-cases/{case.pk}/', {'name': 'must not save', 'steps': steps}, format='json')
    assert response.status_code == 400
    case.refresh_from_db()
    assert case.name == 'case'
