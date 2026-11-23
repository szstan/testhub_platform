from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.db import DatabaseError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.app_automation.device_pool import (
    DeviceLeaseError,
    acquire_device,
    heartbeat_device_execution,
    reap_expired_device_leases,
    release_device,
    release_device_execution_lease,
)
from apps.app_automation.execution_admission import (
    clear_device_action_pending,
    mark_device_action_pending,
    release_device_if_idle,
)
from apps.app_automation.models import (
    AppDevice,
    AppProject,
    AppTestCase,
    AppTestExecution,
)
from apps.users.models import User


@pytest.fixture
def pending_action_execution(db):
    owner = User.objects.create_user(username='pending-action-owner')
    project = AppProject.objects.create(name='pending action project', owner=owner)
    case = AppTestCase.objects.create(name='pending action case', project=project, created_by=owner)
    device = AppDevice.objects.create(
        device_id='pending-action-device', status='available', health_status='healthy'
    )
    device, token = acquire_device(owner)
    execution = AppTestExecution.objects.create(
        test_case=case,
        device=device,
        user=owner,
        status='running',
        started_at=timezone.now(),
        device_lease_generation=device.lease_generation,
    )
    return SimpleNamespace(owner=owner, project=project, case=case, device=device,
                           token=token, execution=execution)


def test_uncertain_mutation_and_failed_quarantine_keep_lease_fenced(
    pending_action_execution, tmp_path, monkeypatch,
):
    from apps.app_automation import device_actions
    from apps.app_automation.runners import ui_flow_runner as flow_module
    from apps.app_automation.runners.ui_flow_runner import UiFlowRunner
    from apps.app_automation.tasks import settle_undispatched_app_executions

    f = pending_action_execution
    mark_device_action_pending(f.execution.pk)
    runner = UiFlowRunner(image_base_dir=str(tmp_path), execution_id=f.execution.pk)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=object()))
    uncertain = device_actions.DeviceActionUncertainError('mocked uncertain mutation')
    mutate = Mock(side_effect=uncertain)
    monkeypatch.setattr(device_actions, 'send_key', mutate)

    with patch.object(AppDevice, 'save', side_effect=DatabaseError('quarantine write failed')):
        with pytest.raises(device_actions.DeviceActionUncertainError):
            runner.run(
                [
                    {'type': 'key_event', 'key': 'BACK', 'retry_times': 4},
                    {'type': 'set_variable', 'name': 'must_not_run', 'value': True},
                ],
                runtime={'retry_times': 3, 'retry_interval': 0, 'stop_on_error': False},
            )

    mutate.assert_called_once()
    f.execution.refresh_from_db()
    f.device.refresh_from_db()
    assert f.execution.device_action_pending is True
    assert f.device.maintenance_mode is False

    AppTestExecution.objects.filter(pk=f.execution.pk).update(status='error')
    settle_undispatched_app_executions(
        [f.execution.pk],
        'publish cleanup',
        device_id=f.device.pk,
        lease_owner=f.owner,
        lease_generation=f.device.lease_generation,
        release_device_lease=True,
    )
    assert release_device_if_idle(f.device.pk, f.owner, f.device.lease_generation) is False
    assert release_device_execution_lease(f.device.pk, f.owner, f.device.lease_generation) is False
    with pytest.raises(DeviceLeaseError, match='待安全恢复'):
        release_device(f.device.pk, f.owner, f.token)
    f.device.refresh_from_db()
    assert f.device.locked_by_id == f.owner.pk


@pytest.mark.parametrize('legacy', [False, True], ids=['token-lease', 'generation-zero-tokenless'])
def test_expiry_reaper_and_reacquisition_respect_pending_marker(db, legacy):
    owner = User.objects.create_user(username=f'expiry-owner-{legacy}')
    replacement_owner = User.objects.create_user(username=f'expiry-replacement-{legacy}')
    now = timezone.now()
    if legacy:
        device = AppDevice.objects.create(
            device_id='pending-expired-legacy',
            status='locked',
            locked_by=owner,
            locked_at=now - timedelta(minutes=5),
            last_heartbeat_at=now - timedelta(minutes=5),
            max_allocation_time=30,
            lease_generation=0,
            lease_token_hash='',
        )
        generation = None
    else:
        device = AppDevice.objects.create(
            device_id='pending-expired-token', status='available', health_status='healthy'
        )
        device, _token = acquire_device(owner)
        AppDevice.objects.filter(pk=device.pk).update(
            lease_expires_at=now - timedelta(seconds=1)
        )
        device.refresh_from_db()
        generation = device.lease_generation

    execution = AppTestExecution.objects.create(
        device=device,
        user=owner,
        status='error',
        device_lease_generation=generation,
        device_action_pending=True,
        created_at=now,
    )

    assert reap_expired_device_leases() == 0
    with pytest.raises(DeviceLeaseError, match='没有满足能力条件'):
        acquire_device(replacement_owner)
    assert release_device_execution_lease(device.pk, owner, generation) is False
    device.refresh_from_db()
    execution.refresh_from_db()
    assert device.locked_by_id == owner.pk
    assert device.lease_generation == (0 if legacy else generation)
    assert execution.device_action_pending is True


@pytest.mark.django_db
@pytest.mark.parametrize('changed_fence', ['owner', 'generation'])
def test_marker_requires_the_current_lease_owner_and_generation(
    pending_action_execution, changed_fence,
):
    f = pending_action_execution
    if changed_fence == 'owner':
        replacement_owner = User.objects.create_user(username='replacement-action-owner')
        AppDevice.objects.filter(pk=f.device.pk).update(locked_by=replacement_owner)
    else:
        AppDevice.objects.filter(pk=f.device.pk).update(
            lease_generation=f.device.lease_generation + 1
        )

    with pytest.raises(DeviceLeaseError):
        mark_device_action_pending(f.execution.pk)
    assert AppTestExecution.objects.get(pk=f.execution.pk).device_action_pending is False


@pytest.mark.django_db
def test_pending_marker_blocks_batch_claim_and_idle_release(pending_action_execution):
    from apps.app_automation.execution_admission import (
        DeviceExecutionBusy,
        claim_app_execution_batch,
    )

    f = pending_action_execution
    mark_device_action_pending(f.execution.pk)
    AppTestExecution.objects.filter(pk=f.execution.pk).update(status='error')

    with pytest.raises(DeviceExecutionBusy):
        claim_app_execution_batch([f.execution.pk])
    assert release_device_if_idle(f.device.pk, f.owner, f.device.lease_generation) is False


@pytest.mark.django_db
def test_confirmed_quarantine_clears_marker_but_keeps_device_unavailable(
    pending_action_execution, tmp_path,
):
    from apps.app_automation.runners.ui_flow_runner import UiFlowRunner

    f = pending_action_execution
    mark_device_action_pending(f.execution.pk)
    runner = UiFlowRunner(image_base_dir=str(tmp_path), execution_id=f.execution.pk)

    assert runner._quarantine_execution_device() is True
    f.execution.refresh_from_db()
    f.device.refresh_from_db()
    assert f.execution.device_action_pending is False
    assert f.device.maintenance_mode is True

    AppTestExecution.objects.filter(pk=f.execution.pk).update(status='error')
    assert release_device_execution_lease(f.device.pk, f.owner, f.device.lease_generation) is True
    with pytest.raises(DeviceLeaseError, match='没有满足能力条件'):
        acquire_device(f.owner)


@pytest.mark.django_db
def test_heartbeat_continues_renewing_while_action_marker_is_pending(pending_action_execution):
    f = pending_action_execution
    mark_device_action_pending(f.execution.pk)

    heartbeat_device_execution(
        f.device.pk,
        f.owner,
        f.device.lease_generation,
        execution_id=f.execution.pk,
        lease_seconds=120,
    )
    f.device.refresh_from_db()
    assert f.device.last_heartbeat_at is not None
    assert f.device.lease_expires_at > timezone.now()
    assert AppTestExecution.objects.get(pk=f.execution.pk).device_action_pending is True


@pytest.mark.django_db
def test_clean_worker_completion_clears_marker(pending_action_execution):
    f = pending_action_execution
    assert mark_device_action_pending(f.execution.pk) is True
    assert clear_device_action_pending(f.execution.pk) is True
    f.execution.refresh_from_db()
    assert f.execution.device_action_pending is False
    assert release_device_execution_lease(f.device.pk, f.owner, f.device.lease_generation) is True


@pytest.mark.django_db
def test_clean_worker_result_clears_marker_before_execution_settlement(
    pending_action_execution,
):
    from apps.app_automation.tasks import execute_app_test_task

    f = pending_action_execution
    AppTestExecution.objects.filter(pk=f.execution.pk).update(status='pending')
    heartbeat = Mock()
    heartbeat.start.return_value = heartbeat

    def successful_worker(*args, before_device_io, **kwargs):
        before_device_io()
        return {
            'success': True,
            'exit_code': 0,
            'report_path': 'synthetic-report',
            'test_results': {
                'total': 1, 'passed': 1, 'failed': 0, 'skipped': 0, 'unverified': 0,
            },
            'device_io_admitted': True,
            'pytest_process_started': True,
            'pytest_process_completed': True,
        }

    with (
        patch('apps.app_automation.device_pool.DeviceLeaseHeartbeat', return_value=heartbeat),
        patch('apps.app_automation.tasks.send_execution_update'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor,
    ):
        executor.return_value.run_tests.side_effect = successful_worker
        execute_app_test_task.run(
            f.execution.pk,
            lease_generation=f.device.lease_generation,
        )

    f.execution.refresh_from_db()
    assert f.execution.status == 'completed'
    assert f.execution.device_action_pending is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('report_result', 'child_clears_marker', 'expected_marker', 'expected_status'),
    [
        (
            {
                'success': False,
                'exit_code': 1,
                'report_path': 'synthetic-report',
                'test_results': {
                    'total': 1, 'passed': 0, 'failed': 1, 'broken': 0,
                    'skipped': 0, 'unverified': 0,
                },
            },
            True,
            False,
            'completed',
        ),
        (
            {
                'success': False,
                'exit_code': 1,
                'report_path': 'synthetic-report',
                'test_results': {
                    'total': 1, 'passed': 0, 'failed': 1, 'broken': 0,
                    'skipped': 0, 'unverified': 0,
                },
            },
            False,
            True,
            'completed',
        ),
        (
            {
                'success': True,
                'exit_code': 0,
                'report_path': 'synthetic-report',
                'test_results': {
                    'total': 1, 'passed': 0, 'failed': 0, 'broken': 0,
                    'skipped': 1, 'unverified': 1,
                },
                'device_io_admitted': True,
                'pytest_process_started': True,
                'pytest_process_completed': True,
            },
            False,
            False,
            'completed',
        ),
        (
            {
                'success': False,
                'exit_code': 1,
                'report_path': 'synthetic-report',
                'test_results': {
                    'total': 1, 'passed': 0, 'failed': 1, 'broken': 1,
                    'skipped': 0, 'unverified': 0,
                },
            },
            False,
            True,
            'completed',
        ),
        (
            {
                'success': False,
                'exit_code': 0,
                'report_path': None,
                'test_results': {
                    'total': 1, 'passed': 1, 'failed': 0, 'broken': 0,
                    'skipped': 0, 'unverified': 0,
                },
                'device_io_admitted': True,
                'pytest_process_started': True,
                'pytest_process_completed': True,
                'error': 'Allure report generation failed after pytest completed',
            },
            False,
            False,
            'error',
        ),
        (
            {
                'success': False,
                'exit_code': None,
                'test_results': {},
                'device_io_admitted': True,
                'pytest_process_started': False,
                'pytest_process_completed': False,
            },
            False,
            False,
            'error',
        ),
    ],
    ids=[
        'read-only-assertion-child-signal',
        'assertion-report-without-child-signal',
        'unverified-skip',
        'broken-uncertain-action',
        'allure-generation-failure-after-clean-exit',
        'pytest-not-started-after-marker',
    ],
)
def test_terminal_worker_results_clear_only_with_explicit_completion_evidence(
    pending_action_execution, report_result, child_clears_marker, expected_marker, expected_status,
):
    from apps.app_automation.execution_admission import clear_device_action_pending
    from apps.app_automation.tasks import execute_app_test_task

    f = pending_action_execution
    AppTestExecution.objects.filter(pk=f.execution.pk).update(status='pending')
    heartbeat = Mock()
    heartbeat.start.return_value = heartbeat

    def worker_result(*args, before_device_io, **kwargs):
        before_device_io()
        if child_clears_marker:
            clear_device_action_pending(f.execution.pk)
        return report_result

    with (
        patch('apps.app_automation.device_pool.DeviceLeaseHeartbeat', return_value=heartbeat),
        patch('apps.app_automation.tasks.send_execution_update'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor,
    ):
        executor.return_value.run_tests.side_effect = worker_result
        execute_app_test_task.run(
            f.execution.pk,
            lease_generation=f.device.lease_generation,
        )

    f.execution.refresh_from_db()
    assert f.execution.device_action_pending is expected_marker
    assert f.execution.status == expected_status
    if report_result['test_results'].get('failed'):
        assert f.execution.result == 'failed'
    elif expected_status == 'completed':
        assert f.execution.result == 'unverified'


@pytest.mark.django_db
def test_mark_failure_prevents_pytest_child_launch(tmp_path):
    from apps.app_automation.executors.test_executor import AppTestExecutor

    executor = AppTestExecutor(base_path=str(tmp_path))
    fence = Mock(side_effect=DatabaseError('marker persistence failed'))
    with (
        patch.object(executor, '_preflight_worker', return_value={'adb_path': 'adb'}),
        patch.object(executor, '_get_allure_results_dir', return_value=str(tmp_path / 'results')),
        patch('apps.app_automation.executors.test_executor.subprocess.Popen') as popen,
    ):
        result = executor.run_tests(
            1,
            'mock-device',
            'org.example.mock',
            execution_id=1,
            before_device_io=fence,
        )

    fence.assert_called_once()
    popen.assert_not_called()
    assert result['success'] is False


@pytest.mark.django_db(transaction=True)
def test_worker_does_not_spawn_pytest_if_marker_cannot_commit(db, pending_action_execution, tmp_path):
    from apps.app_automation.executors.test_executor import AppTestExecutor

    f = pending_action_execution
    executor = AppTestExecutor(base_path=str(tmp_path))
    with (
        patch.object(executor, '_preflight_worker', return_value={'adb_path': 'adb'}),
        patch.object(
            executor,
            '_get_allure_results_dir',
            return_value=str(tmp_path / 'allure-results'),
        ),
        patch('apps.app_automation.executors.test_executor.subprocess.Popen') as popen,
    ):
        with transaction.atomic():
            result = executor.run_tests(
                1,
                f.device.device_id,
                'org.example.mock',
                execution_id=f.execution.pk,
                before_device_io=lambda: mark_device_action_pending(f.execution.pk),
            )
            popen.assert_not_called()
            assert result['device_io_admitted'] is False
            assert result['pytest_process_started'] is False

    f.execution.refresh_from_db()
    assert f.execution.device_action_pending is False


@pytest.mark.django_db
def test_child_termination_retains_marker_and_does_not_release_lease(
    pending_action_execution,
):
    from apps.app_automation.executors.test_executor import AppTestProcessTerminationError
    from apps.app_automation.tasks import execute_app_test_task

    f = pending_action_execution
    AppTestExecution.objects.filter(pk=f.execution.pk).update(status='pending')
    heartbeat = Mock()
    heartbeat.start.return_value = heartbeat

    def child_terminated(*args, before_device_io, **kwargs):
        before_device_io()
        raise AppTestProcessTerminationError('mocked child termination uncertainty')

    with (
        patch('apps.app_automation.device_pool.DeviceLeaseHeartbeat', return_value=heartbeat),
        patch('apps.app_automation.tasks.send_execution_update'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor,
    ):
        executor.return_value.run_tests.side_effect = child_terminated
        execute_app_test_task.run(
            f.execution.pk,
            lease_generation=f.device.lease_generation,
        )

    f.execution.refresh_from_db()
    f.device.refresh_from_db()
    assert f.execution.device_action_pending is True
    assert f.execution.stop_requested is True
    assert f.execution.status == 'running'
    assert f.device.locked_by_id == f.owner.pk


@pytest.mark.django_db
def test_admin_recovery_requires_confirmation_and_preserves_legacy_lease(db):
    owner = User.objects.create_user(username='legacy-action-owner')
    admin = User.objects.create_user(username='action-admin', is_staff=True)
    locked_at = timezone.now() - timedelta(seconds=2)
    device = AppDevice.objects.create(
        device_id='legacy-action-device',
        status='locked',
        locked_by=owner,
        locked_at=locked_at,
        last_heartbeat_at=locked_at,
        lease_generation=0,
        lease_token_hash='',
        lease_acquired_at=None,
        lease_expires_at=None,
        max_allocation_time=3600,
    )
    execution = AppTestExecution.objects.create(
        device=device,
        user=owner,
        device_lease_generation=None,
        status='running',
        device_action_pending=False,
        created_at=timezone.now(),
    )
    mark_device_action_pending(execution.pk)
    AppTestExecution.objects.filter(pk=execution.pk).update(status='error')
    client = APIClient()
    client.force_authenticate(user=admin)
    url = f'/api/app-automation/devices/{device.pk}/recover-pending-action/'

    missing_confirmation = client.post(url, {}, format='json')
    assert missing_confirmation.status_code == 400
    response = client.post(url, {'confirm_no_active_worker': True}, format='json')

    assert response.status_code == 200
    assert response.data['device_lease_preserved'] is True
    device.refresh_from_db()
    execution.refresh_from_db()
    assert execution.device_action_pending is False
    assert device.maintenance_mode is True
    assert device.lease_generation == 0
    assert device.lease_token_hash == ''
    assert device.locked_by_id == owner.pk
    assert device.locked_at == locked_at
    assert release_device_execution_lease(device.pk, owner, None) is True
    with pytest.raises(DeviceLeaseError, match='没有满足能力条件'):
        acquire_device(admin)


@pytest.mark.django_db
def test_admin_recovery_rejects_running_worker_until_stop_is_requested(pending_action_execution):
    f = pending_action_execution
    mark_device_action_pending(f.execution.pk)
    admin = User.objects.create_user(username='action-recovery-admin', is_staff=True)
    client = APIClient()
    client.force_authenticate(user=admin)
    url = f'/api/app-automation/devices/{f.device.pk}/recover-pending-action/'

    blocked = client.post(url, {'confirm_no_active_worker': True}, format='json')
    assert blocked.status_code == 409
    f.execution.refresh_from_db()
    assert f.execution.device_action_pending is True

    AppTestExecution.objects.filter(pk=f.execution.pk).update(stop_requested=True)
    recovered = client.post(url, {'confirm_no_active_worker': True}, format='json')
    assert recovered.status_code == 200
    f.execution.refresh_from_db()
    f.device.refresh_from_db()
    assert f.execution.status == 'stopped'
    assert f.execution.device_action_pending is False
    assert f.device.maintenance_mode is True
    assert f.device.locked_by_id == f.owner.pk


@pytest.mark.django_db
def test_non_admin_cannot_recover_pending_action(pending_action_execution):
    f = pending_action_execution
    f.execution.device_action_pending = True
    f.execution.save(update_fields=['device_action_pending'])
    client = APIClient()
    client.force_authenticate(user=f.owner)

    response = client.post(
        f'/api/app-automation/devices/{f.device.pk}/recover-pending-action/',
        {'confirm_no_active_worker': True},
        format='json',
    )

    assert response.status_code == 403
    assert AppTestExecution.objects.get(pk=f.execution.pk).device_action_pending is True
