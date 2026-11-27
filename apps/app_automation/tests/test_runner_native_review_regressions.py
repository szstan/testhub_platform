from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

pytest.importorskip('airtest.core.api', reason='Airtest is required to import the runner')

from apps.app_automation import native_locator
from apps.app_automation.native_locator import (
    NativeLocatorHierarchyError,
    NativeLocatorTimeout,
    NativeLocatorUnsupportedDevice,
)
from apps.app_automation.runners import ui_flow_runner as flow_module
from apps.app_automation.runners.ui_flow_runner import ReadOnlyBusinessAssertionFailure, UiFlowRunner


class FakeADB:
    def __init__(self, outcome):
        self.outcome = outcome
        self.timeouts = []
        self.commands = []

    def cmd(self, commands, timeout=None):
        self.commands.append(commands)
        self.timeouts.append(timeout)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def make_runner(tmp_path):
    runner = UiFlowRunner.__new__(UiFlowRunner)
    runner.image_base_dir = str(tmp_path / 'images')
    runner.screenshots_dir = str(tmp_path / 'screenshots')
    runner.context = {'local': {}, 'global': {}, 'outputs': {}, 'steps': {}}
    runner.runtime = {'retry_times': 0, 'retry_interval': 0}
    runner._ocr_helper = None
    runner._assertions_executed = 0
    return runner


def test_sensitive_dynamic_config_name_is_absent_from_logs_and_allure(tmp_path, monkeypatch, caplog):
    secret = 'private-credential-value'
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'ALLURE_AVAILABLE', False)

    runner.run(
        [{'type': 'set_variable', 'config': {'name': '{{local.credential}}', 'value': 'ordinary'}}],
        variables=[{'name': 'credential', 'value': secret, 'sensitive': True}],
        runtime={'retry_times': 0, 'stop_on_error': True},
    )

    attach = Mock()
    attach.file = Mock()
    allure = SimpleNamespace(
        attach=attach,
        attachment_type=SimpleNamespace(JSON='json', PNG='png'),
    )
    monkeypatch.setattr(flow_module, 'allure', allure)
    monkeypatch.setattr(flow_module, 'ALLURE_AVAILABLE', True)
    monkeypatch.setattr(runner, '_capture_screenshot', Mock(return_value=None))
    runner._attach_allure(secret, {
        'type': 'set_variable',
        'config': {'name': secret, 'value': '{{local.credential}}'},
    })

    assert secret not in caplog.text
    payload = attach.call_args.args[0]
    assert secret not in payload
    assert secret not in str(attach.call_args)


def test_swallowed_mutation_error_invalidates_later_safe_proof(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=object()))
    mutation = Mock(side_effect=ValueError('synthetic mutation failure'))
    monkeypatch.setattr(flow_module.device_actions, 'start_application', mutation)
    swallowed = {
        'type': 'try',
        'try_steps': [{
            'type': 'app_start', 'package_name': 'org.synthetic.app',
        }],
        'catch_steps': [{'type': 'set_variable', 'name': 'handled', 'value': True}],
    }

    with pytest.raises(AssertionError) as error:
        runner.run([
            swallowed,
            {'type': 'assert_value', 'left': 3, 'operator': '==', 'right': 4},
        ], runtime={'retry_times': 0, 'stop_on_error': True})

    mutation.assert_called_once()
    assert not isinstance(error.value, ReadOnlyBusinessAssertionFailure)
    assert runner.context['local']['handled'] is True
    assert runner._ui_flow_unsafe_failure_seen is True


@pytest.mark.parametrize('container', ['sequence', 'if', 'loop'])
def test_nested_read_only_assertion_failure_propagates_safe_proof(tmp_path, container):
    runner = make_runner(tmp_path)
    assertion = {'type': 'assert_value', 'left': 1, 'operator': '==', 'right': 2}
    if container == 'sequence':
        step = {'type': 'sequence', 'steps': [assertion]}
    elif container == 'if':
        step = {
            'type': 'if', 'left': 'yes', 'operator': '==', 'right': 'yes',
            'then_steps': [assertion],
        }
    else:
        step = {'type': 'loop', 'mode': 'count', 'times': 1, 'steps': [assertion]}

    with pytest.raises(ReadOnlyBusinessAssertionFailure) as error:
        runner.run([step], runtime={'retry_times': 0, 'stop_on_error': True})

    assert isinstance(error.value.__cause__, AssertionError)
    assert error.value.__cause__._ui_flow_read_only_assertion_failure is True


def test_mutation_failure_cannot_become_safe_assertion_proof(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=object()))
    mutation = Mock(side_effect=ValueError('synthetic mutation failure'))
    monkeypatch.setattr(flow_module.device_actions, 'start_application', mutation)

    with pytest.raises(ValueError, match='synthetic mutation failure'):
        runner.run([{
            'type': 'sequence',
            'steps': [
                {'type': 'app_start', 'package_name': 'org.synthetic.app'},
                {'type': 'assert_value', 'left': 1, 'operator': '==', 'right': 2},
            ],
        }], runtime={'retry_times': 0, 'stop_on_error': True})

    mutation.assert_called_once()
    assert runner._ui_flow_unsafe_failure_seen is True


def test_swallowed_assertion_invalidates_later_safe_proof(tmp_path):
    runner = make_runner(tmp_path)
    swallowed = {
        'type': 'try',
        'try_steps': [{'type': 'assert_value', 'left': 1, 'operator': '==', 'right': 2}],
        'catch_steps': [{'type': 'set_variable', 'name': 'handled', 'value': True}],
    }
    later_failure = {'type': 'assert_value', 'left': 3, 'operator': '==', 'right': 4}

    with pytest.raises(AssertionError) as error:
        runner.run([swallowed, later_failure], runtime={'retry_times': 0, 'stop_on_error': True})

    assert not isinstance(error.value, ReadOnlyBusinessAssertionFailure)
    assert runner.context['local']['handled'] is True
    assert runner._ui_flow_unsafe_failure_seen is True


def test_native_interaction_skips_hidden_and_disabled_duplicate_matches(tmp_path, monkeypatch):
    hierarchy = '''<hierarchy>
      <node resource-id="app:id/submit" bounds="[1,2][11,12]" visible="false" enabled="true" />
      <node resource-id="app:id/submit" bounds="[21,22][41,42]" visible="true" enabled="false" />
      <node resource-id="app:id/submit" bounds="[51,52][71,72]" visible="true" enabled="true" />
    </hierarchy>'''
    adb = FakeADB(hierarchy)
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=SimpleNamespace(adb=adb)))

    with patch.object(flow_module, 'touch') as touch:
        runner._action_click({'selector_type': 'resource_id', 'selector': 'app:id/submit'})

    touch.assert_called_once_with((61, 62))
    assert len(adb.commands) == 1


def test_native_visibility_assertions_aggregate_duplicate_matches(tmp_path, monkeypatch):
    hierarchy = '''<hierarchy>
      <node text="Submit" bounds="[1,2][11,12]" visible="false" />
      <node text="Submit" bounds="[21,22][41,42]" visible-to-user="true" />
    </hierarchy>'''
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=SimpleNamespace(adb=FakeADB(hierarchy))))
    visible = {
        'selector_type': 'text', 'selector': 'Submit', 'state': 'visible', 'timeout': 0,
    }
    hidden = {**visible, 'state': 'hidden'}

    runner._action_assert_element(visible)
    with pytest.raises(AssertionError, match='did not reach'):
        runner._action_assert_element(hidden)


def test_native_boolean_assertions_aggregate_all_matching_nodes(tmp_path, monkeypatch):
    hierarchy = '''<hierarchy>
      <node text="Toggle" bounds="[1,2][11,12]" enabled="false" checked="false" />
      <node text="Toggle" bounds="[21,22][41,42]" enabled="true" checked="true" />
    </hierarchy>'''
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=SimpleNamespace(adb=FakeADB(hierarchy))))
    base = {'selector_type': 'text', 'selector': 'Toggle', 'timeout': 0}

    runner._action_assert_element({**base, 'state': 'enabled'})
    runner._action_assert_element({**base, 'state': 'checked'})
    with pytest.raises(AssertionError):
        runner._action_assert_element({**base, 'state': 'disabled'})
    with pytest.raises(AssertionError):
        runner._action_assert_element({**base, 'state': 'unchecked'})


def test_duplicate_native_assertion_fails_closed_on_malformed_state(tmp_path, monkeypatch):
    hierarchy = '''<hierarchy>
      <node text="Target" bounds="[1,2][11,12]" visible="true" />
      <node text="Target" bounds="[21,22][41,42]" enabled="sometimes" />
    </hierarchy>'''
    adb = FakeADB(hierarchy)
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=SimpleNamespace(adb=adb)))

    with pytest.raises(NativeLocatorHierarchyError, match='boolean'):
        runner._action_assert_element({
            'selector_type': 'text', 'selector': 'Target', 'state': 'visible', 'timeout': 0,
        })


def test_empty_native_text_falls_back_to_content_description_with_zero_timeout(tmp_path, monkeypatch):
    hierarchy = '''<hierarchy>
      <node resource-id="app:id/title" text="" content-desc="Accessible title"
            bounds="[1,2][11,12]" />
    </hierarchy>'''
    adb = FakeADB(hierarchy)
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=SimpleNamespace(adb=adb)))
    selector = {'selector_type': 'resource_id', 'selector': 'app:id/title', 'timeout': 0}

    assert runner._read_text_selector(selector, 0) == 'Accessible title'
    runner._action_assert_text({
        **selector, 'expected': 'Accessible title', 'match_mode': 'exact',
    })
    runner._action_extract_text({**selector, 'name': 'title', 'scope': 'local'})

    assert runner.context['local']['title'] == 'Accessible title'
    assert adb.timeouts == [2.0, 2.0, 2.0]


def test_airtest_runtime_timeout_is_sanitized_and_not_misclassified(tmp_path):
    private_output = 'private-device-output'
    airtest_timeout = RuntimeError(
        "Command ['adb', 'shell'] timed out after 0.25 seconds: "
        f"stdout['{private_output}'], stderr['{private_output}']"
    )

    with pytest.raises(NativeLocatorTimeout) as timeout_error:
        native_locator.probe_native_element(FakeADB(airtest_timeout), 'text', 'target', timeout=0.25)

    assert private_output not in str(timeout_error.value)

    with pytest.raises(NativeLocatorUnsupportedDevice) as transport_error:
        native_locator.find_native_element(
            FakeADB(RuntimeError(f'private transport {private_output}')),
            'text', 'target', timeout=0.25, interval=0.05,
        )

    assert private_output not in str(transport_error.value)


def test_native_lookup_retries_an_early_airtest_timeout_within_one_deadline(monkeypatch):
    clock = {'now': 0.0}
    monkeypatch.setattr(native_locator.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(
        native_locator.time, 'sleep',
        lambda delay: clock.__setitem__('now', clock['now'] + delay),
    )
    timeout_error = RuntimeError(
        "Command ['adb'] timed out after 0.1 seconds: stdout[''], stderr['']"
    )
    hierarchy = '<hierarchy><node text="target" bounds="[0,0][10,10]" /></hierarchy>'

    class RecoveringADB:
        def __init__(self):
            self.calls = []

        def cmd(self, commands, timeout=None):
            self.calls.append(timeout)
            if len(self.calls) == 1:
                raise timeout_error
            return hierarchy

    adb = RecoveringADB()
    result = native_locator.find_native_element(
        adb, 'text', 'target', timeout=0.5, interval=0.1,
    )

    assert result is not None
    assert adb.calls == pytest.approx([0.5, 0.4])
    assert clock['now'] <= 0.5


def test_airtest_timeout_retry_stops_at_outer_deadline(monkeypatch):
    clock = {'now': 0.0}
    monkeypatch.setattr(native_locator.time, 'monotonic', lambda: clock['now'])
    timeout_error = RuntimeError(
        "Command ['adb'] timed out after 0.5 seconds: stdout['private'], stderr['private']"
    )

    class TimedOutADB:
        def __init__(self):
            self.calls = []

        def cmd(self, commands, timeout=None):
            self.calls.append(timeout)
            clock['now'] += timeout
            raise timeout_error

    adb = TimedOutADB()
    assert native_locator.find_native_element(
        adb, 'text', 'target', timeout=0.5, interval=0.1,
    ) is None
    assert adb.calls == [0.5]
    assert clock['now'] == 0.5


def test_other_runtime_error_at_deadline_is_not_treated_as_absence(monkeypatch):
    clock = {'now': 0.0}
    monkeypatch.setattr(native_locator.time, 'monotonic', lambda: clock['now'])

    class FailedADB:
        def cmd(self, _commands, timeout=None):
            clock['now'] += timeout
            raise RuntimeError('private non-timeout transport failure')

    with pytest.raises(NativeLocatorUnsupportedDevice):
        native_locator.find_native_element(
            FailedADB(), 'text', 'target', timeout=0.5, interval=0.1,
        )


def test_extract_output_errors_do_not_echo_rendered_source_or_path(tmp_path):
    secret = 'private-extract-reference'
    runner = make_runner(tmp_path)
    runner._init_context(variables=[
        {'name': 'secret_reference', 'value': secret, 'sensitive': True},
        {'name': 'payload', 'value': {'public': True}},
    ], runtime={'retry_times': 0})

    with pytest.raises(ValueError) as source_error:
        runner._action_extract_output({
            'source': secret, 'path': 'public', 'name': 'result',
        })
    assert secret not in str(source_error.value)

    with pytest.raises(ValueError) as path_error:
        runner._action_extract_output({
            'source': 'local.payload', 'path': secret, 'name': 'result',
        })
    assert secret not in str(path_error.value)


def test_ocr_region_errors_do_not_echo_rendered_selector(tmp_path):
    secret = 'private-invalid-ocr-region'
    runner = make_runner(tmp_path)
    runner._init_context(variables=[
        {'name': 'region', 'value': secret, 'sensitive': True},
    ], runtime={'retry_times': 0})

    with pytest.raises(ValueError) as error:
        runner._parse_ocr_region({'selector': secret, 'selector_type': 'region'})

    assert secret not in str(error.value)
    assert str(error.value) == 'Invalid OCR region'


def test_rendered_exception_and_step_name_are_redacted_from_reports(tmp_path, monkeypatch, caplog):
    secret = 'private-rendered-step-name'
    runner = make_runner(tmp_path)
    attach = Mock()
    attach.file = Mock()
    monkeypatch.setattr(flow_module, 'allure', SimpleNamespace(
        step=lambda _title: nullcontext(),
        attach=attach,
        attachment_type=SimpleNamespace(JSON='json', PNG='png'),
    ))
    monkeypatch.setattr(flow_module, 'ALLURE_AVAILABLE', True)
    monkeypatch.setattr(runner, '_capture_screenshot', Mock(return_value=None))
    progress = Mock()

    with pytest.raises(ValueError) as error:
        runner.run(
            [{'type': 'unknown_action', 'name': '{{local.credential}}'}],
            variables=[{'name': 'credential', 'value': secret, 'sensitive': True}],
            runtime={'retry_times': 0, 'stop_on_error': True},
            progress_callback=progress,
        )

    assert str(error.value) == '[REDACTED]'
    assert secret not in caplog.text
    assert all(call.args[2] == '[REDACTED]' for call in progress.call_args_list)
    report_payload = attach.call_args.args[0]
    assert secret not in report_payload
    assert '{{local.credential}}' not in report_payload


def test_try_error_variable_is_redacted_before_storage(tmp_path):
    secret = 'private-caught-exception'
    runner = make_runner(tmp_path)

    runner.run(
        [{
            'type': 'try',
            'try_steps': [{'type': 'unknown_action', 'name': '{{local.credential}}'}],
            'catch_steps': [{'type': 'set_variable', 'name': 'handled', 'value': True}],
        }],
        variables=[{'name': 'credential', 'value': secret, 'sensitive': True}],
        runtime={'retry_times': 0, 'stop_on_error': True},
    )

    assert runner.context['local']['error'] == '[REDACTED]'


def test_sensitive_values_are_redacted_from_run_outputs(tmp_path):
    secret = 'private-output-value'
    runner = make_runner(tmp_path)

    result = runner.run(
        [{
            'type': 'set_variable', 'name': 'token', 'scope': 'outputs',
            'value': '{{local.credential}}',
        }],
        variables=[{'name': 'credential', 'value': secret, 'sensitive': True}],
        runtime={'retry_times': 0},
    )

    assert result['outputs'] == {'token': '[REDACTED]'}
    assert runner.context['outputs']['token'] == secret
