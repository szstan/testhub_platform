import subprocess
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.device_pool import DeviceLeaseError
from apps.app_automation.models import AppDevice, AppTestExecution
from apps.app_automation.views.recorder_views import (
    ADB_TIMEOUT_SECONDS,
    RecorderPayloadError,
    RecordInteractionView,
    _parse_interaction,
)


@pytest.fixture
def recorder_device(db):
    user = get_user_model().objects.create_user(username='recorder-owner')
    device = AppDevice.objects.create(device_id='recorder-fixture-serial', status='online')
    device.lock(user)
    return user, device


def post_interaction(user, device, payload):
    payload = dict(payload)
    payload.setdefault('expected_user_id', user.pk if user is not None else 1)
    request = APIRequestFactory().post(
        f'/api/app-automation/devices/{device.pk}/record-interaction/',
        payload,
        format='json',
    )
    if user is not None:
        force_authenticate(request, user=user)
    return RecordInteractionView.as_view()(request, device_id=device.pk)


def patch_valid_lease(device):
    return patch(
        'apps.app_automation.views.recorder_views.validate_device_lease',
        return_value=device,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(('payload', 'adb_args'), [
    (
        {'action': 'tap', 'x': 12, 'y': 34, 'lease_token': 'fixture-token'},
        ['shell', 'input', 'tap', '12', '34'],
    ),
    (
        {'action': 'swipe', 'x1': 10, 'y1': 20, 'x2': 30, 'y2': 40,
         'duration_ms': 450, 'lease_token': 'fixture-token'},
        ['shell', 'input', 'swipe', '10', '20', '30', '40', '450'],
    ),
    ({'action': 'key', 'key': 'back', 'lease_token': 'fixture-token'},
     ['shell', 'input', 'keyevent', 'KEYCODE_BACK']),
    ({'action': 'key', 'key': 'home', 'lease_token': 'fixture-token'},
     ['shell', 'input', 'keyevent', 'KEYCODE_HOME']),
    ({'action': 'key', 'key': 'enter', 'lease_token': 'fixture-token'},
     ['shell', 'input', 'keyevent', 'KEYCODE_ENTER']),
])
def test_allowlisted_interactions_use_fixed_adb_arguments_and_validate_lease(
    recorder_device, payload, adb_args,
):
    user, device = recorder_device
    with (
        patch_valid_lease(device) as validate_lease,
        patch('apps.app_automation.views.recorder_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.recorder_views.subprocess.run') as run,
    ):
        response = post_interaction(user, device, payload)

    assert response.status_code == 200
    assert response.data == {'success': True}
    validate_lease.assert_called_once_with(device.pk, user, 'fixture-token')
    assert run.call_args.args[0] == [
        '/fake/adb', '-s', device.device_id, *adb_args,
    ]
    assert run.call_args.kwargs == {
        'check': True,
        'timeout': ADB_TIMEOUT_SECONDS,
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
        'shell': False,
    }


@pytest.mark.django_db
@pytest.mark.parametrize('execution_status', ['pending', 'running'])
def test_active_execution_blocks_interaction(recorder_device, execution_status):
    user, device = recorder_device
    AppTestExecution.objects.create(device=device, user=user, status=execution_status)

    with (
        patch_valid_lease(device),
        patch('apps.app_automation.views.recorder_views.subprocess.run') as run,
    ):
        response = post_interaction(
            user, device, {'action': 'tap', 'x': 10, 'y': 20, 'lease_token': 'fixture-token'},
        )

    assert response.status_code == 409
    run.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize('payload', [
    {'action': 'shell', 'command': 'input tap 1 2', 'lease_token': 'fixture-token'},
    {'action': 'tap', 'x': True, 'y': 1, 'lease_token': 'fixture-token'},
    {'action': 'tap', 'x': 1.5, 'y': 1, 'lease_token': 'fixture-token'},
    {'action': 'tap', 'x': 100_001, 'y': 1, 'lease_token': 'fixture-token'},
    {'action': 'swipe', 'x1': 1, 'y1': 2, 'x2': 3, 'y2': 4,
     'duration_ms': 1_501, 'lease_token': 'fixture-token'},
    {'action': 'key', 'key': 'power', 'lease_token': 'fixture-token'},
    {'action': 'key', 'key': 'enter', 'text': 'private fixture text',
     'lease_token': 'fixture-token'},
])
def test_invalid_payloads_are_rejected_without_lease_or_adb_calls(recorder_device, payload):
    user, device = recorder_device
    with (
        patch('apps.app_automation.views.recorder_views.validate_device_lease') as validate_lease,
        patch('apps.app_automation.views.recorder_views.subprocess.run') as run,
    ):
        response = post_interaction(user, device, payload)

    assert response.status_code == 400
    assert 'private fixture text' not in str(response.data)
    validate_lease.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_non_finite_coordinates_are_rejected(value):
    with pytest.raises(RecorderPayloadError):
        _parse_interaction({'action': 'tap', 'x': value, 'y': 0, 'expected_user_id': 1})



@pytest.mark.django_db
@pytest.mark.parametrize('has_authentication', [False, True])
def test_authentication_and_lease_are_required(recorder_device, has_authentication):
    user, device = recorder_device
    request_user = user if has_authentication else None
    with (
        patch('apps.app_automation.views.recorder_views.validate_device_lease',
              side_effect=DeviceLeaseError('fixture lease rejection')) as validate_lease,
        patch('apps.app_automation.views.recorder_views.subprocess.run') as run,
    ):
        response = post_interaction(
            request_user, device,
            {'action': 'tap', 'x': 1, 'y': 2, 'lease_token': 'wrong-token'},
        )

    if has_authentication:
        assert response.status_code == 403
        validate_lease.assert_called_once_with(device.pk, user, 'wrong-token')
    else:
        assert response.status_code in (401, 403)
        validate_lease.assert_not_called()
    run.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('failure', 'expected_status', 'expected_detail'),
    [
        (subprocess.TimeoutExpired(['/private/serial'], 8), 504,
         '设备交互超时，设备已进入维护模式，请管理员确认设备状态'),
        (subprocess.CalledProcessError(1, ['/private/serial'], stderr='private fixture detail'),
         502, '设备交互状态未确认，设备已进入维护模式，请管理员检查'),
        (FileNotFoundError('/private/adb/path'), 503, 'ADB 服务不可用'),
    ],
)
def test_adb_failures_return_generic_errors_without_logging_or_echoing_details(
    recorder_device, caplog, failure, expected_status, expected_detail,
):
    user, device = recorder_device
    with (
        patch_valid_lease(device),
        patch('apps.app_automation.views.recorder_views.get_adb_path', return_value='/private/adb/path'),
        patch('apps.app_automation.views.recorder_views.subprocess.run', side_effect=failure),
    ):
        response = post_interaction(
            user, device,
            {'action': 'tap', 'x': 1, 'y': 2, 'lease_token': 'private fixture token'},
        )

    assert response.status_code == expected_status
    assert response.data == {'detail': expected_detail}
    assert 'private fixture' not in str(response.data)
    assert device.device_id not in str(response.data)
    assert '/private/' not in str(response.data)
    assert caplog.text == ''
    device.refresh_from_db()
    assert device.maintenance_mode is (expected_status in (502, 504))
    assert device.device_action_pending is False


def test_recorder_requires_an_explicit_account_fence():
    with pytest.raises(RecorderPayloadError):
        _parse_interaction({'action': 'tap', 'x': 1, 'y': 2})


@pytest.mark.django_db
def test_unavailable_device_is_not_exposed_to_adb():
    user = get_user_model().objects.create_user(username='recorder-missing-device')
    request = APIRequestFactory().post(
        '/api/app-automation/devices/999999/record-interaction/',
        {'action': 'tap', 'x': 1, 'y': 2, 'expected_user_id': user.pk},
        format='json',
    )
    force_authenticate(request, user=user)
    with patch('apps.app_automation.views.recorder_views.subprocess.run') as run:
        response = RecordInteractionView.as_view()(request, device_id=999999)

    assert response.status_code == 404
    run.assert_not_called()


@pytest.mark.django_db
def test_account_switch_is_rejected_before_lease_or_device_access(recorder_device):
    user, device = recorder_device
    with (
        patch('apps.app_automation.views.recorder_views.validate_device_lease') as validate,
        patch('apps.app_automation.views.recorder_views.subprocess.run') as run,
    ):
        response = post_interaction(user, device, {
            'action': 'tap', 'x': 1, 'y': 2, 'expected_user_id': user.pk + 1,
        })
    assert response.status_code == 409
    validate.assert_not_called()
    run.assert_not_called()


@pytest.mark.django_db
def test_timeout_quarantines_device_and_blocks_even_the_existing_owner(recorder_device):
    user, device = recorder_device
    with (
        patch('apps.app_automation.views.recorder_views.get_adb_path', return_value='/fake/adb'),
        patch('apps.app_automation.views.recorder_views.subprocess.run',
              side_effect=subprocess.TimeoutExpired('/fake/adb', 8)),
    ):
        response = post_interaction(user, device, {'action': 'tap', 'x': 1, 'y': 2})
    assert response.status_code == 504
    device.refresh_from_db()
    assert device.maintenance_mode is True
    with patch('apps.app_automation.views.recorder_views.subprocess.run') as run:
        second = post_interaction(user, device, {'action': 'tap', 'x': 1, 'y': 2})
    assert second.status_code == 403
    run.assert_not_called()
