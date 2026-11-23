from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.device_pool import (
    acquire_specific_device,
    heartbeat_device,
    heartbeat_device_execution,
    reap_expired_device_leases,
    release_device,
)
from apps.app_automation.models import AppDevice, AppTestExecution
from apps.app_automation.serializers import AppDeviceSerializer
from apps.app_automation.views.recorder_lease_views import RecorderLeaseView
from apps.users.models import User


def recorder_lease(user, device, **payload):
    data = {"expected_user_id": user.pk, **payload}
    request = APIRequestFactory().post(
        f"/devices/{device.pk}/recorder-lease/", data, format="json"
    )
    force_authenticate(request, user=user)
    return RecorderLeaseView.as_view()(request, device_id=device.pk)


@pytest.mark.django_db
@pytest.mark.parametrize(("allocation_seconds", "granted_seconds"), [(30, 30), (28800, 90)])
def test_recorder_grant_sets_server_owned_cap_and_generic_heartbeat_cannot_raise_it(
    allocation_seconds, granted_seconds,
):
    user = User.objects.create_user(username=f"recorder-cap-{allocation_seconds}")
    device = AppDevice.objects.create(
        device_id=f"synthetic-recorder-cap-{allocation_seconds}",
        status="online",
        max_allocation_time=allocation_seconds,
    )

    response = recorder_lease(
        user,
        device,
        lease_seconds=28800,
        lease_max_seconds=28800,
    )

    assert response.status_code == 201
    assert response.data["lease_seconds"] == granted_seconds
    assert "lease_max_seconds" not in response.data["device"]
    device.refresh_from_db()
    assert device.lease_max_seconds == granted_seconds
    assert int((device.lease_expires_at - device.lease_acquired_at).total_seconds()) == granted_seconds

    serializer = AppDeviceSerializer(
        device, data={"lease_max_seconds": 28800}, partial=True
    )
    assert serializer.is_valid(), serializer.errors
    serializer.save()
    device.refresh_from_db()
    assert device.lease_max_seconds == granted_seconds

    heartbeat_device(
        device.pk,
        user,
        response.data["lease_token"],
        lease_seconds=28800,
    )
    device.refresh_from_db()
    assert device.lease_expires_at - timezone.now() <= timedelta(seconds=granted_seconds)


@pytest.mark.django_db
def test_worker_heartbeat_cannot_raise_recorder_lease_cap():
    user = User.objects.create_user(username="recorder-worker-cap")
    device = AppDevice.objects.create(
        device_id="synthetic-recorder-worker-cap",
        status="online",
        max_allocation_time=28800,
    )
    response = recorder_lease(user, device)
    assert response.status_code == 201
    device.refresh_from_db()
    execution = AppTestExecution.objects.create(
        user=user,
        device=device,
        status="running",
        device_lease_generation=device.lease_generation,
    )

    heartbeat_device_execution(
        device.pk,
        user,
        device.lease_generation,
        execution_id=execution.pk,
        lease_seconds=28800,
    )

    device.refresh_from_db()
    assert device.lease_max_seconds == 90
    assert device.lease_expires_at - timezone.now() <= timedelta(seconds=90)


@pytest.mark.django_db
def test_reclaim_release_and_reacquisition_clear_or_replace_recorder_cap():
    user = User.objects.create_user(username="recorder-cap-lifecycle")
    device = AppDevice.objects.create(
        device_id="synthetic-recorder-cap-lifecycle", status="online"
    )
    first = recorder_lease(user, device)
    assert first.status_code == 201
    device.refresh_from_db()
    assert device.lease_max_seconds == 90

    AppDevice.objects.filter(pk=device.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert reap_expired_device_leases() == 1
    device.refresh_from_db()
    assert device.locked_by_id is None
    assert device.lease_max_seconds is None

    second = recorder_lease(user, device)
    assert second.status_code == 201
    device.refresh_from_db()
    assert device.lease_max_seconds == 90

    release_device(device.pk, user, second.data["lease_token"])
    device.refresh_from_db()
    assert device.lease_max_seconds is None

    ordinary, _ = acquire_specific_device(device.pk, user, lease_seconds=120)
    assert ordinary.lease_max_seconds is None


@pytest.mark.django_db
def test_legacy_and_manual_tokenless_leases_keep_existing_renewal_behavior():
    user = User.objects.create_user(username="recorder-cap-legacy")
    token_device = AppDevice.objects.create(
        device_id="synthetic-legacy-token-lease",
        status="online",
        max_allocation_time=3600,
    )
    token_device, token = acquire_specific_device(
        token_device.pk, user, lease_seconds=120
    )
    assert token_device.lease_max_seconds is None
    heartbeat_device(token_device.pk, user, token, lease_seconds=1800)
    token_device.refresh_from_db()
    assert token_device.lease_max_seconds is None
    assert token_device.lease_expires_at - timezone.now() <= timedelta(seconds=1800)

    manual_device = AppDevice.objects.create(
        device_id="synthetic-legacy-manual-lease", status="online"
    )
    manual_device.lock(user)
    execution = AppTestExecution.objects.create(
        user=user,
        device=manual_device,
        status="running",
        device_lease_generation=manual_device.lease_generation,
    )
    heartbeat_device_execution(
        manual_device.pk,
        user,
        manual_device.lease_generation,
        execution_id=execution.pk,
        lease_seconds=28800,
    )
    manual_device.refresh_from_db()
    assert manual_device.lease_max_seconds is None
    assert manual_device.lease_token_hash == ""
    assert manual_device.lease_expires_at is None
    assert manual_device.last_heartbeat_at is not None
