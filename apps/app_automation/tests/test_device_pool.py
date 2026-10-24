from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.app_automation.device_pool import (
    DeviceLeaseError,
    acquire_device,
    heartbeat_device,
    release_device,
)
from apps.app_automation.models import AppDevice
from apps.users.models import User


@pytest.fixture
def device_pool_user(db):
    user = User.objects.create_user(username='device-pool-owner', password='pass')
    device = AppDevice.objects.create(
        device_id='pool-device-1',
        name='Pool device',
        status='available',
        health_status='healthy',
        capabilities={'platform': 'android', 'android_version': '12', 'resolution': '1080x1920'},
    )
    return user, device


@pytest.mark.django_db
def test_device_lease_acquire_heartbeat_release(device_pool_user):
    user, device = device_pool_user
    leased, token = acquire_device(
        user,
        capabilities={'platform': 'android', 'android_version': '12'},
        lease_seconds=120,
    )
    assert leased.id == device.id
    assert token
    assert leased.locked_by_id == user.id
    assert leased.lease_token_hash != token
    assert leased.lease_expires_at is not None

    heartbeat = heartbeat_device(device.id, user, token, lease_seconds=180)
    assert heartbeat.last_heartbeat_at is not None
    assert heartbeat.lease_expires_at > timezone.now()

    released = release_device(device.id, user, token)
    assert released.locked_by_id is None
    assert released.status == 'available'
    assert released.lease_token_hash == ''


@pytest.mark.django_db
def test_device_lease_rejects_wrong_capability_and_token(device_pool_user):
    user, device = device_pool_user
    with pytest.raises(DeviceLeaseError):
        acquire_device(user, capabilities={'android_version': '99'})
    leased, token = acquire_device(user, capabilities={'platform': 'android'})
    with pytest.raises(DeviceLeaseError):
        heartbeat_device(leased.id, user, 'wrong-token')
    release_device(leased.id, user, token)


@pytest.mark.django_db
def test_device_lease_reclaims_expired_lease(device_pool_user):
    user, device = device_pool_user
    leased, _token = acquire_device(user)
    AppDevice.objects.filter(pk=leased.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    replacement, replacement_token = acquire_device(user)
    assert replacement.id == device.id
    assert replacement.locked_by_id == user.id
    release_device(replacement.id, user, replacement_token)


@pytest.mark.django_db
def test_device_pool_api_returns_lease_without_exposing_hash(device_pool_user):
    user, device = device_pool_user
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(
        '/api/app-automation/devices/acquire/',
        {'capabilities': {'platform': 'android'}, 'lease_seconds': 120},
        format='json',
    )
    assert response.status_code == 201
    assert response.data['lease_token']
    assert 'lease_token_hash' not in response.data['device']
    release = client.post(
        f"/api/app-automation/devices/{device.id}/release/",
        {'lease_token': response.data['lease_token']},
        format='json',
    )
    assert release.status_code == 200
