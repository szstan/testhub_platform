"""Bounded, allowlisted Android actions for APP automation components."""
from __future__ import annotations

import math
import re
import time


class DeviceActionError(RuntimeError):
    """A requested Android action could not be performed safely."""


class DeviceActionUncertainError(DeviceActionError):
    """A device mutation may have started, but its completion is unknown."""


KEY_EVENTS = {
    'BACK': 'KEYCODE_BACK',
    'HOME': 'KEYCODE_HOME',
    'ENTER': 'KEYCODE_ENTER',
    'TAB': 'KEYCODE_TAB',
    'DELETE': 'KEYCODE_DEL',
    'ESCAPE': 'KEYCODE_ESCAPE',
    'APP_SWITCH': 'KEYCODE_APP_SWITCH',
    'VOLUME_UP': 'KEYCODE_VOLUME_UP',
    'VOLUME_DOWN': 'KEYCODE_VOLUME_DOWN',
}

_PACKAGE_RE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+')
_KEYBOARD_PATTERNS = (
    re.compile(r'\bmInputShown\s*[=:]\s*(true|false)\b', re.I),
    re.compile(r'\bmIsInputViewShown\s*[=:]\s*(true|false)\b', re.I),
    re.compile(r'\bisInputViewShown\s*[=:]\s*(true|false)\b', re.I),
)
_PERMISSION_SUFFIXES = {
    'allow': ('permission_allow_button', 'permission_allow_foreground_only_button'),
    'deny': ('permission_deny_button',),
    'while_in_use': ('permission_allow_foreground_only_button',),
}
_PERMISSION_PACKAGES = ('com.android.permissioncontroller', 'com.google.android.permissioncontroller', 'com.android.packageinstaller')
_MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024
_MAX_SCREENSHOT_TIMEOUT = 30.0
_ZERO_TIMEOUT_PERMISSION_PROBE_TIMEOUT = 2.0


def _adb(device):
    adb = getattr(device, 'adb', None)
    if not callable(getattr(adb, 'cmd', None)):
        raise DeviceActionError('当前设备不支持有超时限制的 Android 操作')
    return adb


def _command(device, arguments, *, timeout=10, mutation=False):
    """Run internally constructed argv; never log command output or input text."""
    try:
        adb = _adb(device)
    except DeviceActionError:
        raise
    except Exception:
        raise DeviceActionError('当前设备不支持有超时限制的 Android 操作') from None
    try:
        result = adb.cmd(['shell', *arguments], timeout=timeout)
    except Exception:
        if mutation:
            raise DeviceActionUncertainError('Android 设备操作结果未知，请检查设备状态') from None
        raise DeviceActionError('Android 设备操作失败或超时，请检查设备连接') from None
    if isinstance(result, bytes):
        return result.decode('utf-8', errors='replace')
    return str(result or '')


def capture_screenshot(device, *, timeout=2.0):
    """Capture PNG bytes through a timed, read-only ADB screencap command."""
    if (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout) or not 0 < timeout <= _MAX_SCREENSHOT_TIMEOUT
    ):
        raise DeviceActionError('Android screenshot timeout must be greater than 0 and at most 30 seconds')

    adb = _adb(device)
    arguments = ['shell', 'screencap']
    display_id = getattr(device, 'display_id', None)
    if display_id is not None:
        display_id = str(display_id)
        if not display_id.isascii() or not display_id.isdecimal():
            raise DeviceActionError('Android display identifier is invalid')
        arguments.extend(['-d', display_id])
    arguments.append('-p')
    deadline = time.monotonic() + float(timeout)
    try:
        image = adb.cmd(arguments, timeout=float(timeout), ensure_unicode=False)
    except Exception:
        raise DeviceActionError('Android screenshot failed or timed out; check the device connection') from None
    if time.monotonic() >= deadline:
        raise DeviceActionError('Android screenshot timed out; check the device connection')
    if not isinstance(image, (bytes, bytearray, memoryview)):
        raise DeviceActionError('Android screenshot returned invalid image data')
    image = bytes(image)
    if not image or len(image) > _MAX_SCREENSHOT_BYTES:
        raise DeviceActionError('Android screenshot returned invalid image data')
    return image


def validate_package_name(package_name):
    value = str(package_name or '').strip()
    if len(value) > 255 or not _PACKAGE_RE.fullmatch(value):
        raise DeviceActionError('请提供有效的 Android 应用包名')
    return value


def send_key(device, key):
    name = str(key or '').upper()
    if name not in KEY_EVENTS:
        raise DeviceActionError('不支持的 Android 系统按键')
    _command(device, ['input', 'keyevent', KEY_EVENTS[name]], timeout=5, mutation=True)


def start_application(device, package_name):
    package_name = validate_package_name(package_name)
    output = _command(device, ['monkey', '-p', package_name, '-c', 'android.intent.category.LAUNCHER', '1'], timeout=15, mutation=True)
    if any(term in output.lower() for term in ('no activities found', 'aborted', 'error:')):
        raise DeviceActionError('无法启动应用，请检查包名和设备状态')


def stop_application(device, package_name):
    package_name = validate_package_name(package_name)
    _command(device, ['am', 'force-stop', package_name], timeout=10, mutation=True)


def restart_application(device, package_name):
    package_name = validate_package_name(package_name)
    stop_application(device, package_name)
    start_application(device, package_name)


def hide_keyboard(device):
    """Return False if already hidden; never press Back when state is unknown."""
    output = _command(device, ['dumpsys', 'input_method'], timeout=5)
    for pattern in _KEYBOARD_PATTERNS:
        match = pattern.search(output)
        if match:
            if match.group(1).lower() == 'true':
                send_key(device, 'BACK')
                return True
            return False
    raise DeviceActionError('无法确认键盘状态，未发送返回键')


def clear_focused_input(device):
    """Select all and delete; unsupported Android versions fail without fallback typing."""
    _command(device, ['input', 'keycombination', 'KEYCODE_CTRL_LEFT', 'KEYCODE_A'], timeout=5, mutation=True)
    send_key(device, 'DELETE')


def handle_permission(device, choice, *, timeout=5, optional=True, finder=None):
    """Click only known Android system permission buttons chosen by the caller."""
    if choice not in _PERMISSION_SUFFIXES or not isinstance(optional, bool):
        raise DeviceActionError('权限弹窗处理配置无效')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
        raise DeviceActionError('权限弹窗等待时间必须在 0 到 60 秒之间')
    adb = _adb(device)
    if finder is None:
        from .native_locator import find_native_element_any, probe_native_element_any
        finder = probe_native_element_any if timeout == 0 else find_native_element_any
    if not callable(getattr(device, 'touch', None)):
        raise DeviceActionError('当前设备不支持点击操作')
    identifiers = [f'{package}:id/{suffix}' for package in _PERMISSION_PACKAGES for suffix in _PERMISSION_SUFFIXES[choice]]
    probe_timeout = _ZERO_TIMEOUT_PERMISSION_PROBE_TIMEOUT if timeout == 0 else timeout
    try:
        match = finder(adb, 'resource_id', identifiers, timeout=probe_timeout, interval=0.5,
                       match='exact', active_only=True)
    except Exception:
        raise DeviceActionError('无法读取系统权限弹窗，请检查设备连接') from None
    if match is not None and match.visible and match.enabled and match.resource_id in identifiers:
        try:
            device.touch(match.center)
        except Exception:
            raise DeviceActionUncertainError('无法确认系统权限弹窗操作结果，请检查设备状态') from None
        return True
    if optional:
        return False
    raise DeviceActionError('在指定时间内未找到对应的系统权限弹窗')
