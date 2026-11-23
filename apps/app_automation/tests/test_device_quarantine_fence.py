from datetime import timedelta

import pytest
from django.utils import timezone

from apps.app_automation.device_pool import (
    DeviceLeaseError,
    DeviceLeaseHeartbeat,
    acquire_device,
    assert_device_execution_lease,
    release_device_execution_lease,
)
from apps.app_automation.models import AppDevice, AppTestExecution
from apps.users.models import User


@pytest.fixture
def active_device_lease(db):
    user = User.objects.create_user(username='quarantine-owner')
    device = AppDevice.objects.create(
        device_id='quarantine-device',
        status='available',
        health_status='healthy',
    )
    device, _token = acquire_device(user)
    return user, device


@pytest.mark.django_db
@pytest.mark.parametrize(
    'device_updates',
    [
        pytest.param({'maintenance_mode': True}, id='maintenance'),
        pytest.param({'status': 'offline'}, id='offline'),
    ],
)
def test_active_generation_heartbeat_latches_when_device_is_unavailable(
    active_device_lease, device_updates,
):
    user, device = active_device_lease
    AppDevice.objects.filter(pk=device.pk).update(**device_updates)
    heartbeat = DeviceLeaseHeartbeat(device.pk, user, device.lease_generation)

    with pytest.raises(DeviceLeaseError, match='设备不可用'):
        heartbeat.raise_if_lost()
    with pytest.raises(DeviceLeaseError, match='设备租约续期失败'):
        heartbeat.raise_if_lost()


@pytest.mark.django_db
def test_legacy_generation_zero_is_blocked_until_maintenance_is_cleared(db):
    user = User.objects.create_user(username='legacy-quarantine-owner')
    locked_at = timezone.now() - timedelta(seconds=1)
    device = AppDevice.objects.create(
        device_id='legacy-quarantine-device',
        status='locked',
        locked_by=user,
        locked_at=locked_at,
        last_heartbeat_at=locked_at,
        lease_generation=0,
        maintenance_mode=True,
    )
    execution = AppTestExecution.objects.create(
        device=device,
        user=user,
        device_lease_generation=None,
        status='running',
    )

    with pytest.raises(DeviceLeaseError, match='设备不可用'):
        assert_device_execution_lease(device.pk, user, None, execution_id=execution.pk)

    AppDevice.objects.filter(pk=device.pk).update(maintenance_mode=False)
    assert_device_execution_lease(device.pk, user, None, execution_id=execution.pk)
    assert_device_execution_lease(device.pk, user, 0, execution_id=execution.pk)


@pytest.mark.django_db
def test_releasing_quarantined_lease_does_not_restore_device_automatically(active_device_lease):
    user, device = active_device_lease
    generation = device.lease_generation
    AppDevice.objects.filter(pk=device.pk).update(maintenance_mode=True)

    with pytest.raises(DeviceLeaseError, match='设备不可用'):
        assert_device_execution_lease(device.pk, user, generation)

    assert release_device_execution_lease(device.pk, user, generation)
    device.refresh_from_db()
    assert device.maintenance_mode is True
    with pytest.raises(DeviceLeaseError, match='没有满足能力条件的可用设备'):
        acquire_device(user)

    AppDevice.objects.filter(pk=device.pk).update(maintenance_mode=False)
    restored, _token = acquire_device(user)
    assert restored.pk == device.pk
    assert_device_execution_lease(device.pk, user, restored.lease_generation)
