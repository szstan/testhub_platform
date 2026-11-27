import logging
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.app_automation import secret_input
from apps.app_automation.secret_input import (
    SecretInputDeviceError,
    SecretInputError,
    SecretInputValidationError,
    enter_sensitive_text,
)


class FakeADB:
    def __init__(self, *, adb_path="/opt/android/platform-tools/adb", serialno="ZX-1", host="127.0.0.1", port=5037):
        self.adb_path = adb_path
        self.serialno = serialno
        self.host = host
        self.port = port
        self.command_calls = []

    def cmd(self, *args, **kwargs):
        self.command_calls.append((args, kwargs))
        raise AssertionError("secret_input must not use Airtest ADB.cmd")


def fake_device(**adb_values):
    return SimpleNamespace(adb=FakeADB(**adb_values))


def test_sends_secret_only_on_stdin_with_fixed_bounded_argv(monkeypatch):
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:192.0.2.99:5037")
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5555")
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(secret_input.subprocess, "run", run)
    device = fake_device()
    value = "Pa ss9"

    enter_sensitive_text(device, value)

    argv = run.call_args.args[0]
    options = run.call_args.kwargs
    assert argv == [
        "/opt/android/platform-tools/adb",
        "-H",
        "127.0.0.1",
        "-P",
        "5037",
        "-s",
        "ZX-1",
        "shell",
        "sh -c " + secret_input.shlex.quote(secret_input._REMOTE_SCRIPT),
    ]
    assert value not in " ".join(argv)
    assert options["input"] == b"P\na\n \ns\ns\n9\n__PI_SECRET_INPUT_END__\n"
    assert options["timeout"] == 30
    assert options["shell"] is False
    assert options["check"] is False
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL
    assert "ADB_SERVER_SOCKET" not in options["env"]
    assert "ANDROID_ADB_SERVER_PORT" not in options["env"]
    assert device.adb.command_calls == []


def test_secret_changes_cannot_change_the_remote_command_or_options(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(secret_input.subprocess, "run", run)
    device = fake_device(port=5040)

    enter_sensitive_text(device, "Alpha123")
    first = run.call_args_list[-1]
    enter_sensitive_text(device, "Beta456")
    second = run.call_args_list[-1]

    assert first.args[0] == second.args[0]
    assert first.args[0] == [
        "/opt/android/platform-tools/adb",
        "-H",
        "127.0.0.1",
        "-P",
        "5040",
        "-s",
        "ZX-1",
        "shell",
        "sh -c " + secret_input.shlex.quote(secret_input._REMOTE_SCRIPT),
    ]
    assert first.kwargs["input"] != second.kwargs["input"]
    assert "Alpha123" not in repr(first.args[0])
    assert "Beta456" not in repr(second.args[0])


def test_transport_exception_and_logs_do_not_expose_secret(monkeypatch, caplog):
    value = "Private42"

    def fail_with_sensitive_details(*args, **kwargs):
        raise RuntimeError(f"transport failed for {value}")

    monkeypatch.setattr(secret_input.subprocess, "run", fail_with_sensitive_details)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SecretInputError) as error:
        enter_sensitive_text(fake_device(), value)

    assert value not in str(error.value)
    assert value not in repr(error.value)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert value not in caplog.text


def test_nonzero_remote_exit_has_a_sanitized_error(monkeypatch):
    value = "Private42"
    monkeypatch.setattr(
        secret_input.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(returncode=1, stderr=value)),
    )

    with pytest.raises(SecretInputError) as error:
        enter_sensitive_text(fake_device(), value)

    assert value not in str(error.value)
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "value",
    [
        "private\nvalue",
        "private\rvalue",
        "private\tvalue",
        "private\x00value",
        "private\x1bvalue",
        "private\x85value",
    ],
)
def test_multiline_and_control_characters_fail_before_device_actions(monkeypatch, value):
    run = Mock()
    monkeypatch.setattr(secret_input.subprocess, "run", run)
    device = fake_device()

    with pytest.raises(SecretInputValidationError) as error:
        enter_sensitive_text(device, value)

    assert "no device action was performed" in str(error.value)
    assert value not in str(error.value)
    run.assert_not_called()
    assert device.adb.command_calls == []


@pytest.mark.parametrize("value", ["p\u00e4ssword", "密碼"])
def test_uncertain_unicode_fails_closed_before_subprocess(monkeypatch, value):
    run = Mock()
    monkeypatch.setattr(secret_input.subprocess, "run", run)

    with pytest.raises(SecretInputValidationError) as error:
        enter_sensitive_text(fake_device(), value)

    assert "only printable ASCII" in str(error.value)
    assert value not in str(error.value)
    run.assert_not_called()


def test_fixed_remote_shell_is_valid_with_adb_style_argument_joining():
    shell = shutil.which('sh')
    if shell is None:
        pytest.skip('POSIX shell unavailable for remote-command simulation')
    remote_command = 'sh -c ' + secret_input.shlex.quote(secret_input._REMOTE_SCRIPT)
    result = subprocess.run(
        [shell, '-c', remote_command], input=secret_input._END_RECORD,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
    )
    assert result.returncode == 0


def test_printable_punctuation_is_sent_only_as_individual_stdin_records(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(secret_input.subprocess, 'run', run)
    value = 'P@ss;$(safe)%!'
    enter_sensitive_text(fake_device(), value)
    assert value not in repr(run.call_args.args[0])
    assert run.call_args.kwargs['input'] == b''.join(
        char.encode('ascii') + b'\n' for char in value
    ) + secret_input._END_RECORD


def test_unvalidated_device_endpoint_is_rejected_without_subprocess(monkeypatch):
    run = Mock()
    monkeypatch.setattr(secret_input.subprocess, "run", run)

    with pytest.raises(SecretInputDeviceError, match="local ADB server"):
        enter_sensitive_text(fake_device(serialno="-s;reboot"), "safe123")
    with pytest.raises(SecretInputDeviceError, match="local ADB server"):
        enter_sensitive_text(fake_device(host="192.0.2.4"), "safe123")
    with pytest.raises(SecretInputDeviceError, match="local ADB server"):
        enter_sensitive_text(fake_device(port=True), "safe123")

    run.assert_not_called()


def test_device_exceptions_are_sanitized(monkeypatch):
    run = Mock()
    monkeypatch.setattr(secret_input.subprocess, "run", run)

    class BrokenDevice:
        @property
        def adb(self):
            raise RuntimeError("sensitive device metadata")

    with pytest.raises(SecretInputDeviceError) as error:
        enter_sensitive_text(BrokenDevice(), "safe123")

    assert "sensitive device metadata" not in str(error.value)
    assert error.value.__context__ is None
    run.assert_not_called()


def test_adb_timeout_is_sanitized_and_warns_that_partial_input_is_possible(monkeypatch):
    value = "Private42"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=value.encode())

    monkeypatch.setattr(secret_input.subprocess, "run", timeout)

    with pytest.raises(SecretInputError) as error:
        enter_sensitive_text(fake_device(), value)

    assert "partially changed" in str(error.value)
    assert value not in str(error.value)
    assert error.value.__context__ is None
