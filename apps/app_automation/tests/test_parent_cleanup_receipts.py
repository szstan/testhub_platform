from types import SimpleNamespace
from unittest.mock import Mock
import subprocess

import pytest

from apps.app_automation import appium_execution
from apps.app_automation.executors.test_executor import AppTestProcessTerminationError
from apps.app_automation.tests.test_parent_case_deadline import _executor_with_fake_worker


def test_unreaped_child_never_grants_remote_session_cleanup_proof(tmp_path, monkeypatch):
    process = Mock()
    def wait(**kwargs):
        raise subprocess.TimeoutExpired('pytest', kwargs['timeout'])

    process.wait.side_effect = wait
    executor = _executor_with_fake_worker(tmp_path, monkeypatch, process)
    monkeypatch.setattr(appium_execution, 'preflight_execution_session', lambda *_args: None)
    monkeypatch.setattr(appium_execution, 'start_execution_session', lambda *_args, **_kwargs: SimpleNamespace(id='owned-session'))
    finish = Mock(return_value=False)
    monkeypatch.setattr(appium_execution, 'finish_execution_session', finish)
    checks = {'count': 0}

    def cancel():
        checks['count'] += 1
        if checks['count'] >= 4:
            raise RuntimeError('APP执行已停止或不再处于运行状态')

    with pytest.raises(AppTestProcessTerminationError):
        executor.run_tests(
            1, 'synthetic-device', 'org.example.app', execution_id=1, engine='appium',
            cancel_check=cancel, before_device_io=lambda: None,
        )
    assert executor._current_process is process
    assert executor._last_process_exit_confirmed is False
    assert executor._device_io_stopped_safely is False
    finish.assert_called_once()
    assert finish.call_args.kwargs['child_exit_confirmed'] is False
