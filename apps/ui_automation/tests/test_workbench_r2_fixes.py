import uuid
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.db.models.query import QuerySet
from django.test import override_settings
from rest_framework.response import Response
from rest_framework.test import APIClient

from apps.ui_automation import tasks
from apps.ui_automation.case_runner import execute_case_execution, persist_case_result
from apps.ui_automation.models import (
    UiProject, TestCase as Case, TestCaseExecution as CaseExecution,
    TestExecution as SuiteExecution, TestSuite as Suite, TestSuiteTestCase as SuiteCase,
)
from apps.ui_automation.views import TestCaseViewSet as CaseView


@pytest.fixture
def setup_case(db):
    user = get_user_model().objects.create_user(username='r2-fix-owner', is_staff=False)
    project = UiProject.objects.create(name='r2-fixes', base_url='https://fixture.example.test', owner=user)
    case = Case.objects.create(project=project, name='first', created_by=user)
    case.steps.create(step_number=1, action_type='wait', wait_time=10)
    suite = Suite.objects.create(project=project, name='suite')
    SuiteCase.objects.create(test_suite=suite, test_case=case)
    client = APIClient()
    client.force_authenticate(user=user)
    return user, project, case, suite, client


def payload(project, request_id=None, **extra):
    return {'async': True, 'project_id': project.pk, 'client_request_id': str(request_id or uuid.uuid4()),
            'engine': 'playwright', 'browser': 'chrome', 'headless': True, **extra}


def accepted_case(setup_case, monkeypatch, **extra):
    _user, project, case, _suite, client = setup_case
    dispatch = Mock()
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', dispatch)
    response = client.post(f'/api/ui-automation/test-cases/{case.pk}/run/', payload(project, **extra), format='json')
    assert response.status_code == 202
    return CaseExecution.objects.get(pk=response.data['execution_id']), dispatch


def test_worker_persists_early_permission_response_instead_of_running_forever(setup_case, monkeypatch):
    with override_settings(UI_SELENIUM_COMPATIBILITY=True):
        execution, _dispatch = accepted_case(setup_case, monkeypatch, engine='selenium')
    no_browser = Mock(side_effect=AssertionError('browser must not start'))
    monkeypatch.setattr('apps.ui_automation.selenium_engine.SeleniumTestEngine.check_execution_environment', no_browser)
    with override_settings(UI_SELENIUM_COMPATIBILITY=False):
        assert execute_case_execution(execution.pk, str(execution.dispatch_claim)) is False
    execution.refresh_from_db()
    assert execution.status == 'error'
    assert execution.failure_stage == 'preflight'
    assert execution.finished_at is not None
    assert 'disabled' in execution.error_message
    no_browser.assert_not_called()


def test_worker_does_not_overwrite_already_persisted_engine_failure(setup_case, monkeypatch):
    execution, _ = accepted_case(setup_case, monkeypatch)
    def run(_view, request, pk=None):
        assert persist_case_result(execution.pk, request._ui_dispatch_claim, status='error', fields={'error_message':'specific engine failure'}, failure_stage='preflight')
        return Response({'error':'generic response'}, status=400)
    monkeypatch.setattr(CaseView, 'run', run)
    assert execute_case_execution(execution.pk, str(execution.dispatch_claim)) is False
    execution.refresh_from_db()
    assert execution.error_message == 'specific engine failure'


def test_worker_response_cannot_overwrite_cancellation(setup_case, monkeypatch):
    execution, _ = accepted_case(setup_case, monkeypatch)
    client = setup_case[-1]
    def run(_view, _request, pk=None):
        assert client.post(f'/api/ui-automation/test-case-executions/{execution.pk}/abort/').status_code == 200
        return Response({'accepted':False, 'failure_stage':'preflight', 'error':'late refusal'}, status=403)
    monkeypatch.setattr(CaseView, 'run', run)
    assert execute_case_execution(execution.pk, str(execution.dispatch_claim)) is False
    execution.refresh_from_db()
    assert execution.status == 'aborted'
    assert execution.error_message == '执行已由用户停止。'


def test_worker_closes_unexpected_success_response_without_terminal_write(setup_case, monkeypatch):
    execution, _ = accepted_case(setup_case, monkeypatch)
    monkeypatch.setattr(CaseView, 'run', lambda *_args, **_kwargs: Response({'success':True}))
    assert execute_case_execution(execution.pk, str(execution.dispatch_claim)) is False
    execution.refresh_from_db()
    assert execution.status == 'error'
    assert execution.finished_at is not None


@pytest.mark.parametrize('proof', ['snapshot', 'legacy_result', 'parent_child'])
def test_rerun_source_uses_historical_case_membership(setup_case, monkeypatch, proof):
    user, project, case, suite, client = setup_case
    original = SuiteExecution.objects.create(project=project, test_suite=suite, executed_by=user, status='RUNNING')
    if proof == 'snapshot':
        SuiteExecution.objects.filter(pk=original.pk).update(request_snapshot={'version':1, 'cases':[{'id':case.pk, 'project_id':project.pk}]})
    elif proof == 'legacy_result':
        SuiteExecution.objects.filter(pk=original.pk).update(result_data={'test_cases':[{'test_case_id':case.pk, 'success':False}]})
    else:
        CaseExecution.objects.create(project=project, test_case=case, test_suite=suite, parent_execution=original, created_by=user, status='failed')
    SuiteExecution.objects.filter(pk=original.pk).update(status='FAILED')
    SuiteCase.objects.filter(test_suite=suite).delete()
    later = Case.objects.create(project=project, name='added later', created_by=user)
    later.steps.create(step_number=1, action_type='wait', wait_time=10)
    SuiteCase.objects.create(test_suite=suite, test_case=later)
    dispatch = Mock()
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', dispatch)
    body = payload(project, rerun_of=original.pk, rerun_kind='suite')
    response = client.post(f'/api/ui-automation/test-cases/{case.pk}/run/', body, format='json')
    assert response.status_code == 202
    denied = client.post(f'/api/ui-automation/test-cases/{later.pk}/run/', payload(project, rerun_of=original.pk, rerun_kind='suite'), format='json')
    assert denied.status_code == 404
    assert dispatch.call_count == 1


def test_snapshot_is_authoritative_over_current_membership(setup_case, monkeypatch):
    user, project, case, suite, client = setup_case
    original = SuiteExecution.objects.create(project=project, test_suite=suite, status='FAILED', request_snapshot={'version':1,'cases':[]}, result_data={'test_cases':[{'test_case_id':case.pk}]})
    dispatch = Mock()
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', dispatch)
    response = client.post(f'/api/ui-automation/test-cases/{case.pk}/run/', payload(project, rerun_of=original.pk, rerun_kind='suite'), format='json')
    assert response.status_code == 404
    dispatch.assert_not_called()


def test_current_membership_is_not_historical_proof(setup_case, monkeypatch):
    _user, project, case, suite, client = setup_case
    original = SuiteExecution.objects.create(project=project, test_suite=suite, status='FAILED', result_data={'test_cases':[]})
    dispatch = Mock()
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', dispatch)
    response = client.post(f'/api/ui-automation/test-cases/{case.pk}/run/', payload(project, rerun_of=original.pk, rerun_kind='suite'), format='json')
    assert response.status_code == 404
    dispatch.assert_not_called()


def test_case_replay_precedes_removed_rerun_source_and_conflict_detection(setup_case, monkeypatch):
    user, project, case, suite, client = setup_case
    original = SuiteExecution.objects.create(project=project, test_suite=suite, status='FAILED', request_snapshot={'version':1,'cases':[{'id':case.pk}]})
    dispatch = Mock()
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', dispatch)
    body = payload(project, rerun_of=original.pk, rerun_kind='suite')
    url = f'/api/ui-automation/test-cases/{case.pk}/run/'
    first = client.post(url, body, format='json')
    assert first.status_code == 202
    original.delete()
    replay = client.post(url, body, format='json')
    assert replay.status_code == 202
    assert replay.data['execution_id'] == first.data['execution_id']
    conflict = client.post(url, {**body,'browser':'firefox'}, format='json')
    assert conflict.status_code == 409
    assert dispatch.call_count == 1


def test_replay_survives_policy_change_but_new_submission_does_not(setup_case, monkeypatch):
    _user, project, case, _suite, client = setup_case
    dispatch = Mock()
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', dispatch)
    url = f'/api/ui-automation/test-cases/{case.pk}/run/'
    body = payload(project, engine='selenium')
    with override_settings(UI_SELENIUM_COMPATIBILITY=True):
        first = client.post(url, body, format='json')
    assert first.status_code == 202
    with override_settings(UI_SELENIUM_COMPATIBILITY=False):
        replay = client.post(url, body, format='json')
        rejected = client.post(url, {**body,'client_request_id':str(uuid.uuid4())}, format='json')
    assert replay.status_code == 202
    assert replay.data['execution_id'] == first.data['execution_id']
    assert rejected.status_code == 403
    assert dispatch.call_count == 1


def test_replay_does_not_bypass_project_access(setup_case, monkeypatch):
    _user, project, case, _suite, client = setup_case
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', Mock())
    body = payload(project)
    url = f'/api/ui-automation/test-cases/{case.pk}/run/'
    first = client.post(url, body, format='json')
    assert first.status_code == 202
    other = get_user_model().objects.create_user(username='not-project-owner')
    project.owner = other
    project.save(update_fields=['owner'])
    assert client.post(url, body, format='json').status_code == 404


def test_parent_assignment_race_does_not_upgrade_a_held_child_lock(setup_case, monkeypatch):
    from apps.ui_automation.views import _abort_case_execution
    user, project, case, suite, _client = setup_case
    parent = SuiteExecution.objects.create(project=project, test_suite=suite, status='RUNNING', dispatch_claim=uuid.uuid4())
    child = CaseExecution.objects.create(project=project, test_case=case, parent_execution=parent, status='running', created_by=user)
    manager = CaseExecution.objects
    original_filter = manager.filter
    no_parent = Mock()
    no_parent.values.return_value.first.return_value = {'parent_execution_id': None}
    def initial_identity(*args, **kwargs):
        return no_parent if not args and kwargs == {'pk': child.pk} else original_filter(*args, **kwargs)
    monkeypatch.setattr(manager, 'filter', initial_identity)
    locks = []
    original_lock = QuerySet.select_for_update
    def trace(queryset, *args, **kwargs):
        locks.append(queryset.model._meta.db_table)
        return original_lock(queryset, *args, **kwargs)
    monkeypatch.setattr(QuerySet, 'select_for_update', trace)
    response = _abort_case_execution(child.pk)
    assert response.status_code == 409
    assert locks == [CaseExecution._meta.db_table]
    parent.refresh_from_db()
    assert parent.status == 'RUNNING'


def test_child_abort_uses_parent_first_lock_order(setup_case, monkeypatch):
    user, project, case, suite, client = setup_case
    parent = SuiteExecution.objects.create(project=project, test_suite=suite, status='RUNNING', dispatch_claim=uuid.uuid4())
    child = CaseExecution.objects.create(project=project, test_case=case, test_suite=suite, parent_execution=parent, status='running', created_by=user)
    locks = []
    original = QuerySet.select_for_update
    def trace(queryset, *args, **kwargs):
        locks.append(queryset.model._meta.db_table)
        return original(queryset,*args,**kwargs)
    monkeypatch.setattr(QuerySet,'select_for_update',trace)
    response = client.post(f'/api/ui-automation/test-case-executions/{child.pk}/abort/')
    assert response.status_code == 200
    assert locks[0] == SuiteExecution._meta.db_table
    parent.refresh_from_db()
    child.refresh_from_db()
    assert parent.status == 'ABORTED'
    assert child.status == 'aborted'
    child.status = 'passed'
    child.save()
    child.refresh_from_db()
    assert child.status == 'aborted'
