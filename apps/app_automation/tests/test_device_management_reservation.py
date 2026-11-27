from unittest.mock import Mock, patch

import pytest
from django.db import connection
from rest_framework.test import APIClient

from apps.app_automation.device_pool import (
    DeviceActionBusy,
    DeviceLeaseError,
    acquire_specific_device,
    begin_device_action,
    release_device,
    release_device_execution_lease,
)
from apps.app_automation.execution_admission import (
    DeviceExecutionBusy,
    claim_app_execution_batch,
)
from apps.app_automation.models import AppDevice, AppTestExecution
from apps.users.models import User


@pytest.fixture
def remote_device_lease(db):
    owner = User.objects.create_user(username='management-fence-owner')
    device = AppDevice.objects.create(
        device_id='192.0.2.60:5555',
        status='online',
        connection_type='remote_emulator',
        ip_address='192.0.2.60',
        port=5555,
        health_status='healthy',
    )
    device, token = acquire_specific_device(device.pk, owner, lease_seconds=120)
    return owner, device, token


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db(transaction=True)
def test_disconnect_commits_reservation_before_io_and_blocks_recorder_and_batch_admission(
    remote_device_lease,
):
    owner, device, token = remote_device_lease
    observed = {}
    manager = Mock()

    def disconnect(identity):
        current = AppDevice.objects.get(pk=device.pk)
        observed['identity'] = identity
        observed['in_atomic_block'] = connection.in_atomic_block
        observed['pending_before_io'] = current.device_action_pending
        with pytest.raises(DeviceActionBusy):
            begin_device_action(device.pk, owner, token)
        with pytest.raises(DeviceLeaseError, match='待安全恢复'):
            release_device(device.pk, owner, token)

        execution = AppTestExecution.objects.create(
            device=device,
            user=owner,
            status='pending',
            device_lease_generation=current.lease_generation,
        )
        with pytest.raises(DeviceExecutionBusy):
            claim_app_execution_batch([execution.pk])
        AppTestExecution.objects.filter(pk=execution.pk).update(status='error')
        return True

    manager.disconnect_device.side_effect = disconnect
    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/synthetic/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        response = _client(owner).post(
            f'/api/app-automation/devices/{device.pk}/disconnect/',
            {'lease_token': token},
            format='json',
        )

    assert response.status_code == 200, response.data
    assert observed == {
        'identity': '192.0.2.60:5555',
        'in_atomic_block': False,
        'pending_before_io': True,
    }
    manager.disconnect_device.assert_called_once_with('192.0.2.60:5555')
    device.refresh_from_db()
    assert device.status == 'offline'
    assert device.device_action_pending is False
    assert device.locked_by_id == owner.pk
    assert device.lease_token_hash
    assert release_device_execution_lease(device.pk, owner, device.lease_generation) is True
    device.refresh_from_db()
    assert device.status == 'offline'


@pytest.mark.django_db(transaction=True)
def test_disconnect_rejects_an_already_queued_execution(remote_device_lease):
    owner, device, token = remote_device_lease
    AppTestExecution.objects.create(
        device=device,
        user=owner,
        status='pending',
        device_lease_generation=device.lease_generation,
    )
    manager = Mock()

    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/synthetic/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        response = _client(owner).post(
            f'/api/app-automation/devices/{device.pk}/disconnect/',
            {'lease_token': token},
            format='json',
        )

    assert response.status_code == 409
    manager.disconnect_device.assert_not_called()
    device.refresh_from_db()
    assert device.device_action_pending is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('outcome', [False, RuntimeError('synthetic command failure')], ids=['false', 'exception'])
def test_disconnect_unknown_outcome_quarantines_and_retains_lease(remote_device_lease, outcome):
    owner, device, token = remote_device_lease
    manager = Mock()
    if isinstance(outcome, Exception):
        manager.disconnect_device.side_effect = outcome
    else:
        manager.disconnect_device.return_value = outcome

    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/synthetic/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        response = _client(owner).post(
            f'/api/app-automation/devices/{device.pk}/disconnect/',
            {'lease_token': token},
            format='json',
        )

    assert response.status_code == 502, response.data
    assert response.data['recovery_required'] is True
    device.refresh_from_db()
    assert device.device_action_pending is True
    assert device.maintenance_mode is True
    assert device.health_status == 'unknown'
    assert device.locked_by_id == owner.pk
    assert release_device_execution_lease(device.pk, owner, device.lease_generation) is False


@pytest.mark.django_db(transaction=True)
def test_admin_disconnect_of_free_device_releases_only_its_temporary_reservation(db):
    admin = User.objects.create_user(username='management-fence-admin', is_staff=True)
    device = AppDevice.objects.create(
        device_id='192.0.2.61:5555',
        status='online',
        connection_type='remote_emulator',
        ip_address='192.0.2.61',
        port=5555,
    )
    manager = Mock()
    manager.disconnect_device.return_value = True

    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/synthetic/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        response = _client(admin).post(
            f'/api/app-automation/devices/{device.pk}/disconnect/', {}, format='json'
        )

    assert response.status_code == 200, response.data
    device.refresh_from_db()
    assert device.status == 'offline'
    assert device.locked_by_id is None
    assert device.device_action_pending is False
    assert device.lease_generation == 1


@pytest.mark.django_db(transaction=True)
def test_admin_disconnect_of_free_device_keeps_unknown_reservation(db):
    admin = User.objects.create_user(username='management-unknown-admin', is_staff=True)
    device = AppDevice.objects.create(
        device_id='192.0.2.62:5555',
        status='online',
        connection_type='remote_emulator',
        ip_address='192.0.2.62',
        port=5555,
    )
    manager = Mock()
    manager.disconnect_device.return_value = False

    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/synthetic/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        response = _client(admin).post(
            f'/api/app-automation/devices/{device.pk}/disconnect/', {}, format='json'
        )

    assert response.status_code == 502
    device.refresh_from_db()
    assert device.device_action_pending is True
    assert device.maintenance_mode is True
    assert device.locked_by_id == admin.pk
    assert device.lease_token_hash == ''


@pytest.mark.django_db(transaction=True)
def test_disconnect_requires_token_for_token_lease_but_preserves_legacy_tokenless_lock(db):
    owner = User.objects.create_user(username='management-token-owner')
    token_device = AppDevice.objects.create(
        device_id='192.0.2.63:5555', status='online', connection_type='remote_emulator',
        ip_address='192.0.2.63', port=5555,
    )
    token_device, token = acquire_specific_device(token_device.pk, owner, lease_seconds=120)
    manager = Mock()
    manager.disconnect_device.return_value = True
    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/synthetic/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        missing_token = _client(owner).post(
            f'/api/app-automation/devices/{token_device.pk}/disconnect/', {}, format='json'
        )
    assert missing_token.status_code == 403
    manager.disconnect_device.assert_not_called()
    token_device.refresh_from_db()
    assert token_device.device_action_pending is False

    legacy_device = AppDevice.objects.create(
        device_id='192.0.2.64:5555', status='online', connection_type='remote_emulator',
        ip_address='192.0.2.64', port=5555,
    )
    legacy_device.lock(owner)
    with (
        patch('apps.app_automation.views.device_views.get_adb_path', return_value='/synthetic/adb'),
        patch('apps.app_automation.views.device_views.DeviceManager', return_value=manager),
    ):
        disconnected = _client(owner).post(
            f'/api/app-automation/devices/{legacy_device.pk}/disconnect/', {}, format='json'
        )
    assert disconnected.status_code == 200, disconnected.data
    legacy_device.refresh_from_db()
    assert legacy_device.status == 'offline'
    assert legacy_device.locked_by_id == owner.pk
    assert legacy_device.lease_token_hash == ''
    assert legacy_device.device_action_pending is False

    unlocked = _client(owner).post(
        f'/api/app-automation/devices/{legacy_device.pk}/unlock/', {}, format='json'
    )
    assert unlocked.status_code == 200, unlocked.data
    legacy_device.refresh_from_db()
    assert legacy_device.locked_by_id is None


def test_legacy_unlock_rejects_missing_token_for_token_backed_lease(remote_device_lease):
    owner, device, token = remote_device_lease
    client = _client(owner)
    url = f'/api/app-automation/devices/{device.pk}/unlock/'

    missing = client.post(url, {}, format='json')
    assert missing.status_code == 403
    device.refresh_from_db()
    assert device.locked_by_id == owner.pk
    assert device.lease_token_hash

    released = client.post(url, {'lease_token': token}, format='json')
    assert released.status_code == 200, released.data
    device.refresh_from_db()
    assert device.locked_by_id is None
    assert device.lease_token_hash == ''


@pytest.mark.django_db
def test_admin_force_release_does_not_bypass_a_token_backed_lease(db):
    owner = User.objects.create_user(username='force-release-owner')
    admin = User.objects.create_user(username='force-release-admin', is_staff=True)
    device = AppDevice.objects.create(device_id='force-release-token-device', status='online')
    device, token = acquire_specific_device(device.pk, owner, lease_seconds=120)
    client = _client(admin)
    url = f'/api/app-automation/devices/{device.pk}/release/'

    missing = client.post(url, {}, format='json')
    assert missing.status_code == 403
    device.refresh_from_db()
    assert device.locked_by_id == owner.pk

    released = client.post(url, {'lease_token': token}, format='json')
    assert released.status_code == 200, released.data
    device.refresh_from_db()
    assert device.locked_by_id is None


@pytest.mark.django_db
def test_device_list_allows_only_stable_id_ordering_and_keeps_page_cap(db):
    admin = User.objects.create_user(username='device-ordering-admin', is_staff=True)
    AppDevice.objects.bulk_create([
        AppDevice(device_id=f'ordered-device-{index:03d}', status='online')
        for index in range(101)
    ])
    AppDevice.objects.create(device_id='ordered-device-offline', status='offline')
    client = _client(admin)

    first = client.get(
        '/api/app-automation/devices/',
        {'status': 'online', 'page': 1, 'page_size': 1000, 'ordering': 'id'},
    )
    assert first.status_code == 200, first.data
    assert first.data['count'] == 101
    assert len(first.data['results']) == 100
    assert first.data['next']
    first_ids = [item['id'] for item in first.data['results']]
    assert first_ids == sorted(first_ids)

    second = client.get(
        '/api/app-automation/devices/',
        {'status': 'online', 'page': 2, 'page_size': 100, 'ordering': 'id'},
    )
    assert second.status_code == 200
    assert [item['id'] for item in second.data['results']] == [max(
        AppDevice.objects.filter(status='online').values_list('id', flat=True)
    )]
    assert second.data['next'] is None
