from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.app_automation.device_pool import acquire_specific_device
from apps.app_automation.executors.test_executor import AppTestProcessTerminationError
from apps.app_automation.models import AppDevice, AppProject, AppTestCase, AppTestExecution, AppTestSuite, AppTestSuiteCase


@pytest.fixture
def execution_fixture(db):
    owner = get_user_model().objects.create_user(username='execution-admission-owner')
    other = get_user_model().objects.create_user(username='execution-admission-other')
    project = AppProject.objects.create(name='execution admission', owner=owner)
    first = AppTestCase.objects.create(name='first', project=project, created_by=owner)
    second = AppTestCase.objects.create(name='second', project=project, created_by=owner)
    device = AppDevice.objects.create(device_id='execution-fixture-device', status='online')
    suite = AppTestSuite.objects.create(name='suite', project=project, created_by=owner)
    AppTestSuiteCase.objects.create(test_suite=suite, test_case=first, order=1)
    AppTestSuiteCase.objects.create(test_suite=suite, test_case=second, order=2)
    client = APIClient()
    client.force_authenticate(user=owner)
    return SimpleNamespace(owner=owner, other=other, project=project, first=first, second=second,
                           device=device, suite=suite, client=client)


def case_url(case):
    return f'/api/app-automation/test-cases/{case.pk}/execute/'


def suite_url(suite):
    return f'/api/app-automation/test-suites/{suite.pk}/run/'


BATCH_URL = '/api/app-automation/test-cases/batch-execute/'


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite', 'batch'])
def test_expected_user_is_checked_before_auto_lock(execution_fixture, kind):
    f = execution_fixture
    payload = {'device_id': f.device.device_id, 'auto_lock': True, 'expected_user_id': f.other.pk}
    if kind == 'case':
        url = case_url(f.first)
    elif kind == 'suite':
        url = suite_url(f.suite)
    else:
        url = BATCH_URL
        payload['case_ids'] = [f.first.pk]

    response = f.client.post(url, payload, format='json')

    assert response.status_code == 403
    f.device.refresh_from_db()
    assert f.device.locked_by_id is None
    assert not AppTestExecution.objects.filter(device=f.device).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite', 'batch'])
def test_auto_lock_reserves_device_against_other_entry_points(execution_fixture, kind):
    f = execution_fixture
    payload = {'device_id': f.device.device_id, 'auto_lock': True, 'expected_user_id': f.owner.pk}
    if kind == 'case':
        url, task = case_url(f.first), 'execute_app_test_task'
    elif kind == 'suite':
        url, task = suite_url(f.suite), 'execute_app_suite_task'
    else:
        url, task = BATCH_URL, 'execute_app_batch_task'
        payload['case_ids'] = [f.second.pk, f.first.pk]

    with patch(f'apps.app_automation.tasks.{task}.apply_async') as enqueue:
        response = f.client.post(url, payload, format='json')
    assert response.status_code == 200, response.data
    enqueue.assert_called_once()
    f.device.refresh_from_db()
    assert f.device.locked_by_id == f.owner.pk
    assert response.data['device']['lease_requires_token'] is False

    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async') as competing:
        rejected = f.client.post(case_url(f.second), payload, format='json')
    assert rejected.status_code == 409
    competing.assert_not_called()
    assert AppTestExecution.objects.filter(device=f.device, status='pending').count() == (2 if kind != 'case' else 1)
    if kind == 'batch':
        ids = response.data['data']['execution_ids']
        assert list(AppTestExecution.objects.filter(pk__in=ids).order_by('pk').values_list('test_case_id', flat=True)) == [f.second.pk, f.first.pk]
        assert len(set(AppTestExecution.objects.filter(pk__in=ids).values_list('task_id', flat=True))) == 1


@pytest.mark.django_db
def test_rejected_batch_does_not_take_a_lock(execution_fixture):
    f = execution_fixture
    private_project = AppProject.objects.create(name='private', owner=f.other)
    private_case = AppTestCase.objects.create(name='private', project=private_project, created_by=f.other)
    with patch('apps.app_automation.tasks.execute_app_batch_task.apply_async') as enqueue:
        response = f.client.post(BATCH_URL, {
            'device_id': f.device.device_id, 'auto_lock': True,
            'case_ids': [f.first.pk, private_case.pk],
        }, format='json')
    assert response.status_code == 403
    enqueue.assert_not_called()
    f.device.refresh_from_db()
    assert f.device.locked_by_id is None
    assert not AppTestExecution.objects.filter(device=f.device).exists()


@pytest.mark.django_db
def test_token_backed_lease_is_marked_and_still_requires_token(execution_fixture):
    f = execution_fixture
    device, token = acquire_specific_device(f.device.pk, f.owner)
    detail = f.client.get(f'/api/app-automation/devices/{device.pk}/')
    assert detail.data['lease_requires_token'] is True
    denied = f.client.post(case_url(f.first), {'device_id': device.device_id, 'auto_lock': True}, format='json')
    assert denied.status_code == 403
    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async') as enqueue:
        accepted = f.client.post(case_url(f.first), {
            'device_id': device.device_id, 'lease_token': token,
        }, format='json')
    assert accepted.status_code == 200
    enqueue.assert_called_once()
    assert token not in str(enqueue.call_args)


@pytest.mark.django_db
def test_publish_failure_cleans_only_unstarted_auto_lock(execution_fixture):
    f = execution_fixture
    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async', side_effect=RuntimeError('password=fixture-secret')):
        response = f.client.post(case_url(f.first), {'device_id': f.device.device_id, 'auto_lock': True}, format='json')
    assert response.status_code == 500
    assert 'fixture-secret' not in str(response.data)
    f.device.refresh_from_db()
    assert f.device.locked_by_id is None
    execution = AppTestExecution.objects.get(device=f.device)
    assert execution.status == 'error'
    assert 'fixture-secret' not in execution.error_message


@pytest.mark.django_db
def test_ambiguous_publish_does_not_release_a_worker_that_already_claimed(execution_fixture):
    from apps.app_automation.execution_admission import claim_app_execution_batch

    f = execution_fixture
    def accepted_then_failed(*args, **kwargs):
        execution_id = kwargs['args'][0]
        assert claim_app_execution_batch([execution_id])
        raise RuntimeError('broker response lost')

    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async', side_effect=accepted_then_failed):
        response = f.client.post(case_url(f.first), {'device_id': f.device.device_id, 'auto_lock': True}, format='json')
    assert response.status_code == 500
    f.device.refresh_from_db()
    assert f.device.locked_by_id == f.owner.pk
    assert AppTestExecution.objects.get(device=f.device).status == 'running'


@pytest.mark.django_db
def test_ordered_batch_and_duplicate_delivery(execution_fixture):
    from apps.app_automation.tasks import execute_app_batch_task

    f = execution_fixture
    with patch('apps.app_automation.tasks.execute_app_batch_task.apply_async') as enqueue:
        response = f.client.post(BATCH_URL, {
            'device_id': f.device.device_id, 'auto_lock': True,
            'case_ids': [f.second.pk, f.first.pk],
        }, format='json')
    assert response.status_code == 200
    dispatch_id = enqueue.call_args.kwargs['task_id']
    generation = enqueue.call_args.kwargs['kwargs']['lease_generation']
    order = []
    with (patch('apps.app_automation.device_pool.Thread'),
          patch('apps.app_automation.tasks.send_execution_update'),
          patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor):
        def run_tests(*args, **kwargs):
            order.append(kwargs['test_case_id'])
            kwargs['cancel_check']()
            return {'test_results': {'total': 1, 'passed': 1, 'failed': 0}}
        executor.return_value.run_tests.side_effect = run_tests
        execute_app_batch_task.run(dispatch_id, lease_generation=generation)
        execute_app_batch_task.run(dispatch_id, lease_generation=generation)
    assert order == [f.second.pk, f.first.pk]
    assert list(AppTestExecution.objects.filter(task_id=dispatch_id).order_by('pk').values_list('status', flat=True)) == ['completed', 'completed']


@pytest.mark.django_db
@pytest.mark.parametrize('preexisting_lock', [False, True])
def test_running_stop_keeps_device_busy_until_worker_stops(execution_fixture, preexisting_lock):
    from apps.app_automation.tasks import execute_app_test_task

    f = execution_fixture
    if preexisting_lock:
        f.device.lock(f.owner)
    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async') as enqueue:
        response = f.client.post(case_url(f.first), {'device_id': f.device.device_id, 'auto_lock': True}, format='json')
    assert response.status_code == 200
    execution_id = response.data['execution']['id']
    generation = enqueue.call_args.kwargs['kwargs']['lease_generation']
    observations = []
    with (patch('apps.app_automation.device_pool.Thread'),
          patch('apps.app_automation.tasks.send_execution_update'),
          patch('apps.app_automation.views.execution_views.send_execution_update'),
          patch('celery.current_app.control.revoke'),
          patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor):
        def run_tests(*args, **kwargs):
            stopped = f.client.post(f'/api/app-automation/executions/{execution_id}/stop/', {}, format='json')
            observations.append((stopped.status_code, stopped.data['stopping']))
            blocked = f.client.post(case_url(f.second), {'device_id': f.device.device_id, 'auto_lock': True}, format='json')
            observations.append(blocked.status_code)
            kwargs['cancel_check']()
        executor.return_value.run_tests.side_effect = run_tests
        execute_app_test_task.run(execution_id, lease_generation=generation,
                                  release_device_lease=not preexisting_lock)
    assert observations == [(200, True), 409]
    execution = AppTestExecution.objects.get(pk=execution_id)
    assert execution.status == 'stopped'
    f.device.refresh_from_db()
    assert f.device.locked_by_id == (f.owner.pk if preexisting_lock else None)


@pytest.mark.django_db
def test_unconfirmed_process_exit_keeps_device_busy(execution_fixture):
    from apps.app_automation.tasks import execute_app_test_task

    f = execution_fixture
    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async') as enqueue:
        response = f.client.post(case_url(f.first), {'device_id': f.device.device_id, 'auto_lock': True}, format='json')
    execution_id = response.data['execution']['id']
    generation = enqueue.call_args.kwargs['kwargs']['lease_generation']
    with (patch('apps.app_automation.device_pool.Thread'),
          patch('apps.app_automation.tasks.send_execution_update'),
          patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor):
        executor.return_value.run_tests.side_effect = AppTestProcessTerminationError('process still running')
        execute_app_test_task.run(execution_id, lease_generation=generation, release_device_lease=True)
    execution = AppTestExecution.objects.get(pk=execution_id)
    f.device.refresh_from_db()
    assert execution.status == 'running'
    assert execution.stop_requested
    assert f.device.locked_by_id == f.owner.pk
    blocked = f.client.post(case_url(f.second), {'device_id': f.device.device_id, 'auto_lock': True}, format='json')
    assert blocked.status_code == 409
