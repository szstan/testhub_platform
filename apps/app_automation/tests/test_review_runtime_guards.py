"""Regression coverage for APP ownership, result accounting and safe timeout cleanup."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIClient

from apps.app_automation import tasks
from apps.app_automation.device_pool import acquire_device
from apps.app_automation.executors import test_executor as executor_module
from apps.app_automation.executors.test_executor import (
    AppTestExecutor,
    AppTestProcessTerminationError,
    AppTestTimeoutError,
)
from apps.app_automation.models import (
    AppDevice, AppProject, AppTestCase, AppTestExecution, AppTestSuite,
)
from apps.users.models import User


@pytest.mark.django_db
def test_project_creation_binds_owner_and_admin_can_manage_unrelated_project():
    owner = User.objects.create_user(username='app-project-create-owner')
    stranger = User.objects.create_user(username='app-project-create-stranger')
    admin = User.objects.create_user(username='app-project-create-admin', is_staff=True)
    client = APIClient()
    client.force_authenticate(owner)
    response = client.post('/api/app-automation/projects/', {
        'name': 'Created project', 'owner': stranger.pk,
    }, format='json')
    assert response.status_code == 201, response.data
    project = AppProject.objects.get(name='Created project')
    assert project.owner_id == owner.pk
    assert client.get(f'/api/app-automation/projects/{project.pk}/').status_code == 200
    client.force_authenticate(stranger)
    assert client.patch(f'/api/app-automation/projects/{project.pk}/', {
        'name': 'Forbidden edit',
    }).status_code == 404
    client.force_authenticate(admin)
    assert client.patch(f'/api/app-automation/projects/{project.pk}/', {
        'name': 'Admin edit',
    }).status_code == 200


@pytest.mark.django_db
def test_dashboard_counts_current_and_legacy_results_without_changing_denominator():
    user = User.objects.create_user(username='app-dashboard-count-owner')
    outsider = User.objects.create_user(username='app-dashboard-count-outsider')
    for status, result in [
        ('success', None), ('failed', None), ('completed', 'passed'),
        ('completed', 'failed'), ('error', None), ('completed', 'unverified'),
        ('completed', 'skipped'), ('stopped', None), ('pending', None),
    ]:
        AppTestExecution.objects.create(user=user, status=status, result=result)
    AppTestExecution.objects.create(user=outsider, status='completed', result='passed')
    client = APIClient()
    client.force_authenticate(user)
    response = client.get('/api/app-automation/dashboard/statistics/')
    assert response.status_code == 200, response.data
    assert response.data['data']['executions'] == {
        'total': 9, 'success': 2, 'failed': 3, 'pass_rate': 22.22,
    }


@pytest.fixture
def timeout_execution(db):
    user = User.objects.create_user(username='app-timeout-cleanup-owner')
    project = AppProject.objects.create(name='APP timeout project', owner=user)
    AppDevice.objects.create(device_id='app-timeout-device', status='available')
    device, _token = acquire_device(user)
    case = AppTestCase.objects.create(name='Timeout case', project=project, timeout=1)
    execution = AppTestExecution.objects.create(
        test_case=case, user=user, device=device,
        device_lease_generation=device.lease_generation,
        release_device_lease_on_stop=True,
    )
    return user, device, execution


@pytest.mark.django_db
@pytest.mark.parametrize('suite_mode', [False, True])
@pytest.mark.parametrize('cleanup_confirmed', [False, True])
def test_timeout_settles_rows_and_releases_only_confirmed_device_cleanup(
    timeout_execution, suite_mode, cleanup_confirmed,
):
    user, device, execution = timeout_execution
    suite = None
    if suite_mode:
        suite = AppTestSuite.objects.create(name='Timeout suite', project=execution.test_case.project)
        execution.test_suite = suite
        execution.save(update_fields=['test_suite'])

    executor = Mock(_device_io_stopped_safely=cleanup_confirmed)

    def timeout_worker(*, before_device_io, **kwargs):
        assert kwargs['deadline'] > 0
        before_device_io()
        raise AppTestTimeoutError('APP test case execution deadline exceeded')

    executor.run_tests.side_effect = timeout_worker
    with (
        patch('apps.app_automation.device_pool.DeviceLeaseHeartbeat'),
        patch('apps.app_automation.tasks.send_execution_update'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor', return_value=executor),
    ):
        if suite_mode:
            tasks.execute_app_suite_task.run(
                suite.pk, [execution.pk], lease_generation=device.lease_generation,
            )
        else:
            tasks.execute_app_test_task.run(
                execution.pk, lease_generation=device.lease_generation,
            )

    execution.refresh_from_db()
    device.refresh_from_db()
    assert execution.status == 'error'
    assert execution.result is None
    assert execution.finished_at is not None
    assert execution.device_action_pending is (not cleanup_confirmed)
    assert device.locked_by_id == (None if cleanup_confirmed else user.pk)
    executor._generate_allure_report.assert_not_called()


def test_executor_cannot_discard_unreaped_child(tmp_path):
    executor = AppTestExecutor(base_path=str(tmp_path))
    process = Mock(pid=None)
    process.poll.return_value = None
    process.wait.side_effect = executor_module.subprocess.TimeoutExpired('pytest', 5)
    executor._current_process = process
    with pytest.raises(AppTestProcessTerminationError):
        executor.stop()
    assert executor._current_process is process
    assert executor._last_process_exit_confirmed is False
    assert executor._device_io_stopped_safely is False
    process.terminate.assert_called_once()
    process.kill.assert_called_once()


def test_executor_uses_remaining_deadline_for_report_subprocess(tmp_path, monkeypatch):
    executor = AppTestExecutor(base_path=str(tmp_path))
    executor._case_deadline = 15.0
    monkeypatch.setattr(executor_module, 'time', SimpleNamespace(monotonic=lambda: 12.0))
    monkeypatch.setattr(executor, '_find_allure_command', lambda: 'synthetic-allure')
    monkeypatch.setattr(executor, '_get_allure_results_dir', lambda _id: str(tmp_path / 'results'))
    monkeypatch.setattr(executor, '_get_allure_report_dir', lambda _id: str(tmp_path / 'report'))
    with patch.object(executor_module.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
        assert executor._generate_allure_report(123) == str(tmp_path / 'report')
    assert run.call_args.kwargs['timeout'] == 3.0


def test_expired_deadline_never_starts_child(tmp_path):
    executor = AppTestExecutor(base_path=str(tmp_path))
    with (
        patch.object(executor, '_preflight_worker') as preflight,
        patch.object(executor_module.subprocess, 'Popen') as spawn,
        pytest.raises(AppTestTimeoutError),
    ):
        executor.run_tests(1, 'synthetic-device', 'org.example.app', deadline=0.0)
    preflight.assert_not_called()
    spawn.assert_not_called()


@pytest.mark.parametrize('returncode', [0, 1])
def test_windows_tree_termination_requires_success_receipt(monkeypatch, returncode):
    process = Mock(pid=12345)
    monkeypatch.setattr(executor_module, 'os', SimpleNamespace(name='nt'))
    with (
        patch.object(executor_module.shutil, 'which', return_value='synthetic-taskkill'),
        patch.object(executor_module.subprocess, 'run', return_value=SimpleNamespace(returncode=returncode)) as run,
    ):
        if returncode:
            with pytest.raises(AppTestProcessTerminationError):
                executor_module._signal_process_tree(process)
        else:
            executor_module._signal_process_tree(process)
    assert run.call_args.args[0] == ['synthetic-taskkill', '/PID', '12345', '/T', '/F']
    process.terminate.assert_not_called()


def test_preflight_timeout_at_case_deadline_is_not_relabelled_worker_failure(tmp_path, monkeypatch):
    executor = AppTestExecutor(base_path=str(tmp_path))
    clock = {'now': 12.0}
    executor._case_deadline = 15.0
    monkeypatch.setattr(executor_module, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    monkeypatch.setattr(executor_module.importlib, 'import_module', Mock())
    monkeypatch.setattr(executor_module.shutil, 'which', lambda _command: 'synthetic-adb')

    def preflight_command(*args, **kwargs):
        assert kwargs['timeout'] == 3.0
        clock['now'] = 15.0
        raise executor_module.subprocess.TimeoutExpired('adb', 3.0)

    with (
        patch.object(executor_module.subprocess, 'run', side_effect=preflight_command),
        pytest.raises(AppTestTimeoutError),
    ):
        executor._preflight_worker()
