from types import SimpleNamespace
from unittest.mock import Mock
import subprocess

import pytest

from apps.app_automation.device_actions import (
    DeviceActionError, DeviceActionUncertainError, clear_focused_input,
    handle_permission, hide_keyboard, restart_application, send_key,
    start_application, stop_application,
)


class FakeADB:
    def __init__(self, response='', failures=None):
        self.calls = []
        self.response = response
        self.failures = failures or {}

    def cmd(self, argv, timeout=None):
        self.calls.append((argv, timeout))
        if len(self.calls) in self.failures:
            raise self.failures[len(self.calls)]
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def fake_device(response='', failures=None):
    return SimpleNamespace(adb=FakeADB(response, failures), touch=Mock())


@pytest.mark.parametrize(('key', 'code'), [
    ('BACK', 'KEYCODE_BACK'), ('HOME', 'KEYCODE_HOME'), ('ENTER', 'KEYCODE_ENTER'),
    ('DELETE', 'KEYCODE_DEL'), ('ESCAPE', 'KEYCODE_ESCAPE'),
])
def test_key_events_are_fixed_and_bounded(key, code):
    device = fake_device()
    send_key(device, key)
    assert device.adb.calls == [(['shell', 'input', 'keyevent', code], 5)]


def test_arbitrary_key_and_package_shell_metacharacters_are_rejected():
    device = fake_device()
    with pytest.raises(DeviceActionError) as key_error:
        send_key(device, 'BACK; id')
    assert type(key_error.value) is DeviceActionError
    with pytest.raises(DeviceActionError) as package_error:
        start_application(device, 'org.example;reboot')
    assert type(package_error.value) is DeviceActionError
    assert device.adb.calls == []


def test_app_lifecycle_uses_validated_package_and_fixed_commands():
    device = fake_device()
    restart_application(device, 'org.example.app')
    assert device.adb.calls == [
        (['shell', 'am', 'force-stop', 'org.example.app'], 10),
        (['shell', 'monkey', '-p', 'org.example.app', '-c', 'android.intent.category.LAUNCHER', '1'], 15),
    ]
    stop_application(device, 'org.example.app')
    assert device.adb.calls[-1][0] == ['shell', 'am', 'force-stop', 'org.example.app']


def test_failed_application_launch_and_adb_errors_do_not_echo_sensitive_output():
    with pytest.raises(DeviceActionError, match='无法启动应用') as launch_error:
        start_application(fake_device('No activities found to run'), 'org.example.app')
    assert type(launch_error.value) is DeviceActionError

    error = subprocess.TimeoutExpired(
        cmd=['adb', '-s', 'DEVICE-ID-SECRET', 'shell', 'input', 'keyevent', 'private-command-value'],
        timeout=5,
        output='private-command-value',
    )
    with pytest.raises(DeviceActionUncertainError) as action_error:
        send_key(fake_device(error), 'HOME')
    assert 'DEVICE-ID-SECRET' not in str(action_error.value)
    assert 'private-command-value' not in str(action_error.value)
    assert action_error.value.__cause__ is None


def test_hide_keyboard_only_sends_back_when_visible():
    shown = fake_device('mInputShown=true')
    assert hide_keyboard(shown) is True
    assert shown.adb.calls[-1][0] == ['shell', 'input', 'keyevent', 'KEYCODE_BACK']
    hidden = fake_device('mInputShown=false')
    assert hide_keyboard(hidden) is False
    assert len(hidden.adb.calls) == 1
    unknown = fake_device('unsupported output')
    with pytest.raises(DeviceActionError, match='无法确认键盘状态'):
        hide_keyboard(unknown)
    assert len(unknown.adb.calls) == 1


def test_clear_focused_input_uses_select_all_then_delete():
    device = fake_device()
    clear_focused_input(device)
    assert device.adb.calls == [
        (['shell', 'input', 'keycombination', 'KEYCODE_CTRL_LEFT', 'KEYCODE_A'], 5),
        (['shell', 'input', 'keyevent', 'KEYCODE_DEL'], 5),
    ]


@pytest.mark.parametrize('failure_kind', ['timeout', 'transport'])
@pytest.mark.parametrize('action', ['key', 'clear', 'start', 'stop'])
def test_mutating_adb_failures_have_uncertain_sanitized_result(action, failure_kind):
    if failure_kind == 'timeout':
        failure = subprocess.TimeoutExpired(
            cmd=['adb', '-s', 'DEVICE-ID-SECRET', 'shell', 'monkey', '-p', 'private-command-value'],
            timeout=5,
        )
    else:
        failure = ConnectionError('transport failed for DEVICE-ID-SECRET private-command-value')
    device = fake_device(failures={1: failure})
    operations = {
        'key': lambda: send_key(device, 'HOME'),
        'clear': lambda: clear_focused_input(device),
        'start': lambda: start_application(device, 'org.example.app'),
        'stop': lambda: stop_application(device, 'org.example.app'),
    }

    with pytest.raises(DeviceActionUncertainError) as error:
        operations[action]()

    assert type(error.value) is DeviceActionUncertainError
    assert 'DEVICE-ID-SECRET' not in str(error.value)
    assert 'private-command-value' not in str(error.value)
    assert device.adb.calls[0][0][0] == 'shell'
    if action == 'clear':
        assert len(device.adb.calls) == 1
        assert device.adb.calls[0][0][1:3] == ['input', 'keycombination']


@pytest.mark.parametrize('failed_command', [1, 2])
def test_clear_focused_input_reports_uncertain_failure_at_the_failed_command(failed_command):
    device = fake_device(failures={failed_command: ConnectionError('private transport detail')})

    with pytest.raises(DeviceActionUncertainError):
        clear_focused_input(device)

    assert len(device.adb.calls) == failed_command
    if failed_command == 1:
        assert device.adb.calls[0][0][1:3] == ['input', 'keycombination']
    else:
        assert device.adb.calls[1][0][1:3] == ['input', 'keyevent']


@pytest.mark.parametrize('failed_call', [1, 2])
def test_restart_reports_uncertain_failure_at_each_mutation_stage(failed_call):
    device = fake_device(failures={failed_call: ConnectionError('private transport detail')})

    with pytest.raises(DeviceActionUncertainError) as error:
        restart_application(device, 'org.example.app')

    assert type(error.value) is DeviceActionUncertainError
    assert 'private transport detail' not in str(error.value)
    assert len(device.adb.calls) == failed_call
    assert device.adb.calls[0][0][1:3] == ['am', 'force-stop']
    if failed_call == 2:
        assert device.adb.calls[1][0][1] == 'monkey'


def test_keyboard_state_query_transport_failure_remains_ordinary_error():
    failure = subprocess.TimeoutExpired(
        cmd=['adb', '-s', 'DEVICE-ID-SECRET', 'shell', 'dumpsys', 'input_method'], timeout=5,
    )
    device = fake_device(failure)

    with pytest.raises(DeviceActionError) as error:
        hide_keyboard(device)

    assert type(error.value) is DeviceActionError
    assert not isinstance(error.value, DeviceActionUncertainError)
    assert 'DEVICE-ID-SECRET' not in str(error.value)
    assert len(device.adb.calls) == 1


def test_permission_finder_failure_is_ordinary_but_touch_failure_is_uncertain():
    def fail_finder(*args, **kwargs):
        raise ConnectionError('finder failed for DEVICE-ID-SECRET')

    device = fake_device()
    with pytest.raises(DeviceActionError) as finder_error:
        handle_permission(device, 'allow', finder=fail_finder)
    assert type(finder_error.value) is DeviceActionError
    assert 'DEVICE-ID-SECRET' not in str(finder_error.value)
    device.touch.assert_not_called()

    target = SimpleNamespace(
        visible=True,
        enabled=True,
        center=(12, 34),
        resource_id='com.android.permissioncontroller:id/permission_allow_button',
    )
    device.touch.side_effect = ConnectionError('touch failed for DEVICE-ID-SECRET')
    with pytest.raises(DeviceActionUncertainError) as touch_error:
        handle_permission(device, 'allow', finder=lambda *args, **kwargs: target)
    assert type(touch_error.value) is DeviceActionUncertainError
    assert 'DEVICE-ID-SECRET' not in str(touch_error.value)


def test_permission_action_clicks_only_explicit_system_button():
    hierarchy = '''<hierarchy>
      <node resource-id="malicious.app:id/permission_allow_button" bounds="[0,0][10,10]" />
      <node resource-id="com.android.permissioncontroller:id/permission_allow_button"
            bounds="[0,0][10,10]" enabled="false" />
      <node resource-id="com.google.android.permissioncontroller:id/permission_allow_foreground_only_button"
            bounds="[10,20][30,50]" enabled="true" />
    </hierarchy>'''
    device = fake_device(hierarchy)
    assert handle_permission(device, 'allow', timeout=1) is True
    device.touch.assert_called_once_with((20, 35))
    assert len(device.adb.calls) == 1
    assert device.adb.calls[0][0] == ['shell', 'uiautomator', 'dump', '/dev/tty']


def test_permission_rejects_forged_result_even_when_finder_returns_one():
    device = fake_device()
    target = SimpleNamespace(visible=True, enabled=True, center=(12, 34),
                             resource_id='malicious.app:id/permission_allow_button')
    assert handle_permission(device, 'allow', timeout=1, finder=lambda *a, **k: target) is False
    device.touch.assert_not_called()


def test_permission_action_optional_and_required_missing_behavior():
    device = fake_device()
    assert handle_permission(device, 'deny', timeout=0, optional=True, finder=lambda *a, **k: None) is False
    with pytest.raises(DeviceActionError, match='未找到'):
        handle_permission(device, 'deny', timeout=0, optional=False, finder=lambda *a, **k: None)
    with pytest.raises(DeviceActionError):
        handle_permission(device, 'always_allow', timeout=0, finder=lambda *a, **k: None)
    device.touch.assert_not_called()
