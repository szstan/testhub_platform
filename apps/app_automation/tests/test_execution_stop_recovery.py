from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.app_automation.executors.test_executor import AppTestExecutor, AppTestProcessTerminationError
from apps.app_automation.models import AppDevice, AppProject, AppScheduledTask, AppTestCase, AppTestExecution


@pytest.fixture
def stop_fixture(db):
    owner = get_user_model().objects.create_user(username='execution-stop-owner')
    project = AppProject.objects.create(name='stop project', owner=owner)
    first = AppTestCase.objects.create(name='first', project=project, created_by=owner)
    second = AppTestCase.objects.create(name='second', project=project, created_by=owner)
    device = AppDevice.objects.create(device_id='stop-test-device', status='online')
    client = APIClient()
    client.force_authenticate(user=owner)
    return SimpleNamespace(owner=owner, project=project, first=first, second=second, device=device, client=client)


@pytest.mark.django_db
def test_stop_queued_batch_prevents_late_worker_and_releases_auto_lock(stop_fixture):
    from apps.app_automation.tasks import execute_app_batch_task

    f = stop_fixture
    with patch('apps.app_automation.tasks.execute_app_batch_task.apply_async') as enqueue:
        response = f.client.post('/api/app-automation/test-cases/batch-execute/', {
            'case_ids': [f.first.pk, f.second.pk], 'device_id': f.device.device_id, 'auto_lock': True,
        }, format='json')
    assert response.status_code == 200
    ids = response.data['data']['execution_ids']
    dispatch_id = enqueue.call_args.kwargs['task_id']
    generation = enqueue.call_args.kwargs['kwargs']['lease_generation']
    with (patch('celery.current_app.control.revoke') as revoke,
          patch('apps.app_automation.views.execution_views.send_execution_update'),
          patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor):
        stopped = f.client.post(f'/api/app-automation/executions/{ids[1]}/stop/', {}, format='json')
        execute_app_batch_task.run(dispatch_id, lease_generation=generation, release_device_lease=True)
    assert stopped.status_code == 200 and stopped.data['stopping'] is False
    revoke.assert_called_once_with(dispatch_id, terminate=False)
    executor.return_value.run_tests.assert_not_called()
    assert list(AppTestExecution.objects.filter(pk__in=ids).order_by('pk').values_list('status', flat=True)) == ['stopped', 'stopped']
    f.device.refresh_from_db()
    assert f.device.locked_by_id is None


@pytest.mark.django_db
def test_publish_cleanup_does_not_release_a_new_execution_in_same_generation(stop_fixture):
    from apps.app_automation.tasks import settle_undispatched_app_executions

    f = stop_fixture
    f.device.lock(f.owner)
    generation = f.device.lease_generation
    old = AppTestExecution.objects.create(test_case=f.first, device=f.device, user=f.owner,
                                          status='completed', device_lease_generation=generation)
    current = AppTestExecution.objects.create(test_case=f.second, device=f.device, user=f.owner,
                                              status='pending', device_lease_generation=generation)
    settle_undispatched_app_executions([old.pk], 'broker response lost', device_id=f.device.pk,
                                      lease_owner=f.owner, lease_generation=generation,
                                      release_device_lease=True)
    f.device.refresh_from_db()
    current.refresh_from_db()
    assert f.device.locked_by_id == f.owner.pk
    assert current.status == 'pending'


@pytest.mark.django_db
def test_manual_schedule_is_rejected_while_same_device_has_active_case(stop_fixture):
    f = stop_fixture
    f.device.lock(f.owner)
    AppTestExecution.objects.create(test_case=f.first, device=f.device, user=f.owner,
                                    status='pending', device_lease_generation=f.device.lease_generation)
    schedule = AppScheduledTask.objects.create(
        name='busy schedule', project=f.project, task_type='TEST_CASE', device=f.device,
        test_case=f.second, created_by=f.owner, trigger_type='INTERVAL', interval_seconds=60,
        next_run_time=timezone.now(),
    )
    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async') as enqueue:
        response = f.client.post(f'/api/app-automation/scheduled-tasks/{schedule.pk}/run_now/', {}, format='json')
    assert response.status_code == 409
    enqueue.assert_not_called()
    schedule.refresh_from_db()
    assert schedule.total_runs == 0
    assert AppTestExecution.objects.filter(device=f.device, status='pending').count() == 1


def test_failed_process_termination_does_not_discard_process_handle(tmp_path):
    executor = AppTestExecutor(base_path=str(tmp_path))
    process = Mock()
    process.terminate.side_effect = PermissionError('fixture')
    process.poll.return_value = None
    executor._current_process = process
    with pytest.raises(AppTestProcessTerminationError):
        executor.stop()
    assert executor._current_process is process


@pytest.mark.django_db
def test_unified_cancel_before_worker_settles_pending_and_releases_lease(stop_fixture):
    from apps.app_automation.device_pool import acquire_specific_device
    from apps.executions.source_workers import _app_execute

    f = stop_fixture
    device, token = acquire_specific_device(f.device.pk, f.owner)
    attempts = []

    def cancel_check():
        attempts.append(True)
        if len(attempts) == 2:
            raise RuntimeError('cancelled before worker')

    with (patch('apps.app_automation.device_pool.acquire_device', return_value=(device, token)),
          patch('apps.app_automation.tasks.execute_app_test_task.run') as worker,
          pytest.raises(RuntimeError, match='cancelled before worker')):
        _app_execute(f.first, SimpleNamespace(kind='case'), SimpleNamespace(metadata={'approved': True}),
                     f.owner, cancel_check=cancel_check)
    worker.assert_not_called()
    assert AppTestExecution.objects.get(device=device).status == 'error'
    device.refresh_from_db()
    assert device.locked_by_id is None
