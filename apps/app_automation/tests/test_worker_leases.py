"""APP worker boundaries use persisted generations, never a transported bearer token."""

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.app_automation.device_pool import (
    DeviceLeaseError, acquire_device, assert_device_execution_lease,
    heartbeat_device, heartbeat_device_execution, release_device_execution_lease,
)
from apps.app_automation.models import AppDevice, AppProject, AppTestCase, AppTestExecution, AppTestSuite
from apps.users.models import User


@pytest.fixture
def app_lease(db):
    user = User.objects.create_user(username='app-worker-lease-owner')
    project = AppProject.objects.create(name='worker lease', owner=user)
    device = AppDevice.objects.create(device_id='worker-lease-device', status='available')
    device, token = acquire_device(user)
    case = AppTestCase.objects.create(name='lease case', project=project, created_by=user)
    execution = AppTestExecution.objects.create(
        test_case=case, device=device, user=user, device_lease_generation=device.lease_generation,
    )
    return user, device, token, case, execution


@pytest.mark.django_db
def test_allocator_and_manual_lock_reject_live_lease_even_with_status_drift(app_lease):
    user, device, token, *_ = app_lease
    AppDevice.objects.filter(pk=device.pk).update(status='online')
    with pytest.raises(DeviceLeaseError):
        acquire_device(user)
    with pytest.raises(DeviceLeaseError):
        AppDevice.objects.get(pk=device.pk).lock(user)
    assert_device_execution_lease(device.pk, user, device.lease_generation)


@pytest.mark.django_db
def test_token_heartbeat_cannot_revive_expired_lease(app_lease):
    user, device, token, *_ = app_lease
    AppDevice.objects.filter(pk=device.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(DeviceLeaseError):
        heartbeat_device(device.pk, user, token)
    with pytest.raises(DeviceLeaseError):
        heartbeat_device_execution(device.pk, user, device.lease_generation)


@pytest.mark.django_db
def test_stale_model_unlock_cannot_release_same_owner_replacement(app_lease):
    user, stale, _, *_ = app_lease
    assert release_device_execution_lease(stale.pk, user, stale.lease_generation)
    replacement, _ = acquire_device(user)
    assert not stale.unlock()
    replacement.refresh_from_db()
    assert replacement.locked_by_id == user.pk
    assert replacement.lease_generation > stale.lease_generation


@pytest.mark.django_db
def test_new_manual_lock_increments_generation_from_database(app_lease):
    user, stale, _, *_ = app_lease
    assert release_device_execution_lease(stale.pk, user, stale.lease_generation)
    stale.lock(user)
    assert stale.lease_generation == 2
    assert not stale.lease_token_hash
    assert_device_execution_lease(stale.pk, user, 2)
    with pytest.raises(DeviceLeaseError):
        assert_device_execution_lease(stale.pk, user, None)


@pytest.mark.django_db
def test_legacy_generation_zero_is_valid_but_missing_generation_needs_proof(app_lease):
    user, device, _, _, execution = app_lease
    AppDevice.objects.filter(pk=device.pk).update(
        lease_generation=0, lease_token_hash='', lease_acquired_at=None, lease_expires_at=None,
        locked_at=execution.created_at - timedelta(seconds=1),
    )
    AppTestExecution.objects.filter(pk=execution.pk).update(device_lease_generation=None)
    assert_device_execution_lease(device.pk, user, None, execution_id=execution.pk)
    assert_device_execution_lease(device.pk, user, 0, execution_id=execution.pk)
    with pytest.raises(DeviceLeaseError):
        assert_device_execution_lease(device.pk, user, None)
    AppDevice.objects.filter(pk=device.pk).update(locked_at=execution.created_at + timedelta(seconds=1))
    with pytest.raises(DeviceLeaseError):
        assert_device_execution_lease(device.pk, user, None, execution_id=execution.pk)


@pytest.mark.django_db
def test_reaper_respects_renewed_token_lease_not_original_lock_age(app_lease):
    from apps.app_automation.tasks import check_and_release_expired_devices

    user, device, _, *_ = app_lease
    AppDevice.objects.filter(pk=device.pk).update(
        locked_at=timezone.now() - timedelta(days=2),
        lease_expires_at=timezone.now() + timedelta(seconds=60),
    )
    check_and_release_expired_devices.run()
    device.refresh_from_db()
    assert device.locked_by_id == user.pk


@pytest.mark.django_db
def test_suite_worker_rejects_same_owner_stale_generation_without_runner(app_lease):
    from apps.app_automation.tasks import execute_app_suite_task

    user, device, _, case, execution = app_lease
    suite = AppTestSuite.objects.create(name='lease suite', project=case.project)
    execution.test_suite = suite
    execution.save(update_fields=['test_suite'])
    stale_generation = device.lease_generation
    release_device_execution_lease(device.pk, user, stale_generation)
    replacement, _ = acquire_device(user)
    with (
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as runner,
        patch('apps.app_automation.tasks.send_execution_update'),
    ):
        execute_app_suite_task.run(suite.pk, [execution.pk], lease_generation=stale_generation)
    runner.assert_not_called()
    replacement.refresh_from_db()
    execution.refresh_from_db()
    assert replacement.locked_by_id == user.pk
    assert replacement.lease_generation == stale_generation + 1
    assert execution.status == 'error'
    assert execution.result is None


@pytest.mark.django_db
def test_suite_worker_stops_before_second_case_and_settles_pending_rows(app_lease):
    from apps.app_automation.tasks import execute_app_suite_task

    user, device, _, case, first = app_lease
    suite = AppTestSuite.objects.create(name='cancel suite', project=case.project)
    first.test_suite = suite
    first.save(update_fields=['test_suite'])
    second = AppTestExecution.objects.create(
        test_suite=suite, test_case=case, device=device, user=user,
        device_lease_generation=device.lease_generation,
    )

    def run_tests(*args, cancel_check, **kwargs):
        cancel_check()
        AppDevice.objects.filter(pk=device.pk).update(lease_generation=device.lease_generation + 1)
        cancel_check()
        return {'test_results': {'total': 1, 'passed': 1}}

    with (
        patch('apps.app_automation.device_pool.Thread'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor_class,
        patch('apps.app_automation.tasks.send_execution_update'),
    ):
        executor_class.return_value.run_tests.side_effect = run_tests
        execute_app_suite_task.run(suite.pk, [first.pk, second.pk], lease_generation=device.lease_generation)
    assert executor_class.return_value.run_tests.call_count == 1
    for execution in (first, second):
        execution.refresh_from_db()
        assert execution.status == 'error'
        assert execution.result is None
        assert execution.finished_at is not None


@pytest.mark.django_db
def test_suite_enqueue_persists_generation_without_sending_token(app_lease):
    from rest_framework.test import APIClient
    from apps.app_automation.models import AppTestSuiteCase

    user, device, token, case, _ = app_lease
    suite = AppTestSuite.objects.create(name='queued lease suite', project=case.project)
    AppTestSuiteCase.objects.create(test_suite=suite, test_case=case)
    client = APIClient()
    client.force_authenticate(user=user)
    with patch('apps.app_automation.tasks.execute_app_suite_task.delay', return_value=Mock(id='queued')) as delay:
        response = client.post(
            f'/api/app-automation/test-suites/{suite.pk}/run/',
            {'device_id': device.device_id, 'lease_token': token}, format='json',
        )
    assert response.status_code == 200
    generation = delay.call_args.kwargs['lease_generation']
    assert generation == device.lease_generation
    assert token not in str(delay.call_args)
    created = AppTestExecution.objects.get(pk=response.data['data']['execution_ids'][0])
    assert created.device_lease_generation == generation


@pytest.mark.django_db
def test_legacy_heartbeat_preserves_binding_and_extends_only_live_lock(app_lease):
    user, device, _, _, execution = app_lease
    original_lock = execution.created_at - timedelta(seconds=1)
    AppDevice.objects.filter(pk=device.pk).update(
        lease_generation=0, lease_token_hash='', lease_acquired_at=None,
        lease_expires_at=None, last_heartbeat_at=None, locked_at=original_lock,
    )
    AppTestExecution.objects.filter(pk=execution.pk).update(device_lease_generation=None)
    heartbeat_device_execution(device.pk, user, None, execution_id=execution.pk)
    heartbeat_device_execution(device.pk, user, None, execution_id=execution.pk)
    device.refresh_from_db()
    assert device.locked_at == original_lock
    assert device.last_heartbeat_at > original_lock
    assert_device_execution_lease(device.pk, user, None, execution_id=execution.pk)
    AppDevice.objects.filter(pk=device.pk).update(
        last_heartbeat_at=timezone.now() - timedelta(days=2),
        locked_at=timezone.now() - timedelta(days=2),
    )
    with pytest.raises(DeviceLeaseError):
        heartbeat_device_execution(device.pk, user, None, execution_id=execution.pk)


@pytest.mark.django_db(transaction=True)
def test_manual_schedule_checks_lease_under_transaction_and_enqueues_generation(app_lease):
    from django.db import connection
    from rest_framework.test import APIClient
    from apps.app_automation.device_pool import validate_device_lease
    from apps.app_automation.models import AppScheduledTask

    user, device, token, case, _ = app_lease
    schedule = AppScheduledTask.objects.create(
        name='manual run lease', project=case.project, task_type='TEST_CASE',
        device=device, test_case=case, created_by=user, trigger_type='INTERVAL', interval_seconds=60,
    )
    client = APIClient()
    client.force_authenticate(user=user)

    def validate(*args):
        assert connection.in_atomic_block
        return validate_device_lease(*args)

    with (
        patch('apps.app_automation.views.scheduled_task_views.validate_device_lease', side_effect=validate),
        patch('apps.app_automation.tasks.execute_app_test_task.delay', return_value=Mock(id='manual')) as delay,
    ):
        response = client.post(
            f'/api/app-automation/scheduled-tasks/{schedule.pk}/run_now/',
            {'lease_token': token}, format='json',
        )
    assert response.status_code == 200
    assert delay.call_args.kwargs['lease_generation'] == device.lease_generation
    assert delay.call_args.kwargs['release_device_lease'] is False
    assert token not in str(delay.call_args)
