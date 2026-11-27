from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
import traceback

import pytest

pytest.importorskip('airtest.core.api', reason='Airtest is required to import the runner')

from apps.app_automation.runners import ui_flow_runner as flow_module
from apps.app_automation.runners.ui_flow_runner import (
    ReadOnlyBusinessAssertionFailure,
    UiFlowRunner,
)


PRIVATE = 'synthetic-private-mode'


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


def _disable_allure(monkeypatch):
    monkeypatch.setattr(flow_module, 'ALLURE_AVAILABLE', False)


def test_sensitive_rendered_error_is_sanitized_before_allure_captures_it(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    recorded_steps = []
    attached_payloads = []

    @contextmanager
    def allure_step(title):
        try:
            yield
        except Exception as error:
            recorded_steps.append((title, error, str(error), ''.join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )))
            raise

    allure = SimpleNamespace(
        step=allure_step,
        attach=Mock(side_effect=lambda payload, **_kwargs: attached_payloads.append(payload)),
        attachment_type=SimpleNamespace(JSON='json', PNG='png'),
    )
    monkeypatch.setattr(flow_module, 'allure', allure)
    monkeypatch.setattr(flow_module, 'ALLURE_AVAILABLE', True)
    monkeypatch.setattr(runner, '_capture_screenshot', Mock(return_value=None))

    with pytest.raises(ValueError) as error:
        runner.run(
            [{'type': 'loop', 'mode': '{{local.credential}}', 'steps': []}],
            variables=[{'name': 'credential', 'value': PRIVATE, 'sensitive': True}],
            runtime={'stop_on_error': True},
        )

    assert len(recorded_steps) == 1
    _, captured_error, captured_message, captured_traceback = recorded_steps[0]
    assert captured_error is error.value
    assert PRIVATE not in str(error.value)
    assert PRIVATE not in repr(error.value.args)
    assert PRIVATE not in captured_message
    assert PRIVATE not in captured_traceback
    assert '[REDACTED]' in str(error.value)
    assert all(PRIVATE not in payload for payload in attached_payloads)


def test_nested_catch_variable_and_finally_keep_secret_out_of_rendered_errors(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    _disable_allure(monkeypatch)

    result = runner.run(
        [{
            'type': 'try',
            'try_steps': [{
                'type': 'sequence',
                'steps': [{'type': 'loop', 'mode': '{{local.credential}}', 'steps': []}],
            }],
            'catch_steps': [{
                'type': 'set_variable', 'name': 'observed_error', 'value': '{{local.error}}',
            }],
            'finally_steps': [{'type': 'set_variable', 'name': 'finalized', 'value': True}],
        }],
        variables=[{'name': 'credential', 'value': PRIVATE, 'sensitive': True}],
        runtime={'stop_on_error': True},
    )

    assert runner.context['local']['error'] == '[REDACTED]'
    assert runner.context['local']['observed_error'] == '[REDACTED]'
    assert runner.context['local']['finalized'] is True
    assert result['failed'] == 0
    assert PRIVATE not in runner.context['local']['error']
    assert PRIVATE not in runner.context['local']['observed_error']


def test_sensitive_integer_conversion_cause_is_redacted_recursively(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    _disable_allure(monkeypatch)

    with pytest.raises(ValueError, match='times must be an integer') as error:
        runner.run(
            [{
                'type': 'loop', 'mode': 'count', 'times': '{{local.credential}}', 'steps': [],
            }],
            variables=[{'name': 'credential', 'value': PRIVATE, 'sensitive': True}],
            runtime={'stop_on_error': True},
        )

    assert isinstance(error.value.__cause__, ValueError)
    assert PRIVATE not in str(error.value)
    assert PRIVATE not in str(error.value.__cause__)
    assert PRIVATE not in repr(error.value.__cause__.args)
    rendered_chain = ''.join(traceback.format_exception(error.value))
    assert PRIVATE not in rendered_chain
    assert 'times must be an integer in the supported range' in rendered_chain


def test_non_sensitive_error_keeps_its_public_diagnostic(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    _disable_allure(monkeypatch)

    with pytest.raises(ValueError, match='^Unsupported loop mode: unsupported-fixture-mode$') as error:
        runner.run(
            [{'type': 'loop', 'mode': 'unsupported-fixture-mode', 'steps': []}],
            runtime={'stop_on_error': True},
        )

    assert error.value.args == ('Unsupported loop mode: unsupported-fixture-mode',)


def test_uncertain_device_error_identity_and_try_control_flow_are_preserved(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, execution_id=71)
    _disable_allure(monkeypatch)
    uncertain = flow_module.device_actions.DeviceActionUncertainError('synthetic uncertain outcome')
    quarantine = Mock(return_value=True)
    monkeypatch.setattr(runner, '_quarantine_execution_device', quarantine)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=object()))
    monkeypatch.setattr(flow_module.device_actions, 'start_application', Mock(side_effect=uncertain))

    with pytest.raises(flow_module.device_actions.DeviceActionUncertainError) as error:
        runner.run([{
            'type': 'try',
            'try_steps': [{'type': 'app_start', 'package_name': 'org.synthetic.app'}],
            'catch_steps': [{'type': 'set_variable', 'name': 'caught', 'value': True}],
            'finally_steps': [{'type': 'set_variable', 'name': 'finalized', 'value': True}],
        }], runtime={'retry_times': 2, 'retry_interval': 0, 'stop_on_error': True})

    assert error.value is uncertain
    quarantine.assert_called_once_with()
    assert 'caught' not in runner.context['local']
    assert 'finalized' not in runner.context['local']


def test_nested_read_only_assertion_still_carries_safe_failure_proof(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    _disable_allure(monkeypatch)

    with pytest.raises(ReadOnlyBusinessAssertionFailure) as error:
        runner.run([{
            'type': 'sequence',
            'steps': [{'type': 'assert_value', 'left': 1, 'operator': '==', 'right': 2}],
        }], runtime={'stop_on_error': True})

    assert isinstance(error.value.__cause__, AssertionError)
    assert error.value.__cause__._ui_flow_read_only_assertion_failure is True
