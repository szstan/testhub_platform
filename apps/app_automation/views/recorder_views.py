"""Bounded, opt-in Android device interactions for the APP recorder."""

import subprocess

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..device_pool import (
    DeviceActionBusy,
    DeviceLeaseError,
    begin_device_action,
    finish_device_action,
    validate_device_lease,
)
from ..execution_dispatch import execution_user_matches
from ..models import AppDevice
from .device_views import get_adb_path

ADB_TIMEOUT_SECONDS = 8
MAX_COORDINATE = 100_000
MIN_SWIPE_DURATION_MS = 1
MAX_SWIPE_DURATION_MS = 1_500
DEFAULT_SWIPE_DURATION_MS = 300
KEY_EVENTS = {
    'back': 'KEYCODE_BACK',
    'home': 'KEYCODE_HOME',
    'enter': 'KEYCODE_ENTER',
}


class RecorderPayloadError(ValueError):
    """Raised for interaction payloads outside the recorder allowlist."""


def _require_integer(payload, field, *, minimum=0, maximum=MAX_COORDINATE):
    value = payload.get(field)
    if type(value) is not int or value < minimum or value > maximum:
        raise RecorderPayloadError
    return value


def _reject_unknown_fields(payload, allowed_fields):
    if set(payload) - allowed_fields:
        raise RecorderPayloadError


def _parse_interaction(payload):
    if not isinstance(payload, dict):
        raise RecorderPayloadError

    lease_token = payload.get('lease_token', '')
    expected_user_id = payload.get('expected_user_id')
    if not isinstance(lease_token, str) or len(lease_token) > 1024:
        raise RecorderPayloadError
    if (type(expected_user_id) not in (int, str) or not str(expected_user_id).isascii()
            or not str(expected_user_id).isdigit() or len(str(expected_user_id)) > 20):
        raise RecorderPayloadError

    action = payload.get('action')
    if action == 'tap':
        _reject_unknown_fields(payload, {'action', 'lease_token', 'expected_user_id', 'x', 'y'})
        x = _require_integer(payload, 'x')
        y = _require_integer(payload, 'y')
        command = ['shell', 'input', 'tap', str(x), str(y)]
    elif action == 'swipe':
        _reject_unknown_fields(
            payload,
            {'action', 'lease_token', 'expected_user_id', 'x1', 'y1', 'x2', 'y2', 'duration_ms'},
        )
        x1 = _require_integer(payload, 'x1')
        y1 = _require_integer(payload, 'y1')
        x2 = _require_integer(payload, 'x2')
        y2 = _require_integer(payload, 'y2')
        duration_ms = payload.get('duration_ms', DEFAULT_SWIPE_DURATION_MS)
        if (
            type(duration_ms) is not int
            or duration_ms < MIN_SWIPE_DURATION_MS
            or duration_ms > MAX_SWIPE_DURATION_MS
        ):
            raise RecorderPayloadError
        command = [
            'shell', 'input', 'swipe', str(x1), str(y1), str(x2), str(y2),
            str(duration_ms),
        ]
    elif action == 'key':
        _reject_unknown_fields(payload, {'action', 'lease_token', 'expected_user_id', 'key'})
        key_event = KEY_EVENTS.get(payload.get('key'))
        if key_event is None:
            raise RecorderPayloadError
        command = ['shell', 'input', 'keyevent', key_event]
    else:
        raise RecorderPayloadError

    return lease_token, command, expected_user_id


def _quarantine_uncertain_device(device_id, user, generation):
    """Commit maintenance quarantine before clearing the device-level fence."""
    try:
        finish_device_action(device_id, user, generation, quarantine=True)
        return True
    except Exception:
        return False


class RecordInteractionView(APIView):
    """Perform one allowlisted interaction with a committed device fence."""

    permission_classes = [IsAuthenticated]

    def post(self, request, device_id):
        try:
            lease_token, interaction_command, expected_user_id = _parse_interaction(request.data)
        except RecorderPayloadError:
            return Response(
                {'detail': '交互参数无效，仅支持点击、滑动和返回/主页/确认键'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not execution_user_matches(request.user, expected_user_id):
            return Response(
                {'detail': '登录账号已变化，请重新打开录制器'},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            adb_path = get_adb_path()
        except Exception:
            return Response({'detail': 'ADB 服务不可用'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        try:
            device_serial, generation = begin_device_action(
                device_id,
                request.user,
                lease_token,
                lease_validator=validate_device_lease,
            )
        except AppDevice.DoesNotExist:
            return Response({'detail': '设备不存在'}, status=status.HTTP_404_NOT_FOUND)
        except DeviceActionBusy:
            return Response(
                {'detail': '设备存在待执行或待安全恢复的动作'},
                status=status.HTTP_409_CONFLICT,
            )
        except DeviceLeaseError:
            return Response(
                {'detail': '设备租约无效、已过期或不属于当前用户'},
                status=status.HTTP_403_FORBIDDEN,
            )
        except Exception:
            return Response({'detail': '设备交互暂不可用'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        command = [adb_path, '-s', str(device_serial), *interaction_command]
        try:
            subprocess.run(
                command,
                check=True,
                timeout=ADB_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            quarantined = _quarantine_uncertain_device(device_id, request.user, generation)
            detail = (
                '设备交互超时，设备已进入维护模式，请管理员确认设备状态'
                if quarantined else '设备交互超时，设备占用已保留，请管理员恢复设备'
            )
            return Response({'detail': detail}, status=status.HTTP_504_GATEWAY_TIMEOUT)
        except (FileNotFoundError, PermissionError):
            try:
                finish_device_action(device_id, request.user, generation)
            except Exception:
                detail = 'ADB 服务不可用，设备占用已保留，请管理员恢复设备'
            else:
                detail = 'ADB 服务不可用'
            return Response({'detail': detail}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception:
            quarantined = _quarantine_uncertain_device(device_id, request.user, generation)
            detail = (
                '设备交互状态未确认，设备已进入维护模式，请管理员检查'
                if quarantined else '设备交互状态未确认，设备占用已保留，请管理员恢复设备'
            )
            return Response({'detail': detail}, status=status.HTTP_502_BAD_GATEWAY)

        try:
            finish_device_action(device_id, request.user, generation)
        except Exception:
            return Response(
                {'detail': '设备交互已完成但状态未能安全确认，设备占用已保留，请管理员恢复设备'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({'success': True}, status=status.HTTP_200_OK)
