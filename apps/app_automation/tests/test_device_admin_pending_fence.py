import pytest
from django.db import connection
from django.db.models.query import QuerySet
from django.urls import reverse
from rest_framework.test import APIClient

from apps.app_automation.models import AppDevice, AppTestExecution
from apps.users.models import User


@pytest.fixture
def device_admin(db):
    return User.objects.create_user(username='pending-device-admin', is_staff=True)


def _create_pending_device(marker_source, *, maintenance_mode=True):
    device = AppDevice.objects.create(
        device_id='pending-device-serial',
        name='Pending device',
        status='online',
        maintenance_mode=maintenance_mode,
        device_action_pending=marker_source == 'device',
        lease_token_hash='stored-token-hash-must-not-leak',
    )
    execution = None
    if marker_source == 'execution':
        execution = AppTestExecution.objects.create(
            device=device,
            status='error',
            device_action_pending=True,
        )
    return device, execution


def _admin_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
@pytest.mark.parametrize('marker_source', ['device', 'execution'])
def test_admin_cannot_delete_device_with_pending_action(device_admin, marker_source):
    device, _execution = _create_pending_device(marker_source)
    response = _admin_client(device_admin).delete(
        reverse('app-device-detail', args=[device.pk]),
    )

    assert response.status_code == 409
    assert AppDevice.objects.filter(pk=device.pk).exists()
    assert device.device_id not in response.content.decode()
    assert 'stored-token-hash-must-not-leak' not in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize('marker_source', ['device', 'execution'])
@pytest.mark.parametrize(
    'changes',
    [
        {'device_id': 'replacement-device-serial'},
        {'connection_type': 'remote_emulator'},
        {'ip_address': '192.0.2.10'},
        {'port': 5556},
        {'status': 'offline'},
        {'maintenance_mode': False},
    ],
    ids=[
        'device-id', 'connection-type', 'ip-address', 'port', 'status', 'clear-maintenance',
    ],
)
def test_admin_cannot_change_protected_fields_with_pending_action(
    device_admin, marker_source, changes,
):
    device, execution = _create_pending_device(marker_source)
    response = _admin_client(device_admin).patch(
        reverse('app-device-detail', args=[device.pk]),
        changes,
        format='json',
    )

    assert response.status_code == 409
    assert device.device_id not in response.content.decode()
    assert 'stored-token-hash-must-not-leak' not in response.content.decode()
    device.refresh_from_db()
    assert device.device_id == 'pending-device-serial'
    assert device.connection_type == 'emulator'
    assert device.ip_address == ''
    assert device.port == 5555
    assert device.status == 'online'
    assert device.maintenance_mode is True
    assert device.device_action_pending is (marker_source == 'device')
    if execution:
        execution.refresh_from_db()
        assert execution.device_action_pending is True


@pytest.mark.django_db
@pytest.mark.parametrize('marker_source', ['device', 'execution'])
def test_explicit_false_maintenance_request_is_blocked_even_if_already_false(
    device_admin, marker_source,
):
    device, _execution = _create_pending_device(marker_source, maintenance_mode=False)
    response = _admin_client(device_admin).patch(
        reverse('app-device-detail', args=[device.pk]),
        {'maintenance_mode': False},
        format='json',
    )

    assert response.status_code == 409
    device.refresh_from_db()
    assert device.maintenance_mode is False


@pytest.mark.django_db
@pytest.mark.parametrize('method', ['patch', 'delete'])
def test_non_admin_cannot_mutate_device_with_pending_action(db, method):
    user = User.objects.create_user(username=f'pending-device-user-{method}')
    device, _execution = _create_pending_device('execution')
    client = _admin_client(user)
    url = reverse('app-device-detail', args=[device.pk])

    if method == 'patch':
        response = client.patch(url, {'device_id': 'unauthorized-replacement'}, format='json')
    else:
        response = client.delete(url)

    assert response.status_code == 403
    device.refresh_from_db()
    assert device.device_id == 'pending-device-serial'
    assert AppTestExecution.objects.get(device=device).device_action_pending is True


@pytest.mark.django_db
@pytest.mark.parametrize('marker_source', ['device', 'execution'])
def test_pending_action_allows_benign_metadata_edit(device_admin, marker_source):
    device, execution = _create_pending_device(marker_source)
    response = _admin_client(device_admin).patch(
        reverse('app-device-detail', args=[device.pk]),
        {'name': 'Updated device', 'description': 'Metadata only'},
        format='json',
    )

    assert response.status_code == 200, response.data
    device.refresh_from_db()
    assert device.name == 'Updated device'
    assert device.description == 'Metadata only'
    assert device.device_id == 'pending-device-serial'
    assert device.status == 'online'
    assert device.maintenance_mode is True
    assert device.device_action_pending is (marker_source == 'device')
    if execution:
        execution.refresh_from_db()
        assert execution.device_action_pending is True


@pytest.mark.django_db
def test_explicit_recovery_allows_protected_device_changes(device_admin):
    device, _execution = _create_pending_device('device')
    execution = AppTestExecution.objects.create(
        device=device,
        status='error',
        device_action_pending=True,
    )
    client = _admin_client(device_admin)
    detail_url = reverse('app-device-detail', args=[device.pk])

    recovered = client.post(
        reverse('app-device-recover-pending-action', args=[device.pk]),
        {'confirm_no_active_worker': True},
        format='json',
    )
    assert recovered.status_code == 200, recovered.data
    assert recovered.data['maintenance_mode'] is True
    assert recovered.data['device_lease_preserved'] is True

    response = client.patch(
        detail_url,
        {
            'device_id': 'recovered-device-serial',
            'connection_type': 'remote_emulator',
            'ip_address': '192.0.2.10',
            'port': 5556,
            'status': 'online',
            'maintenance_mode': False,
        },
        format='json',
    )
    assert response.status_code == 200, response.data
    assert 'lease_token_hash' not in response.data
    device.refresh_from_db()
    execution.refresh_from_db()
    assert device.device_id == 'recovered-device-serial'
    assert device.connection_type == 'remote_emulator'
    assert device.ip_address == '192.0.2.10'
    assert device.port == 5556
    assert device.status == 'online'
    assert device.maintenance_mode is False
    assert device.device_action_pending is False
    assert execution.device_action_pending is False


@pytest.mark.django_db
def test_expected_user_id_guards_recovery_and_maintenance_clear(device_admin):
    device, _execution = _create_pending_device('device')
    client = _admin_client(device_admin)
    recovery_url = reverse('app-device-recover-pending-action', args=[device.pk])
    detail_url = reverse('app-device-detail', args=[device.pk])
    stale_user_id = device_admin.pk + 1

    stale_recovery = client.post(
        recovery_url,
        {'confirm_no_active_worker': True, 'expected_user_id': stale_user_id},
        format='json',
    )
    assert stale_recovery.status_code == 403
    device.refresh_from_db()
    assert device.device_action_pending is True

    recovered = client.post(
        recovery_url,
        {'confirm_no_active_worker': True, 'expected_user_id': device_admin.pk},
        format='json',
    )
    assert recovered.status_code == 200, recovered.data

    stale_clear = client.patch(
        detail_url,
        {'maintenance_mode': False, 'expected_user_id': stale_user_id},
        format='json',
    )
    assert stale_clear.status_code == 403
    device.refresh_from_db()
    assert device.maintenance_mode is True

    current_clear = client.patch(
        detail_url,
        {'maintenance_mode': False, 'expected_user_id': device_admin.pk},
        format='json',
    )
    assert current_clear.status_code == 200, current_clear.data
    device.refresh_from_db()
    assert device.maintenance_mode is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('method', ['patch', 'delete'])
def test_admin_mutation_locks_device_inside_atomic_transaction(db, device_admin, monkeypatch, method):
    device = AppDevice.objects.create(device_id=f'lock-scope-{method}', status='online')
    original_select_for_update = QuerySet.select_for_update
    lock_transactions = []

    def track_select_for_update(queryset, *args, **kwargs):
        if queryset.model is AppDevice:
            lock_transactions.append(connection.in_atomic_block)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, 'select_for_update', track_select_for_update)
    client = _admin_client(device_admin)
    url = reverse('app-device-detail', args=[device.pk])
    if method == 'patch':
        response = client.patch(url, {'name': 'Locked update'}, format='json')
        assert response.status_code == 200, response.data
    else:
        response = client.delete(url)
        assert response.status_code == 204

    assert lock_transactions == [True]
