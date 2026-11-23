from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

pytest.importorskip('airtest.core.api', reason='Airtest is required to import the runner')

from apps.app_automation.models import AppDevice, AppTestExecution
from apps.app_automation.runners import ui_flow_runner as flow_module
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner


class FakeDevice:
    def __init__(self, resolution=(101, 201)):
        self.adb = object()
        self.resolution = resolution

    def get_current_resolution(self):
        return self.resolution


def make_runner(tmp_path, *, execution_id=None):
    runner = UiFlowRunner.__new__(UiFlowRunner)
    runner.image_base_dir = str(tmp_path / 'images')
    runner.screenshots_dir = str(tmp_path / 'screenshots')
    runner.context = {'local': {}, 'global': {}, 'outputs': {}, 'steps': {}}
    runner.runtime = {'retry_times': 0, 'retry_interval': 0}
    runner._ocr_helper = None
    runner._assertions_executed = 0
    runner.execution_id = execution_id
    return runner


def test_native_selectors_resolve_to_safe_centers_and_click(tmp_path, monkeypatch, caplog):
    runner = make_runner(tmp_path)
    device = FakeDevice()
    match = SimpleNamespace(center=(40, 60), text='private hierarchy text')
    calls = []

    def find(adb, strategy, value, **kwargs):
        calls.append((adb, strategy, value, kwargs))
        return match

    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=device))
    monkeypatch.setattr(flow_module.native_locator, 'find_native_element', find)
    with patch.object(flow_module, 'touch') as touch:
        for strategy in ('resource_id', 'text', 'content_desc', 'class'):
            runner._action_click({
                'selector_type': strategy,
                'selector': f'private-{strategy}',
                'selector_match': 'contains',
            })

    assert [call[1] for call in calls] == ['resource_id', 'text', 'content_desc', 'class']
    assert all(call[0] is device.adb and call[3]['match'] == 'contains' for call in calls)
    assert touch.call_args_list == [call((40, 60))] * 4
    assert 'private hierarchy text' not in caplog.text


def test_device_actions_use_helpers_and_runtime_package_fallback(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    runner.runtime = {'package_name': 'org.runtime.app'}
    device = FakeDevice()
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=device))
    clear = Mock()
    key = Mock()
    hide = Mock()
    start = Mock()
    stop = Mock()
    restart = Mock()
    permission = Mock()
    monkeypatch.setattr(flow_module.device_actions, 'clear_focused_input', clear)
    monkeypatch.setattr(flow_module.device_actions, 'send_key', key)
    monkeypatch.setattr(flow_module.device_actions, 'hide_keyboard', hide)
    monkeypatch.setattr(flow_module.device_actions, 'start_application', start)
    monkeypatch.setattr(flow_module.device_actions, 'stop_application', stop)
    monkeypatch.setattr(flow_module.device_actions, 'restart_application', restart)
    monkeypatch.setattr(flow_module.device_actions, 'handle_permission', permission)

    runner._dispatch_step({'type': 'clear_input'}, 'clear_input')
    runner._dispatch_step({'type': 'key_event', 'key': 'BACK'}, 'key_event')
    runner._dispatch_step({'type': 'hide_keyboard'}, 'hide_keyboard')
    runner._dispatch_step({'type': 'app_start', 'package_name': 'org.explicit.app'}, 'app_start')
    runner._dispatch_step({'type': 'app_stop'}, 'app_stop')
    runner._dispatch_step({'type': 'app_restart'}, 'app_restart')
    runner._dispatch_step({
        'type': 'handle_permission', 'choice': 'allow', 'timeout': 2, 'optional': False,
    }, 'handle_permission')

    clear.assert_called_once_with(device)
    key.assert_called_once_with(device, 'BACK')
    hide.assert_called_once_with(device)
    start.assert_called_once_with(device, 'org.explicit.app')
    stop.assert_called_once_with(device, 'org.runtime.app')
    restart.assert_called_once_with(device, 'org.runtime.app')
    permission.assert_called_once_with(device, 'allow', timeout=2, optional=False)


def test_new_element_actions_and_assertions_count_only_successful_assertions(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    device = FakeDevice()
    native_match = SimpleNamespace(
        center=(20, 30), text='Ready', content_desc='Ready', visible=True,
        enabled=True, checked=False, selected=False,
    )
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=device))
    monkeypatch.setattr(flow_module.native_locator, 'find_native_element', lambda *a, **k: native_match)

    result = runner.run([
        {'type': 'wait_element', 'selector_type': 'resource_id', 'selector': 'app:id/title',
         'state': 'visible', 'timeout': 0},
        {'type': 'assert_element', 'selector_type': 'resource_id', 'selector': 'app:id/title',
         'state': 'enabled', 'timeout': 0},
        {'type': 'assert_text', 'selector_type': 'resource_id', 'selector': 'app:id/title',
         'expected': 'Ready', 'match_mode': 'exact', 'timeout': 0},
        {'type': 'assert_value', 'left': '2', 'operator': '==', 'right': '2'},
        {'type': 'assert_response', 'source': 'local.response', 'path': 'status_code',
         'operator': '==', 'expected': 200},
        {'type': 'extract_text', 'selector_type': 'resource_id', 'selector': 'app:id/title',
         'name': 'page_title', 'scope': 'local', 'timeout': 0},
    ], variables=[{'name': 'response', 'value': {'status_code': 200}}],
       runtime={'retry_times': 0, 'stop_on_error': True})

    assert result['failed'] == 0
    assert result['assertions'] == 4
    assert runner.context['local']['page_title'] == 'Ready'

    monkeypatch.setattr(runner, '_attach_allure', Mock())
    failed = runner.run([
        {'type': 'assert_value', 'left': 'expected', 'operator': '==', 'right': 'different'},
    ], runtime={'retry_times': 0})
    assert failed['failed'] == 1
    assert failed['assertions'] == 0


def test_runner_signals_only_completed_read_only_assertion_failures(tmp_path):
    runner = make_runner(tmp_path)
    with pytest.raises(flow_module.ReadOnlyBusinessAssertionFailure):
        runner.run(
            [{'type': 'assert_value', 'left': 1, 'operator': '==', 'right': 2}],
            runtime={'retry_times': 0, 'stop_on_error': True},
        )

    runner = make_runner(tmp_path)
    runner._attach_allure = Mock()
    simulated_clicks = []

    def click_then_fail(_step):
        simulated_clicks.append('click')
        raise AssertionError('foreach assertion failed after click')

    runner._action_foreach_assert = click_then_fail
    with pytest.raises(AssertionError) as error:
        runner.run(
            [{'type': 'foreach_assert'}],
            runtime={'retry_times': 0, 'stop_on_error': True},
        )

    assert type(error.value) is AssertionError
    assert simulated_clicks == ['click']


def test_missing_device_and_unavailable_ocr_fail_clearly(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=None))
    with pytest.raises(RuntimeError, match='Android device is unavailable'):
        runner._action_wait_element({
            'selector_type': 'resource_id', 'selector': 'app:id/title', 'state': 'visible', 'timeout': 0,
        })

    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=FakeDevice()))
    monkeypatch.setattr(flow_module, 'OCR_AVAILABLE', False)
    with pytest.raises(RuntimeError, match='OCR is unavailable'):
        runner._action_assert_text({
            'selector_type': 'region', 'selector': '0,0,20,20', 'expected': 'Ready', 'timeout': 0,
        })


def test_relative_click_and_image_ratio_swipe_scale_and_validate(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=FakeDevice((101, 201))))
    assert runner._resolve_selector({'selector_type': 'relative', 'selector': '0.5,1'}) == (50, 200)

    with patch.object(flow_module, 'swipe') as swipe:
        runner._action_swipe({
            'coordinate_space': 'image_ratio', 'start': '0,0', 'end': '1,1', 'duration': 0.5,
        })
        with pytest.raises(ValueError, match='ratios from 0 to 1'):
            runner._action_swipe({
                'coordinate_space': 'image_ratio', 'start': '0,0', 'end': '1.01,1', 'duration': 0.5,
            })

    swipe.assert_called_once_with((0, 0), (100, 200), duration=0.5)
    with pytest.raises(ValueError, match='ratios from 0 to 1'):
        runner._resolve_selector({'selector_type': 'relative', 'selector': '0,-0.1'})


def test_sensitive_input_uses_secret_helper_and_never_airtest_text(tmp_path, monkeypatch, caplog):
    runner = make_runner(tmp_path)
    device = FakeDevice()
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=device))
    secret_input = Mock()
    monkeypatch.setattr(flow_module.secret_input, 'enter_sensitive_text', secret_input)
    caplog.set_level('INFO', logger=flow_module.__name__)

    with patch.object(flow_module, 'airtest_text') as airtest_text:
        runner._action_input({'sensitive': True, 'value': 'private-input-fixture'})

    secret_input.assert_called_once_with(device, 'private-input-fixture')
    airtest_text.assert_not_called()
    assert 'private-input-fixture' not in caplog.text


@pytest.mark.parametrize('expression', ['{{secret}}', 'prefix-${local.secret}-suffix'])
def test_sensitive_variable_references_use_secret_path_without_explicit_flag(
    tmp_path, monkeypatch, caplog, expression,
):
    runner = make_runner(tmp_path, execution_id=123)
    device = FakeDevice()
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=device))
    entry = Mock(side_effect=flow_module.secret_input.SecretInputError('safe failure'))
    monkeypatch.setattr(flow_module.secret_input, 'enter_sensitive_text', entry)
    quarantine = Mock(return_value=True)
    monkeypatch.setattr(runner, '_quarantine_execution_device', quarantine)
    caplog.set_level('INFO', logger=flow_module.__name__)

    with (
        patch.object(flow_module, 'ALLURE_AVAILABLE', True),
        patch.object(flow_module.allure, 'attach') as attach,
        patch.object(runner, '_capture_screenshot') as screenshot,
        patch.object(flow_module, 'airtest_text') as airtest_text,
        pytest.raises(flow_module.secret_input.SecretInputError),
    ):
        runner.run([{
            'type': 'input', 'value': expression, 'retry_times': 3,
        }], variables=[{
            'name': 'secret', 'scope': 'local', 'value': 'private-alias-value', 'sensitive': True,
        }], runtime={'retry_times': 3, 'retry_interval': 0, 'stop_on_error': True})

    entry.assert_called_once_with(device, expression.replace('{{secret}}', 'private-alias-value').replace(
        '${local.secret}', 'private-alias-value',
    ))
    quarantine.assert_called_once_with()
    airtest_text.assert_not_called()
    screenshot.assert_not_called()
    assert 'private-alias-value' not in caplog.text
    assert 'private-alias-value' not in str(attach.call_args)


def test_sensitive_variable_value_taints_rendered_unmarked_input(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    device = FakeDevice()
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=device))
    entry = Mock()
    monkeypatch.setattr(flow_module.secret_input, 'enter_sensitive_text', entry)

    with patch.object(flow_module, 'airtest_text') as airtest_text:
        runner.run([{'type': 'input', 'value': 'private-dataset-secret'}], variables=[{
            'name': 'password', 'value': 'private-dataset-secret', 'sensitive': True,
        }], runtime={'retry_times': 0, 'stop_on_error': True})

    entry.assert_called_once_with(device, 'private-dataset-secret')
    airtest_text.assert_not_called()


def test_uncertain_sensitive_failure_is_not_retried_and_quarantines(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, execution_id=123)
    device = FakeDevice()
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=device))
    entry = Mock(side_effect=flow_module.secret_input.SecretInputError('safe failure'))
    monkeypatch.setattr(flow_module.secret_input, 'enter_sensitive_text', entry)
    quarantine = Mock(return_value=True)
    monkeypatch.setattr(runner, '_quarantine_execution_device', quarantine)
    monkeypatch.setattr(runner, '_attach_allure', Mock())

    with pytest.raises(flow_module.secret_input.SecretInputError):
        runner.run([{
            'type': 'input', 'sensitive': True, 'value': 'private-input-fixture',
            'retry_times': 4,
        }], runtime={'retry_times': 4, 'retry_interval': 0, 'stop_on_error': True})

    entry.assert_called_once()
    quarantine.assert_called_once_with()


def test_sensitive_validation_failure_does_not_quarantine(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=FakeDevice()))
    monkeypatch.setattr(
        flow_module.secret_input,
        'enter_sensitive_text',
        Mock(side_effect=flow_module.secret_input.SecretInputValidationError('invalid input')),
    )
    quarantine = Mock()
    monkeypatch.setattr(runner, '_quarantine_execution_device', quarantine)

    with pytest.raises(flow_module.secret_input.SecretInputValidationError):
        runner._action_input({'sensitive': True, 'value': 'private-input-fixture'})
    quarantine.assert_not_called()


@pytest.mark.parametrize(('execution_generation', 'device_generation'), [(42, 42), (None, 0)])
def test_quarantine_is_fenced_to_current_execution_device_generation(
    monkeypatch, execution_generation, device_generation,
):
    execution = SimpleNamespace(
        pk=123, device_id=7, device_lease_generation=execution_generation, status='running',
    )
    device = SimpleNamespace(
        pk=7, lease_generation=device_generation, maintenance_mode=False,
        health_status='healthy', save=Mock(),
    )

    class Query:
        def __init__(self, value):
            self.value = value

        def values(self, *_fields):
            return self

        def first(self):
            return self.value

    class ExecutionManager:
        def __init__(self):
            self.filters = []

        def filter(self, **kwargs):
            self.filters.append(kwargs)
            return Query({
                'device_id': 7, 'device_lease_generation': execution_generation,
            } if len(self.filters) == 1 else execution)

        def select_for_update(self):
            return self

    class DeviceManager:
        def __init__(self):
            self.filters = []

        def select_for_update(self):
            return self

        def filter(self, **kwargs):
            self.filters.append(kwargs)
            return Query(device if kwargs.get('lease_generation') == device.lease_generation else None)

    executions = ExecutionManager()
    devices = DeviceManager()
    monkeypatch.setattr(AppTestExecution, 'objects', executions)
    monkeypatch.setattr(AppDevice, 'objects', devices)
    monkeypatch.setattr(flow_module.transaction, 'atomic', lambda: nullcontext())

    runner = UiFlowRunner.__new__(UiFlowRunner)
    runner.execution_id = 123
    assert runner._quarantine_execution_device() is True

    assert devices.filters == [{'pk': 7, 'lease_generation': device_generation}]
    assert executions.filters[1] == {
        'pk': 123,
        'device_id': 7,
        'device_lease_generation': execution_generation,
        'status__in': ('pending', 'running'),
    }
    assert device.maintenance_mode is True
    assert device.health_status == 'unknown'
    device.save.assert_called_once_with(
        update_fields=['maintenance_mode', 'health_status', 'updated_at'],
    )


def test_test_app_flow_passes_execution_and_package_to_runner():
    from apps.app_automation.tests import test_app_flow

    case = SimpleNamespace(
        pk=1,
        name='flow case',
        ui_flow=[{'type': 'wait'}],
        variables=[],
        runtime_config={'retry_times': 0},
    )
    execution = SimpleNamespace(pk=77, dataset=None, user=None)
    fake_airtest = SimpleNamespace(open_app=Mock(return_value=True))
    clear_marker = Mock()
    execution_query = SimpleNamespace(get=Mock(return_value=execution))
    with (
        patch.object(test_app_flow.AppTestCase.objects, 'get', return_value=case),
        patch.object(
            test_app_flow.AppTestExecution.objects,
            'select_related',
            return_value=execution_query,
        ),
        patch.object(test_app_flow, 'prepare_scene_execution', return_value=(case.ui_flow, [])),
        patch.object(test_app_flow, '_clear_device_action_marker', clear_marker),
        patch.object(test_app_flow, 'UiFlowRunner') as runner,
        patch.object(test_app_flow.allure.dynamic, 'title'),
        patch.object(test_app_flow.allure.dynamic, 'suite'),
        patch.object(test_app_flow.allure.dynamic, 'label'),
        patch.object(test_app_flow.allure, 'attach'),
    ):
        runner.return_value.run.return_value = {
            'total': 1, 'passed': 1, 'failed': 0, 'assertions': 1,
        }
        test_app_flow.TestAppFlow().test_execute_ui_flow(
            1, 'org.example.app', 77, fake_airtest, 'fixture-user',
        )

    runner.assert_called_once_with(username='fixture-user', execution_id=77)
    assert runner.return_value.run.call_args.kwargs['runtime'] == {
        'retry_times': 0, 'stop_on_error': True, 'package_name': 'org.example.app',
    }
    clear_marker.assert_called_once_with(77)


def test_business_assertion_failure_clears_only_after_runner_stops():
    from apps.app_automation.tests import test_app_flow

    case = SimpleNamespace(
        pk=1, name='flow case', ui_flow=[{'type': 'assert_value'}], variables=[], runtime_config={}
    )
    execution = SimpleNamespace(pk=77, dataset=None, user=None)
    fake_airtest = SimpleNamespace(open_app=Mock(return_value=True))
    clear_marker = Mock()
    execution_query = SimpleNamespace(get=Mock(return_value=execution))
    with (
        patch.object(test_app_flow.AppTestCase.objects, 'get', return_value=case),
        patch.object(test_app_flow.AppTestExecution.objects, 'select_related', return_value=execution_query),
        patch.object(test_app_flow, 'prepare_scene_execution', return_value=(case.ui_flow, [])),
        patch.object(test_app_flow, '_clear_device_action_marker', clear_marker),
        patch.object(test_app_flow, 'UiFlowRunner') as runner,
        patch.object(test_app_flow, '_make_progress_callback', return_value=Mock()),
        patch.object(test_app_flow.allure.dynamic, 'title'),
        patch.object(test_app_flow.allure.dynamic, 'suite'),
    ):
        runner.return_value.run.side_effect = (
            test_app_flow.ui_flow_runner_module.ReadOnlyBusinessAssertionFailure(
                'business assertion failed'
            )
        )
        with pytest.raises(AssertionError, match='business assertion failed'):
            test_app_flow.TestAppFlow().test_execute_ui_flow(
                1, 'org.example.app', 77, fake_airtest, 'fixture-user',
            )

    clear_marker.assert_called_once_with(77)


def test_unclassified_assertion_does_not_clear_worker_marker():
    from apps.app_automation.tests import test_app_flow

    case = SimpleNamespace(
        pk=1, name='flow case', ui_flow=[{'type': 'assert_value'}], variables=[], runtime_config={}
    )
    execution = SimpleNamespace(pk=77, dataset=None, user=None)
    fake_airtest = SimpleNamespace(open_app=Mock(return_value=True))
    clear_marker = Mock()
    execution_query = SimpleNamespace(get=Mock(return_value=execution))
    with (
        patch.object(test_app_flow.AppTestCase.objects, 'get', return_value=case),
        patch.object(test_app_flow.AppTestExecution.objects, 'select_related', return_value=execution_query),
        patch.object(test_app_flow, 'prepare_scene_execution', return_value=(case.ui_flow, [])),
        patch.object(test_app_flow, '_clear_device_action_marker', clear_marker),
        patch.object(test_app_flow, 'UiFlowRunner') as runner,
        patch.object(test_app_flow.allure.dynamic, 'title'),
        patch.object(test_app_flow.allure.dynamic, 'suite'),
    ):
        runner.return_value.run.side_effect = AssertionError('unclassified assertion')
        with pytest.raises(AssertionError, match='unclassified assertion'):
            test_app_flow.TestAppFlow().test_execute_ui_flow(
                1, 'org.example.app', 77, fake_airtest, 'fixture-user',
            )

    clear_marker.assert_not_called()


def test_foreach_assertion_failure_retains_worker_marker():
    from apps.app_automation.tests import test_app_flow

    case = SimpleNamespace(
        pk=1, name='flow case', ui_flow=[{'type': 'foreach_assert'}], variables=[], runtime_config={}
    )
    execution = SimpleNamespace(pk=77, dataset=None, user=None)
    fake_airtest = SimpleNamespace(open_app=Mock(return_value=True))
    clear_marker = Mock()
    execution_query = SimpleNamespace(get=Mock(return_value=execution))
    with (
        patch.object(test_app_flow.AppTestCase.objects, 'get', return_value=case),
        patch.object(test_app_flow.AppTestExecution.objects, 'select_related', return_value=execution_query),
        patch.object(test_app_flow, 'prepare_scene_execution', return_value=(case.ui_flow, [])),
        patch.object(test_app_flow, '_clear_device_action_marker', clear_marker),
        patch.object(test_app_flow, 'UiFlowRunner') as runner,
        patch.object(test_app_flow, '_make_progress_callback', return_value=Mock()),
        patch.object(test_app_flow.allure.dynamic, 'title'),
        patch.object(test_app_flow.allure.dynamic, 'suite'),
    ):
        runner.return_value.run.side_effect = AssertionError('foreach assertion failed')
        with pytest.raises(AssertionError, match='foreach assertion failed'):
            test_app_flow.TestAppFlow().test_execute_ui_flow(
                1, 'org.example.app', 77, fake_airtest, 'fixture-user',
            )

    clear_marker.assert_not_called()


def test_uncertain_action_and_app_launch_do_not_clear_worker_marker():
    from apps.app_automation import device_actions
    from apps.app_automation.tests import test_app_flow

    with pytest.raises(device_actions.DeviceActionUncertainError):
        test_app_flow._open_app_or_raise_uncertain(
            SimpleNamespace(open_app=Mock(return_value=False)), 'org.example.app'
        )

    with patch.object(flow_module, 'touch', side_effect=AssertionError('unknown ADB result')):
        with test_app_flow._guard_airtest_mutations():
            with pytest.raises(device_actions.DeviceActionUncertainError):
                flow_module.touch((10, 20))


def test_input_send_enter_uncertain_adb_error_keeps_worker_marker(tmp_path, monkeypatch):
    from apps.app_automation.tests import test_app_flow

    case = SimpleNamespace(
        pk=1,
        name='flow case',
        app_package=None,
        ui_flow=[{'type': 'input', 'value': 'hello', 'send_enter': True}],
        variables=[],
        runtime_config={},
    )
    execution = SimpleNamespace(pk=77, dataset=None, user=None)
    execution_query = SimpleNamespace(get=Mock(return_value=execution))
    clear_marker = Mock()
    events = []

    class DispatchThenFailAdb:
        def cmd(self, arguments, *, timeout):
            events.append(('adb', arguments, timeout))
            raise ValueError('synthetic post-dispatch failure')

    runner = make_runner(tmp_path)
    runner._attach_allure = Mock()
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=SimpleNamespace(adb=DispatchThenFailAdb())))
    monkeypatch.setattr(flow_module.time, 'sleep', Mock())
    with (
        patch.object(test_app_flow.AppTestCase.objects, 'get', return_value=case),
        patch.object(test_app_flow.AppTestExecution.objects, 'select_related', return_value=execution_query),
        patch.object(test_app_flow, 'prepare_scene_execution', return_value=(case.ui_flow, [])),
        patch.object(test_app_flow, '_clear_device_action_marker', clear_marker),
        patch.object(test_app_flow, '_make_progress_callback', return_value=Mock()),
        patch.object(test_app_flow, 'UiFlowRunner', return_value=runner),
        patch.object(flow_module, 'airtest_text', side_effect=lambda value: events.append(('text', value))),
        patch.object(test_app_flow.allure.dynamic, 'title'),
        patch.object(test_app_flow.allure.dynamic, 'suite'),
        patch.object(test_app_flow.allure, 'attach'),
    ):
        with pytest.raises(
            flow_module.device_actions.DeviceActionUncertainError,
            match='Android 设备操作结果未知',
        ):
            test_app_flow.TestAppFlow().test_execute_ui_flow(
                1, '', 77, SimpleNamespace(open_app=Mock()), 'fixture-user',
            )

    assert events == [
        ('text', 'hello'),
        ('adb', ['shell', 'input', 'keyevent', 'KEYCODE_ENTER'], 5),
    ]
    clear_marker.assert_not_called()


def test_pre_device_validation_failure_clears_worker_marker():
    from apps.app_automation.tests import test_app_flow

    case = SimpleNamespace(pk=1, name='flow case', ui_flow=[], variables=[], runtime_config={})
    execution = SimpleNamespace(pk=77, dataset=None, user=None)
    fake_airtest = SimpleNamespace(open_app=Mock(return_value=True))
    clear_marker = Mock()
    execution_query = SimpleNamespace(get=Mock(return_value=execution))
    with (
        patch.object(test_app_flow.AppTestCase.objects, 'get', return_value=case),
        patch.object(test_app_flow.AppTestExecution.objects, 'select_related', return_value=execution_query),
        patch.object(test_app_flow, 'prepare_scene_execution', side_effect=ValueError('invalid flow')),
        patch.object(test_app_flow, '_clear_device_action_marker', clear_marker),
        patch.object(test_app_flow, 'UiFlowRunner') as runner,
    ):
        with pytest.raises(ValueError, match='invalid flow'):
            test_app_flow.TestAppFlow().test_execute_ui_flow(
                1, 'org.example.app', 77, fake_airtest, 'fixture-user',
            )

    clear_marker.assert_called_once_with(77)
    fake_airtest.open_app.assert_not_called()
    runner.assert_not_called()
