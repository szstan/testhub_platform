import subprocess
from unittest.mock import Mock, patch

import pytest
from django.db import connection
from rest_framework.test import APIClient

from apps.app_automation.device_pool import (
    acquire_specific_device,
    begin_device_action,
    heartbeat_device,
)
from apps.app_automation.models import AppDevice
from apps.users.models import User


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _discovery_row(device_id, *, name='ADB snapshot'):
    return {
        'device_id': device_id,
        'name': name,
        'status': 'online',
        'android_version': '14',
        'ip_address': '192.0.2.90',
        'port': 5555,
    }


@pytest.mark.django_db(transaction=True)
def test_delayed_management_completion_cannot_release_recovered_retry_reservation(db):
    admin = User.objects.create_user(username='action-epoch-management-admin', is_staff=True)
    device = AppDevice.objects.create(
        device_id='192.0.2.91:5555',
        status='online',
        connection_type='remote_emulator',
        ip_address='192.0.2.91',
        port=5555,
    )
    client = _client(admin)
    manager = Mock()
    calls = 0

    def disconnect(_identity):
        nonlocal calls
        calls += 1
        if calls == 1:
            recovered = client.post(
                f'/api/app-automation/devices/{device.pk}/recover-pending-action/',
                {'confirm_no_active_worker': True},
                format='json',
            )
            assert recovered.status_code == 200, recovered.data
            retry = client.post(
                f'/api/app-automation/devices/{device.pk}/disconnect/', {}, format='json'
            )
            assert retry.status_code == 200, retry.data
            assert 'device_action_epoch' not in retry.data['device']
            return True
        return True

    manager.disconnect_device.side_effect = disconnect
    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        delayed = client.post(
            f'/api/app-automation/devices/{device.pk}/disconnect/', {}, format='json'
        )

    assert delayed.status_code == 503, delayed.data
    assert calls == 2
    device.refresh_from_db()
    assert device.status == 'offline'
    assert device.device_action_pending is False
    assert device.device_action_epoch == 3
    assert device.lease_generation == 1
    assert device.locked_by_id == admin.pk
    assert device.lease_token_hash == ''


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    'bad_token', [1, True, [], {}, ['not-a-token']],
    ids=['integer', 'boolean', 'list', 'object', 'nested-list'],
)
@pytest.mark.parametrize('endpoint', ['unlock', 'disconnect'])
def test_management_rejects_malformed_lease_token_without_marker_or_io(db, bad_token, endpoint):
    owner = User.objects.create_user(username=f'malformed-token-{endpoint}')
    device = AppDevice.objects.create(
        device_id=f'192.0.2.{100 if endpoint == "unlock" else 101}:5555',
        status='online',
        connection_type='remote_emulator',
        ip_address='192.0.2.100',
        port=5555,
    )
    device, _token = acquire_specific_device(device.pk, owner, lease_seconds=120)
    client = _client(owner)
    manager = Mock()
    route = 'unlock' if endpoint == 'unlock' else 'disconnect'

    with (
        patch('apps.app_automation.views.device_views.get_adb_path') as get_adb_path,
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        response = client.post(
            f'/api/app-automation/devices/{device.pk}/{route}/',
            {'lease_token': bad_token},
            format='json',
        )

    assert response.status_code == 403
    assert 'Traceback' not in str(response.data)
    get_adb_path.assert_not_called()
    manager.disconnect_device.assert_not_called()
    device.refresh_from_db()
    assert device.device_action_pending is False
    assert device.locked_by_id == owner.pk


@pytest.mark.django_db(transaction=True)
def test_discovery_does_not_resurrect_offline_state_after_admin_free_disconnect(db):
    admin = User.objects.create_user(username='stale-discovery-admin', is_staff=True)
    device = AppDevice.objects.create(
        device_id='192.0.2.92:5555',
        name='Managed device',
        status='online',
        connection_type='remote_emulator',
        ip_address='192.0.2.92',
        port=5555,
    )
    client = _client(admin)
    manager = Mock()

    def list_devices():
        assert connection.in_atomic_block is False
        disconnected = client.post(
            f'/api/app-automation/devices/{device.pk}/disconnect/', {}, format='json'
        )
        assert disconnected.status_code == 200, disconnected.data
        return [_discovery_row(device.device_id)]

    manager.list_devices.side_effect = list_devices
    manager.disconnect_device.return_value = True
    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        response = client.get('/api/app-automation/devices/discover/')

    assert response.status_code == 200, response.data
    assert response.data['devices'][0]['status'] == 'offline'
    device.refresh_from_db()
    assert device.status == 'offline'
    assert device.locked_by_id is None
    assert device.device_action_pending is False


@pytest.mark.django_db(transaction=True)
def test_discovery_does_not_overwrite_row_created_after_scan_snapshot(db):
    admin = User.objects.create_user(username='discovery-new-row-admin', is_staff=True)
    client = _client(admin)
    manager = Mock()
    device_id = 'late-created-device'

    def list_devices():
        assert connection.in_atomic_block is False
        AppDevice.objects.create(device_id=device_id, name='Concurrent row', status='offline')
        return [_discovery_row(device_id)]

    manager.list_devices.side_effect = list_devices
    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        response = client.get('/api/app-automation/devices/discover/')

    assert response.status_code == 200, response.data
    row = AppDevice.objects.get(device_id=device_id)
    assert row.status == 'offline'
    assert row.name == 'Concurrent row'
    assert response.data['devices'][0]['name'] == 'Concurrent row'


@pytest.mark.django_db(transaction=True)
def test_discovery_does_not_recreate_row_deleted_after_scan_snapshot(db):
    admin = User.objects.create_user(username='discovery-deleted-row-admin', is_staff=True)
    device = AppDevice.objects.create(device_id='deleted-during-scan', status='online')
    client = _client(admin)
    manager = Mock()

    def list_devices():
        assert connection.in_atomic_block is False
        device.delete()
        return [_discovery_row(device.device_id)]

    manager.list_devices.side_effect = list_devices
    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        response = client.get('/api/app-automation/devices/discover/')

    assert response.status_code == 200, response.data
    assert response.data['devices'] == []
    assert not AppDevice.objects.filter(device_id='deleted-during-scan').exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('concurrent_change', ['heartbeat', 'lease_replacement'])
def test_discovery_ignores_stale_snapshot_after_lease_authority_change(
    db, concurrent_change,
):
    owner = User.objects.create_user(username=f'discovery-owner-{concurrent_change}')
    replacement = User.objects.create_user(username=f'discovery-replacement-{concurrent_change}')
    device = AppDevice.objects.create(
        device_id=f'discovery-{concurrent_change}', name='Current name', status='online'
    )
    device, token = acquire_specific_device(device.pk, owner, lease_seconds=120)
    client = _client(User.objects.create_user(
        username=f'discovery-admin-{concurrent_change}', is_staff=True
    ))
    manager = Mock()

    def list_devices():
        assert connection.in_atomic_block is False
        if concurrent_change == 'heartbeat':
            heartbeat_device(device.pk, owner, token, lease_seconds=180)
        else:
            from apps.app_automation.device_pool import release_device

            release_device(device.pk, owner, token)
            acquire_specific_device(device.pk, replacement, lease_seconds=120)
        return [_discovery_row(device.device_id, name='Stale ADB name')]

    manager.list_devices.side_effect = list_devices
    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        response = client.get('/api/app-automation/devices/discover/')

    assert response.status_code == 200, response.data
    device.refresh_from_db()
    assert device.name == 'Current name'
    assert device.status == 'locked'
    if concurrent_change == 'lease_replacement':
        assert device.locked_by_id == replacement.pk
        assert device.lease_generation == 2
    else:
        assert device.locked_by_id == owner.pk
        assert device.lease_generation == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ('adb_outcome', 'expected_status', 'expected_outcome'),
    [
        ('completed', 503, 'completed'),
        ('unknown', 504, 'unknown'),
    ],
)
def test_recorder_recovery_invalidates_delayed_finish_and_quarantine(
    db, adb_outcome, expected_status, expected_outcome,
):
    owner = User.objects.create_user(username=f'recorder-epoch-owner-{adb_outcome}')
    admin = User.objects.create_user(username=f'recorder-epoch-admin-{adb_outcome}', is_staff=True)
    device = AppDevice.objects.create(
        device_id=f'recorder-epoch-{adb_outcome}', status='online', health_status='healthy'
    )
    device.lock(owner)
    admin_client = _client(admin)
    retry_fences = []

    def adb_call(*_args, **_kwargs):
        recovered = admin_client.post(
            f'/api/app-automation/devices/{device.pk}/recover-pending-action/',
            {'confirm_no_active_worker': True},
            format='json',
        )
        assert recovered.status_code == 200, recovered.data
        cleared = admin_client.patch(
            f'/api/app-automation/devices/{device.pk}/',
            {'maintenance_mode': False, 'expected_user_id': admin.pk},
            format='json',
        )
        assert cleared.status_code == 200, cleared.data
        retry_fences.append(begin_device_action(device.pk, owner, ''))
        if adb_outcome == 'unknown':
            raise subprocess.TimeoutExpired('/fake/adb', 8)

    with (
        patch('apps.app_automation.views.recorder_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.recorder_views.subprocess.run', side_effect=adb_call),
    ):
        response = _client(owner).post(
            f'/api/app-automation/devices/{device.pk}/record-interaction/',
            {'action': 'tap', 'x': 1, 'y': 2, 'expected_user_id': owner.pk},
            format='json',
        )

    assert response.status_code == expected_status
    assert response.data['action_outcome'] == expected_outcome
    assert response.data['recovery_required'] is True
    assert len(retry_fences) == 1
    assert retry_fences[0][1] == device.lease_generation
    assert retry_fences[0][2] == 3
    device.refresh_from_db()
    assert device.device_action_pending is True
    assert device.device_action_epoch == 3
    assert device.maintenance_mode is False
    assert 'device_action_epoch' not in response.data
