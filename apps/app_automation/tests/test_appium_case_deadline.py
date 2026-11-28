from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.app_automation import appium_execution, appium_runtime
from apps.app_automation.appium_models import AppiumSession
from apps.app_automation.device_pool import acquire_specific_device
from apps.app_automation.execution_admission import mark_device_action_pending
from apps.app_automation.executors import test_executor
from apps.app_automation.models import AppDevice, AppPackage, AppProject, AppTestCase, AppTestExecution
from apps.users.models import User


class Options:
    def load_capabilities(self, capabilities):
        self.capabilities = capabilities


def fake_bindings(remote):
    class Connection:
        def __init__(self, client_config):
            self._client_config = client_config

    return SimpleNamespace(
        webdriver=SimpleNamespace(Remote=remote), options_type=Options,
        client_config_type=lambda **kwargs: SimpleNamespace(**kwargs),
        connection_type=Connection,
    )


def test_new_session_client_uses_remaining_case_budget_and_disables_retry(settings, monkeypatch):
    settings.APP_APPIUM_ENDPOINTS = {'1': 'http://127.0.0.1:4723'}
    remote = Mock(return_value=SimpleNamespace(session_id='synthetic-session'))
    monkeypatch.setattr(appium_runtime, '_load_client', lambda: fake_bindings(remote))
    monkeypatch.setattr(appium_runtime, 'time', SimpleNamespace(monotonic=lambda: 200.0))
    device = SimpleNamespace(pk=1, id=1, device_id='synthetic-serial')
    driver = appium_runtime.create_driver(
        device, 'org.example.app', system_port=8200, deadline=200.75,
    )
    assert driver.session_id == 'synthetic-session'
    config = remote.call_args.kwargs['client_config']
    assert config.timeout == 0.75
    assert config.init_args_for_pool_manager['init_args_for_pool_manager']['retries'] is False


def test_expired_session_budget_never_issues_new_session_request(settings, monkeypatch):
    settings.APP_APPIUM_ENDPOINTS = {'1': 'http://127.0.0.1:4723'}
    remote = Mock()
    monkeypatch.setattr(appium_runtime, '_load_client', lambda: fake_bindings(remote))
    monkeypatch.setattr(appium_runtime, 'time', SimpleNamespace(monotonic=lambda: 200.0))
    device = SimpleNamespace(pk=1, id=1, device_id='synthetic-serial')
    with pytest.raises(appium_runtime.AppiumInputError, match='expired'):
        appium_runtime.create_driver(device, 'org.example.app', system_port=8200, deadline=200.0)
    remote.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_parent_classifies_session_timeout_and_preserves_unknown_device_fence(settings, tmp_path, monkeypatch):
    owner = User.objects.create_user(username='appium-start-timeout-owner')
    project = AppProject.objects.create(name='Session deadline project', owner=owner, appium_enabled=True)
    package = AppPackage.objects.create(name='Deadline app', package_name='org.example.deadline', created_by=owner)
    case = AppTestCase.objects.create(name='Short case budget', project=project, app_package=package, created_by=owner)
    device = AppDevice.objects.create(name='Synthetic device', device_id='deadline-synthetic-device', status='available')
    device, _token = acquire_specific_device(device.pk, owner, lease_seconds=90, cap_lease_to_grant=True)
    settings.APP_APPIUM_ENDPOINTS = {str(device.pk): 'http://127.0.0.1:4723'}
    execution = AppTestExecution.objects.create(
        test_case=case, user=owner, device=device, status='running', engine='appium',
        device_lease_generation=device.lease_generation,
    )
    clock = {'now': 100.0}
    monkeypatch.setattr(appium_runtime, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    monkeypatch.setattr(test_executor, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    monkeypatch.setattr(appium_execution, '_validate_execution_inputs', lambda *_args: (
        'http://127.0.0.1:4723', 8200, {'platformName': 'Android'},
    ))
    timeouts = []

    def new_session(**kwargs):
        timeout = kwargs['client_config'].timeout
        timeouts.append(timeout)
        clock['now'] += timeout
        raise TimeoutError('Synthetic new-session timeout')

    monkeypatch.setattr(appium_runtime, '_load_client', lambda: fake_bindings(new_session))
    executor = test_executor.AppTestExecutor(base_path=str(tmp_path))
    monkeypatch.setattr(executor, '_preflight_worker', lambda: {'adb_path': str(tmp_path / 'adb')})
    monkeypatch.setattr(executor, '_get_allure_results_dir', lambda _id: str(tmp_path / 'results'))
    spawn = Mock()
    monkeypatch.setattr(test_executor.subprocess, 'Popen', spawn)
    with pytest.raises(test_executor.AppTestTimeoutError):
        executor.run_tests(
            case.pk, device.device_id, package.package_name,
            execution_id=execution.pk, engine='appium', deadline=100.5,
            before_device_io=lambda: mark_device_action_pending(execution.pk),
        )
    assert timeouts == [0.5]
    spawn.assert_not_called()
    session = AppiumSession.objects.get(execution=execution)
    assert session.status == 'unknown'
    assert session.failure_code == 'appium_start_outcome_unknown'
    device.refresh_from_db()
    execution.refresh_from_db()
    assert device.device_action_pending is True
    assert execution.device_action_pending is True
    assert executor._device_io_stopped_safely is False
