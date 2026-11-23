from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.device_pool import acquire_specific_device, heartbeat_device
from apps.app_automation.models import AppDevice, AppTestExecution
from apps.app_automation.views.recorder_lease_views import RecorderLeaseView
from apps.users.models import User


def request_lease(method, user, device, payload):
    request = getattr(APIRequestFactory(), method)(f'/devices/{device.pk}/recorder-lease/', payload, format='json')
    force_authenticate(request, user=user)
    return RecorderLeaseView.as_view()(request, device_id=device.pk)


@pytest.mark.parametrize(('max_allocation_time', 'expected_seconds'), [(30, 30), (90, 90)])
@pytest.mark.django_db
def test_recorder_lease_reports_persisted_duration_and_heartbeat_cannot_extend_it_to_hours(
    max_allocation_time, expected_seconds,
):
    user = User.objects.create_user(username=f'short-recorder-owner-{max_allocation_time}')
    device = AppDevice.objects.create(
        device_id=f'synthetic-short-recorder-{max_allocation_time}',
        status='online',
        max_allocation_time=max_allocation_time,
    )
    response = request_lease('post', user, device, {'expected_user_id': user.pk, 'lease_seconds': 99999})
    assert response.status_code == 201
    assert response.data['lease_seconds'] == expected_seconds
    token = response.data['lease_token']
    device.refresh_from_db()
    assert int((device.lease_expires_at - device.lease_acquired_at).total_seconds()) == expected_seconds
    assert timedelta(seconds=0) < device.lease_expires_at - timezone.now() <= timedelta(seconds=expected_seconds)
    heartbeat_device(device.pk, user, token, lease_seconds=response.data['lease_seconds'])
    device.refresh_from_db()
    assert device.lease_expires_at - timezone.now() <= timedelta(seconds=expected_seconds)


@pytest.mark.django_db
def test_expired_recorder_token_cannot_release_a_newer_same_owner_lease_even_for_admin():
    user = User.objects.create_user(username='recorder-admin', is_staff=True)
    device = AppDevice.objects.create(device_id='synthetic-recorder-replaced', status='online')
    first = request_lease('post', user, device, {'expected_user_id': user.pk})
    old_token = first.data['lease_token']
    AppDevice.objects.filter(pk=device.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    device, new_token = acquire_specific_device(device.pk, user, lease_seconds=90)
    response = request_lease('delete', user, device, {'expected_user_id': user.pk, 'lease_token': old_token})
    assert response.status_code == 403
    device.refresh_from_db()
    assert device.locked_by_id == user.pk
    assert request_lease('delete', user, device, {'expected_user_id': user.pk, 'lease_token': new_token}).status_code == 200


@pytest.mark.django_db
def test_busy_device_or_changed_identity_cannot_acquire_or_release():
    user = User.objects.create_user(username='recorder-lease-owner')
    other = User.objects.create_user(username='recorder-lease-other')
    device = AppDevice.objects.create(device_id='synthetic-recorder-busy', status='online')
    denied = request_lease('post', user, device, {'expected_user_id': other.pk})
    assert denied.status_code == 403
    first = request_lease('post', user, device, {'expected_user_id': user.pk})
    assert first.status_code == 201
    token = first.data['lease_token']
    AppTestExecution.objects.create(user=user, device=device, status='pending',
                                    device_lease_generation=AppDevice.objects.get(pk=device.pk).lease_generation)
    assert request_lease('delete', user, device, {'expected_user_id': user.pk, 'lease_token': token}).status_code == 409
    assert request_lease('post', other, device, {'expected_user_id': other.pk}).status_code == 403
