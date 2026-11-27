import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.app_automation.appium_models import AppiumOperation, AppiumSession
from apps.app_automation.appium_runtime import AppiumConfigurationError, endpoint_for_device
from apps.app_automation.device_pool import acquire_specific_device
from apps.app_automation.models import AppDevice, AppPackage, AppProject, AppTestCase, AppTestExecution
from apps.users.models import User


@pytest.fixture
def appium_start_request(db):
    user = User.objects.create_user(username="appium-endpoint-user")
    project = AppProject.objects.create(
        name="Appium endpoint project", owner=user, appium_enabled=True,
    )
    package = AppPackage.objects.create(
        name="Endpoint test app", package_name="org.example.endpoint", created_by=user,
    )
    device = AppDevice.objects.create(
        name="Endpoint test device", device_id="appium-endpoint-serial", status="available",
    )
    device, lease_token = acquire_specific_device(
        device.pk, user, lease_seconds=90, cap_lease_to_grant=True,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, project, package, device, lease_token, client


def test_endpoint_map_uses_django_configured_json_and_preserves_settings_override(appium_start_request):
    _user, _project, _package, device, _token, _client = appium_start_request
    configured = '{"appium-endpoint-serial":"http://127.0.0.1:4723"}'

    with override_settings(APP_APPIUM_ENDPOINTS=None, APP_APPIUM_ENDPOINTS_JSON=configured):
        assert endpoint_for_device(device) == "http://127.0.0.1:4723"

    with override_settings(
        APP_APPIUM_ENDPOINTS={str(device.pk): "http://[::1]:4724"},
        APP_APPIUM_ENDPOINTS_JSON="not-json",
    ):
        assert endpoint_for_device(device) == "http://[::1]:4724"


def test_invalid_or_missing_endpoint_map_fails_before_session_is_queued(appium_start_request):
    user, project, package, device, lease_token, client = appium_start_request
    url = reverse("appium-session-start", args=[device.pk])
    payload = {
        "expected_user_id": user.pk,
        "lease_token": lease_token,
        "idempotency_key": "9b09da54-6f8e-49d7-b09f-b83ea8f72198",
        "project_id": project.pk,
        "app_package_id": package.pk,
    }

    for raw in ("", "not-json", '{"appium-endpoint-serial":"http://192.0.2.5:4723"}'):
        with override_settings(APP_APPIUM_ENDPOINTS=None, APP_APPIUM_ENDPOINTS_JSON=raw):
            response = client.post(url, payload, format="json")
        assert response.status_code == 503
        assert not AppiumSession.objects.filter(device=device).exists()
        assert not AppiumOperation.objects.exists()


def _admin_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def appium_history_device(db):
    admin = User.objects.create_user(username="appium-history-admin", is_staff=True)
    project = AppProject.objects.create(name="History project", owner=admin)
    package = AppPackage.objects.create(
        name="History app", package_name="org.example.history", created_by=admin,
    )
    device = AppDevice.objects.create(
        name="History device", device_id="appium-history-serial", status="available",
        device_action_pending=False,
    )
    session = AppiumSession.objects.create(
        kind="interactive",
        device=device,
        user=admin,
        project=project,
        package=package,
        lease_generation=1,
        device_action_epoch=1,
        system_port=8201,
        appium_endpoint="http://127.0.0.1:4723",
        remote_session_id="historical-remote-session",
        status="closed",
        request_key="terminal-history-key",
        closed_at=timezone.now(),
    )
    operation = AppiumOperation.objects.create(
        session=session,
        user=admin,
        lease_generation=1,
        device_action_epoch=1,
        action="close",
        status="succeeded",
        request_key="terminal-close-key",
        finished_at=timezone.now(),
    )
    return admin, device, session, operation


@pytest.mark.django_db(transaction=True)
def test_admin_can_delete_idle_device_without_losing_terminal_appium_history(appium_history_device):
    admin, device, session, operation = appium_history_device

    response = _admin_client(admin).delete(reverse("app-device-detail", args=[device.pk]))

    assert response.status_code == 204
    assert not AppDevice.objects.filter(pk=device.pk).exists()
    session.refresh_from_db()
    operation.refresh_from_db()
    assert session.device_id is None
    assert session.status == "closed"
    assert session.remote_session_id == "historical-remote-session"
    assert operation.session_id == session.pk
    assert operation.status == "succeeded"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('execution_status', ('pending', 'running'))
def test_admin_cannot_delete_device_while_an_execution_still_references_it(appium_history_device, execution_status):
    admin, device, session, _operation = appium_history_device
    device, _ = acquire_specific_device(device.pk, admin, lease_seconds=90)
    case = AppTestCase.objects.create(
        project=session.project, app_package=session.package, created_by=admin,
        name='Appium case awaiting device use', engine='appium',
    )
    execution = AppTestExecution.objects.create(
        test_case=case, device=device, user=admin, status=execution_status,
        device_lease_generation=device.lease_generation, device_action_pending=False,
    )

    response = _admin_client(admin).delete(reverse('app-device-detail', args=[device.pk]))

    assert response.status_code == 409
    assert '待执行或正在执行' in response.data['detail']
    execution.refresh_from_db()
    assert execution.device_id == device.pk
    assert execution.status == execution_status


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("session_status", ["starting", "active", "closing", "stopping", "unknown"])
def test_admin_cannot_delete_device_with_nonterminal_appium_session_even_without_pending_marker(
    appium_history_device, session_status,
):
    admin, device, session, _operation = appium_history_device
    session.status = session_status
    session.closed_at = None
    session.save(update_fields=["status", "closed_at", "updated_at"])
    AppDevice.objects.filter(pk=device.pk).update(device_action_pending=False)

    response = _admin_client(admin).delete(reverse("app-device-detail", args=[device.pk]))

    assert response.status_code == 409
    assert AppDevice.objects.filter(pk=device.pk).exists()
    session.refresh_from_db()
    assert session.device_id == device.pk
    assert session.status == session_status


def test_endpoint_map_rejects_non_loopback_targets(appium_start_request):
    _user, _project, _package, device, _token, _client = appium_start_request

    with override_settings(
        APP_APPIUM_ENDPOINTS=None,
        APP_APPIUM_ENDPOINTS_JSON='{"appium-endpoint-serial":"http://192.0.2.5:4723"}',
    ):
        with pytest.raises(AppiumConfigurationError):
            endpoint_for_device(device)
