import subprocess
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.app_automation.device_pool import (
    DeviceLeaseError,
    acquire_device,
    heartbeat_device,
    reap_expired_device_leases,
    release_device,
    release_device_execution_lease,
)
from apps.app_automation.execution_admission import (
    DeviceExecutionBusy,
    assert_device_execution_idle,
    release_device_if_idle,
)
from apps.app_automation.models import AppDevice
from apps.app_automation.views.recorder_views import RecordInteractionView
from apps.users.models import User


def _post_interaction(user, device, *, token=''):
    request = APIRequestFactory().post(
        f'/api/app-automation/devices/{device.pk}/record-interaction/',
        {
            'action': 'tap',
            'x': 10,
            'y': 20,
            'lease_token': token,
            'expected_user_id': user.pk,
        },
        format='json',
    )
    force_authenticate(request, user=user)
    return RecordInteractionView.as_view()(request, device_id=device.pk)


@pytest.fixture
def recorder_lease(db):
    user = User.objects.create_user(username='recorder-fence-owner')
    device = AppDevice.objects.create(
        device_id='recorder-fence-device', status='online', health_status='healthy'
    )
    device.lock(user)
    return user, device


@pytest.mark.django_db(transaction=True)
def test_committed_device_marker_brackets_recorder_adb_io(recorder_lease):
    user, device = recorder_lease
    observed = {}

    def adb_call(*args, **kwargs):
        observed['in_atomic_block'] = connection.in_atomic_block
        observed['marker'] = AppDevice.objects.get(pk=device.pk).device_action_pending

    with (
        patch('apps.app_automation.views.recorder_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.recorder_views.subprocess.run', side_effect=adb_call),
    ):
        response = _post_interaction(user, device)

    assert response.status_code == 200
    assert observed == {'in_atomic_block': False, 'marker': True}
    device.refresh_from_db()
    assert device.device_action_pending is False


@pytest.mark.django_db(transaction=True)
def test_outer_transaction_rejects_recorder_action_before_adb(recorder_lease):
    user, device = recorder_lease
    with (
        patch('apps.app_automation.views.recorder_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.recorder_views.subprocess.run') as adb,
    ):
        with transaction.atomic():
            response = _post_interaction(user, device)
            assert response.status_code == 500
            adb.assert_not_called()

    device.refresh_from_db()
    assert device.device_action_pending is False


@pytest.mark.django_db(transaction=True)
def test_marker_write_failure_prevents_recorder_adb_io(recorder_lease):
    user, device = recorder_lease
    original_save = AppDevice.save

    def fail_marker(instance, *args, **kwargs):
        if instance.device_action_pending:
            raise DatabaseError('simulated marker write failure')
        return original_save(instance, *args, **kwargs)

    with (
        patch.object(AppDevice, 'save', fail_marker),
        patch('apps.app_automation.views.recorder_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.recorder_views.subprocess.run') as adb,
    ):
        response = _post_interaction(user, device)

    assert response.status_code == 500
    adb.assert_not_called()
    device.refresh_from_db()
    assert device.device_action_pending is False


@pytest.mark.django_db(transaction=True)
def test_process_loss_retains_committed_marker_and_blocks_reaper_and_reuse(db):
    owner = User.objects.create_user(username='recorder-process-loss-owner')
    other = User.objects.create_user(username='recorder-process-loss-other')
    device = AppDevice.objects.create(
        device_id='recorder-process-loss-device', status='available', health_status='healthy'
    )
    device, token = acquire_device(owner, lease_seconds=90)

    def process_died(*args, **kwargs):
        current = AppDevice.objects.get(pk=device.pk)
        assert current.device_action_pending is True
        assert connection.in_atomic_block is False
        AppDevice.objects.filter(pk=device.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        raise SystemExit('simulated HTTP process death')

    with (
        patch('apps.app_automation.views.recorder_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.recorder_views.subprocess.run', side_effect=process_died),
        pytest.raises(SystemExit, match='simulated HTTP process death'),
    ):
        _post_interaction(owner, device, token=token)

    assert reap_expired_device_leases() == 0
    assert release_device_execution_lease(device.pk, owner, device.lease_generation) is False
    assert release_device_if_idle(device.pk, owner, device.lease_generation) is False
    with pytest.raises(DeviceLeaseError):
        acquire_device(other)
    device.refresh_from_db()
    assert device.locked_by_id == owner.pk
    assert device.device_action_pending is True


@pytest.mark.django_db(transaction=True)
def test_failed_quarantine_write_retains_marker_and_blocks_reuse(recorder_lease):
    user, device = recorder_lease
    original_save = AppDevice.save

    def fail_quarantine(instance, *args, **kwargs):
        if instance.maintenance_mode:
            raise DatabaseError('simulated quarantine write failure')
        return original_save(instance, *args, **kwargs)

    with (
        patch.object(AppDevice, 'save', fail_quarantine),
        patch('apps.app_automation.views.recorder_views.get_adb_path', return_value='/fake/adb'),
        patch(
            'apps.app_automation.views.recorder_views.subprocess.run',
            side_effect=subprocess.TimeoutExpired('/fake/adb', 8),
        ),
    ):
        response = _post_interaction(user, device)

    assert response.status_code == 504
    device.refresh_from_db()
    assert device.maintenance_mode is False
    assert device.device_action_pending is True

    AppDevice.objects.filter(pk=device.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert reap_expired_device_leases() == 0
    assert release_device_execution_lease(device.pk, user, device.lease_generation) is False
    assert release_device_if_idle(device.pk, user, device.lease_generation) is False


@pytest.mark.django_db(transaction=True)
def test_successful_adb_with_failed_end_write_retains_marker(recorder_lease):
    user, device = recorder_lease
    original_save = AppDevice.save

    def fail_clear(instance, *args, **kwargs):
        if not instance.device_action_pending:
            raise DatabaseError('simulated end marker write failure')
        return original_save(instance, *args, **kwargs)

    with (
        patch.object(AppDevice, 'save', fail_clear),
        patch('apps.app_automation.views.recorder_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.recorder_views.subprocess.run'),
    ):
        response = _post_interaction(user, device)

    assert response.status_code == 503
    device.refresh_from_db()
    assert device.device_action_pending is True
    assert device.maintenance_mode is False


@pytest.mark.django_db(transaction=True)
def test_admin_recovery_clears_device_marker_and_preserves_lease(recorder_lease):
    owner, device = recorder_lease
    admin = User.objects.create_user(username='recorder-fence-admin', is_staff=True)
    generation = device.lease_generation
    owner_id = device.locked_by_id
    locked_at = device.locked_at
    token_hash = device.lease_token_hash
    AppDevice.objects.filter(pk=device.pk).update(device_action_pending=True)

    client = APIClient()
    client.force_authenticate(user=admin)
    response = client.post(
        f'/api/app-automation/devices/{device.pk}/recover-pending-action/',
        {'confirm_no_active_worker': True},
        format='json',
    )

    assert response.status_code == 200
    assert response.data['recovered_device_action'] is True
    device.refresh_from_db()
    assert device.device_action_pending is False
    assert device.maintenance_mode is True
    assert device.health_status == 'unknown'
    assert device.lease_generation == generation
    assert device.locked_by_id == owner_id
    assert device.locked_at == locked_at
    assert device.lease_token_hash == token_hash
    assert release_device_execution_lease(device.pk, owner, generation) is True
    with pytest.raises(DeviceLeaseError):
        acquire_device(admin)


@pytest.mark.django_db(transaction=True)
def test_non_admin_cannot_recover_device_marker(recorder_lease):
    user, device = recorder_lease
    AppDevice.objects.filter(pk=device.pk).update(device_action_pending=True)
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post(
        f'/api/app-automation/devices/{device.pk}/recover-pending-action/',
        {'confirm_no_active_worker': True},
        format='json',
    )

    assert response.status_code == 403
    assert AppDevice.objects.get(pk=device.pk).device_action_pending is True


@pytest.mark.django_db(transaction=True)
def test_device_marker_blocks_concurrent_recorder_and_execution_admission(recorder_lease):
    user, device = recorder_lease
    AppDevice.objects.filter(pk=device.pk).update(device_action_pending=True)

    with (
        patch('apps.app_automation.views.recorder_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.recorder_views.subprocess.run') as adb,
    ):
        response = _post_interaction(user, device)

    assert response.status_code == 409
    adb.assert_not_called()
    with pytest.raises(DeviceExecutionBusy):
        assert_device_execution_idle(device.pk)
    with pytest.raises(DeviceLeaseError, match='待安全恢复'):
        release_device(device.pk, user, '')


@pytest.mark.django_db(transaction=True)
def test_recorder_lease_acquisition_rejects_device_action_marker(db):
    user = User.objects.create_user(username='recorder-acquire-owner')
    device = AppDevice.objects.create(
        device_id='recorder-acquire-device', status='online', health_status='healthy'
    )
    AppDevice.objects.filter(pk=device.pk).update(device_action_pending=True)
    request = APIRequestFactory().post(
        f'/devices/{device.pk}/recorder-lease/',
        {'expected_user_id': user.pk},
        format='json',
    )
    force_authenticate(request, user=user)

    from apps.app_automation.views.recorder_lease_views import RecorderLeaseView

    response = RecorderLeaseView.as_view()(request, device_id=device.pk)

    assert response.status_code == 409
    device.refresh_from_db()
    assert device.locked_by_id is None
    assert device.device_action_pending is True


@pytest.mark.django_db(transaction=True)
def test_recorder_heartbeat_can_renew_while_action_marker_is_pending(recorder_lease):
    user, device = recorder_lease
    AppDevice.objects.filter(pk=device.pk).update(device_action_pending=True)

    heartbeat_device(device.pk, user, '', lease_seconds=60)

    device.refresh_from_db()
    assert device.device_action_pending is True
    assert device.last_heartbeat_at is not None
