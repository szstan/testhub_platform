from unittest.mock import Mock, patch

import pytest

from apps.app_automation.executors.test_executor import AppTestExecutor, AppTestWorkerError
from apps.app_automation.utils.airtest_base import AirtestBase
from apps.app_automation import tasks
from apps.app_automation.models import AppDevice, AppProject, AppTestCase, AppTestExecution
from apps.users.models import User


PRIVATE = 'SYNTHETIC-private-input-serial-response'


def test_worker_preflight_failure_hides_arbitrary_exception_details(tmp_path, caplog):
    executor = AppTestExecutor(base_path=str(tmp_path))
    caplog.set_level('DEBUG')
    with patch.object(executor, '_preflight_worker', side_effect=RuntimeError(PRIVATE)):
        with pytest.raises(AppTestWorkerError) as error:
            executor.run_tests(1, PRIVATE, 'org.example.app')
    assert PRIVATE not in str(error.value)
    assert PRIVATE not in caplog.text


def test_airtest_device_connection_hides_serial_and_exception(tmp_path, caplog):
    caplog.set_level('DEBUG')
    base = AirtestBase(device_id=PRIVATE, screenshots_dir=str(tmp_path))
    with patch('apps.app_automation.utils.airtest_base.init_device', side_effect=RuntimeError(PRIVATE)):
        assert not base.setup_airtest({'RETRY_COUNT': 1, 'DEVICE_CONNECT_TIMEOUT': 1})
    assert PRIVATE not in caplog.text


def test_websocket_progress_never_echoes_raw_step_details():
    layer = Mock()
    emit = Mock()
    with (
        patch.object(tasks, 'get_channel_layer', return_value=layer),
        patch.object(tasks, 'async_to_sync', return_value=emit),
    ):
        assert tasks.send_execution_update(
            42, status='running', progress=25,
            message=f'步骤 1/2: {PRIVATE} - 失败', report_path=f'/private/{PRIVATE}',
        )
    payload = emit.call_args.args[1]
    assert payload['message'] == '步骤 1/2 - 失败'
    assert payload['report_path'] is True
    assert PRIVATE not in str(payload)


@pytest.mark.django_db
def test_worker_failure_persists_and_publishes_only_curated_error(caplog):
    user = User.objects.create_user(username='worker-redaction')
    project = AppProject.objects.create(name='test project', owner=user)
    case = AppTestCase.objects.create(name='test case', project=project, created_by=user)
    device = AppDevice.objects.create(device_id=PRIVATE, status='online')
    device.lock(user)
    execution = AppTestExecution.objects.create(
        test_case=case, device=device, user=user, status='pending',
        device_lease_generation=device.lease_generation,
    )
    caplog.set_level('DEBUG')
    with (
        patch('apps.app_automation.device_pool.Thread'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor,
        patch.object(tasks, 'send_execution_update') as send,
    ):
        executor.return_value.run_tests.side_effect = RuntimeError(PRIVATE)
        tasks.execute_app_test_task.run(
            execution.pk, lease_generation=device.lease_generation,
            release_device_lease=False,
        )
    execution.refresh_from_db()
    assert execution.status == 'error'
    assert PRIVATE not in execution.error_message
    assert PRIVATE not in caplog.text
    assert PRIVATE not in str(send.call_args_list)
