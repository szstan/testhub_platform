from datetime import datetime, timedelta, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.app_automation.executors import test_executor as executor_module
from apps.app_automation.executors.test_executor import (
    AppTestExecutor,
    AppTestTimeoutError,
)
from apps.app_automation.tasks import _app_case_deadline


def _executor_with_fake_worker(tmp_path, monkeypatch, process):
    executor = AppTestExecutor(base_path=str(tmp_path))
    monkeypatch.setattr(executor, '_preflight_worker', lambda: {'adb_path': str(tmp_path / 'adb')})
    monkeypatch.setattr(executor, '_get_allure_results_dir', lambda _execution_id: str(tmp_path / 'allure'))
    monkeypatch.setattr(executor, '_build_pytest_args', lambda _directory: ['pytest'])
    process.pid = 'fake-pid'
    process.poll.return_value = None
    monkeypatch.setattr(executor_module.subprocess, 'Popen', lambda *_args, **_kwargs: process)
    return executor


def test_case_deadline_uses_persisted_start_and_bounded_timeout():
    started_at = datetime(2026, 1, 1, tzinfo=datetime_timezone.utc)
    execution = SimpleNamespace(started_at=started_at)
    case = SimpleNamespace(timeout=30)

    deadline = _app_case_deadline(
        case,
        execution,
        now=started_at + timedelta(seconds=12),
        monotonic_now=100.0,
    )

    assert deadline == 118.0


def test_parent_deadline_terminates_worker_before_discarding_process_handle(tmp_path, monkeypatch):
    clock = {'now': 0.0}
    monkeypatch.setattr(
        executor_module,
        'time',
        SimpleNamespace(monotonic=lambda: clock['now']),
    )
    process = Mock()

    def wait(*, timeout):
        if timeout == 5:
            return 0
        clock['now'] += timeout
        raise executor_module.subprocess.TimeoutExpired('pytest', timeout)

    process.wait.side_effect = wait
    executor = _executor_with_fake_worker(tmp_path, monkeypatch, process)

    with pytest.raises(AppTestTimeoutError):
        executor.run_tests(
            test_case_id=1,
            device_id='device-1',
            package_name='org.example.app',
            deadline=0.5,
        )

    assert clock['now'] == 0.5
    assert [call.kwargs['timeout'] for call in process.wait.call_args_list] == [0.25, 0.25, 5]
    process.terminate.assert_called_once()
    assert process.kill.call_count == (executor_module.os.name != 'nt')
    assert executor._current_process is None
    assert executor._last_process_exit_confirmed is True


def test_cancelled_worker_is_terminated_and_confirmed_before_handle_cleanup(tmp_path, monkeypatch):
    process = Mock()
    process.wait.return_value = 0
    executor = _executor_with_fake_worker(tmp_path, monkeypatch, process)
    checks = {'count': 0}

    def cancel_check():
        checks['count'] += 1
        if checks['count'] >= 3:
            raise RuntimeError('APP执行已停止或不再处于运行状态')

    with pytest.raises(RuntimeError, match='APP执行已停止'):
        executor.run_tests(
            test_case_id=1,
            device_id='device-1',
            package_name='org.example.app',
            cancel_check=cancel_check,
        )

    process.terminate.assert_called_once()
    assert executor._current_process is None
    assert executor._last_process_exit_confirmed is True
