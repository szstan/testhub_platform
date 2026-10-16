import importlib
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from apps.app_automation.executors.test_executor import (
    AppTestExecutor,
    AppTestWorkerError,
)
from apps.app_automation.utils.airtest_base import AirtestBase
from apps.app_automation.utils import airtest_base as airtest_base_module


ALLURE_COMMAND = Path(__file__).resolve().parents[3] / 'allure' / 'bin' / 'allure'


def test_build_pytest_args_selects_only_ui_flow_entry(tmp_path):
    executor = AppTestExecutor(base_path=str(tmp_path))

    args = executor._build_pytest_args(str(tmp_path / 'allure-results'))

    assert 'apps/app_automation/tests/test_app_flow.py' in args
    pytest_index = args.index('pytest')
    assert args[args.index('-m', pytest_index) + 1] == 'app_ui_flow'
    assert args[args.index('-p', pytest_index) + 1] == 'no:django'
    assert args[args.index('-o', pytest_index) + 1] == 'addopts='
    assert 'apps/app_automation/tests/' not in args
    assert not any('test_security_boundaries.py' in arg for arg in args)


@patch.object(airtest_base_module, 'init_device')
def test_airtest_base_passes_resolved_adb_path_to_airtest(init_device, tmp_path):
    airtest_base = AirtestBase(
        device_id='device-1',
        screenshots_dir=str(tmp_path),
        adb_path='/opt/android-sdk/platform-tools/adb',
    )

    assert airtest_base._init_device_with_timeout(
        platform='Android', uuid='device-1', timeout=1
    )

    init_device.assert_called_once_with(
        platform='Android',
        uuid='device-1',
        adb_path='/opt/android-sdk/platform-tools/adb',
    )


def test_preflight_checks_python_packages_adb_and_allure(tmp_path):
    executor = AppTestExecutor(base_path=str(tmp_path))

    with (
        patch.object(importlib, 'import_module', return_value=object()) as import_module,
        patch.object(shutil, 'which', return_value='/usr/bin/adb') as which,
        patch.object(subprocess, 'run') as run,
        patch.object(executor, '_find_allure_command', return_value=str(ALLURE_COMMAND)),
    ):
        run.return_value.returncode = 0
        runtime = executor._preflight_worker()

    assert runtime['adb_path'] == '/usr/bin/adb'
    assert runtime['allure_path'] == str(ALLURE_COMMAND)
    assert import_module.call_args_list == [
        call('airtest.core.api'),
        call('allure'),
        call('allure_pytest'),
    ]
    which.assert_called_once_with('adb')
    assert run.call_args_list == [
        call(
            ['/usr/bin/adb', 'version'],
            capture_output=True,
            text=True,
            timeout=5,
        ),
        call(
            [str(ALLURE_COMMAND), '--version'],
            capture_output=True,
            text=True,
            timeout=15,
        ),
    ]


def test_preflight_rejects_unhealthy_allure_command(tmp_path):
    executor = AppTestExecutor(base_path=str(tmp_path))

    with (
        patch.object(importlib, 'import_module', return_value=object()),
        patch.object(shutil, 'which', return_value='/usr/bin/adb'),
        patch.object(subprocess, 'run', side_effect=[Mock(returncode=0), Mock(returncode=1)]),
        patch.object(executor, '_find_allure_command', return_value=str(ALLURE_COMMAND)),
    ):
        with pytest.raises(AppTestWorkerError, match='Allure command failed'):
            executor._preflight_worker()


def test_preflight_fails_before_starting_without_worker_dependencies(tmp_path):
    executor = AppTestExecutor(base_path=str(tmp_path))

    with (
        patch.object(importlib, 'import_module', side_effect=ImportError),
        patch.object(shutil, 'which', return_value=None) as which,
        patch.object(executor, '_find_allure_command', return_value=None),
    ):
        with pytest.raises(AppTestWorkerError) as error:
            executor._preflight_worker()

    message = str(error.value)
    assert 'airtest' in message
    assert 'allure-pytest' in message
    assert 'ADB executable' in message
    assert 'Allure command' in message
    which.assert_called_once_with('adb')


def test_run_tests_propagates_allure_report_failure(tmp_path):
    executor = AppTestExecutor(base_path=str(tmp_path))
    results_dir = tmp_path / 'allure-results'
    results_dir.mkdir()
    process = Mock()
    process.stdout = []
    process.wait.return_value = 0

    with (
        patch.object(
            executor,
            '_preflight_worker',
            return_value={
                'adb_path': str(tmp_path / 'adb'),
                'allure_path': str(ALLURE_COMMAND),
            },
        ),
        patch.object(executor, '_get_allure_results_dir', return_value=str(results_dir)),
        patch.object(executor, '_get_log_file_path', return_value=str(tmp_path / 'run.log')),
        patch.object(
            executor,
            '_parse_allure_results',
            return_value={
                'total': 1,
                'passed': 1,
                'failed': 0,
                'skipped': 0,
                'broken': 0,
            },
        ),
        patch.object(executor, '_generate_allure_report', return_value=None),
        patch.object(subprocess, 'Popen', return_value=process),
    ):
        with pytest.raises(AppTestWorkerError, match='Allure report generation failed'):
            executor.run_tests(1, 'device', 'package')


def test_run_tests_does_not_spawn_pytest_when_worker_preflight_fails(tmp_path):
    executor = AppTestExecutor(base_path=str(tmp_path))

    with (
        patch.object(
            executor,
            '_preflight_worker',
            side_effect=AppTestWorkerError('worker preflight failed'),
        ),
        patch.object(subprocess, 'Popen') as popen,
    ):
        with pytest.raises(AppTestWorkerError, match='worker preflight failed'):
            executor.run_tests(1, 'device', 'package')

    popen.assert_not_called()
