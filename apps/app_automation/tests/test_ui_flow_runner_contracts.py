from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

pytest.importorskip('airtest.core.api', reason='Airtest is required to import the runner')

from apps.app_automation.runners import ui_flow_runner as flow_module
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner


class FakeDevice:
    pass


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


def test_input_clear_first_focuses_then_clears_before_typing(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    device = FakeDevice()
    events = []
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=device))
    monkeypatch.setattr(runner, '_resolve_selector', Mock(return_value=(12, 34)))

    with (
        patch.object(flow_module, 'touch', side_effect=lambda target: events.append(('focus', target))),
        patch.object(flow_module.device_actions, 'clear_focused_input',
                     side_effect=lambda target: events.append(('clear', target))),
        patch.object(flow_module, 'airtest_text', side_effect=lambda value: events.append(('type', value))),
        patch.object(flow_module.time, 'sleep'),
    ):
        runner.run([{
            'type': 'input',
            'config': {'selector': 'field', 'selector_type': 'pos', 'value': 'replacement', 'clear_first': True},
        }], runtime={'retry_times': 0})

    assert events == [('focus', (12, 34)), ('clear', device), ('type', 'replacement')]


def test_blank_swipe_endpoints_select_direction_and_one_sided_endpoint_is_invalid(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    monkeypatch.setattr(
        flow_module, 'G', SimpleNamespace(DEVICE=SimpleNamespace(get_current_resolution=lambda: (100, 200))),
    )
    with patch.object(flow_module, 'swipe') as swipe:
        runner._action_swipe({'start': '', 'end': '  ', 'direction': 'down', 'duration': 0.4})
        with pytest.raises(ValueError, match='both start and end'):
            runner._action_swipe({'start': '1,2', 'end': '', 'direction': 'down'})

    swipe.assert_called_once_with((50, 60), (50, 140), duration=0.4)


@pytest.mark.parametrize(('value_type', 'value', 'expected'), [
    ('string', 12, '12'),
    ('number', '12', 12),
    ('number', '-1.25e1', -12.5),
    ('boolean', 'false', False),
    ('boolean', True, True),
    ('array', '[1, "two", false]', [1, 'two', False]),
    ('object', '{"active": true, "count": 0}', {'active': True, 'count': 0}),
    ('array', [1, {'ok': True}], [1, {'ok': True}]),
])
def test_set_variable_converts_supported_explicit_types(tmp_path, value_type, value, expected):
    runner = make_runner(tmp_path)
    runner.run([{
        'type': 'set_variable', 'name': 'typed', 'value': value, 'value_type': value_type,
    }], runtime={'retry_times': 0, 'stop_on_error': True})
    assert runner.context['local']['typed'] == expected


def test_set_variable_rejects_invalid_typed_input_without_echoing_value(tmp_path):
    runner = make_runner(tmp_path)
    private_value = 'private-not-a-number'
    with pytest.raises(ValueError) as error:
        runner._action_set_variable({
            'name': 'typed', 'value': private_value, 'value_type': 'number',
        })
    assert private_value not in str(error.value)
    assert 'typed' not in runner.context['local']


@pytest.mark.parametrize(('value_type', 'value'), [
    ('number', '01'), ('number', '1e999'), ('boolean', 'yes'),
    ('array', '{}'), ('array', '[NaN]'), ('object', '[]'),
])
def test_set_variable_rejects_non_strict_values(tmp_path, value_type, value):
    runner = make_runner(tmp_path)
    with pytest.raises(ValueError) as error:
        runner._action_set_variable({'name': 'typed', 'value': value, 'value_type': value_type})
    assert value not in str(error.value)
    assert 'typed' not in runner.context['local']


def test_set_variable_without_value_type_preserves_legacy_value(tmp_path):
    runner = make_runner(tmp_path)
    legacy_value = {'nested': [False, 0, '']}
    runner._action_set_variable({'name': 'legacy', 'value': legacy_value})
    assert runner.context['local']['legacy'] is legacy_value


@pytest.mark.parametrize('match_mode', ['contains', 'regex'])
def test_legacy_and_native_text_assertions_reject_empty_expected(tmp_path, match_mode):
    runner = make_runner(tmp_path)
    legacy_step = {
        'selector_type': 'region', 'selector': '0,0,10,10',
        'expected': '', 'match_mode': match_mode,
    }
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', True),
        patch.object(runner, '_parse_ocr_region', return_value=(0, 0, 10, 10)),
        patch.object(runner, '_ocr_recognize_text') as recognize,
        pytest.raises(ValueError, match='non-empty expected'),
    ):
        runner._assert_text(legacy_step)
    recognize.assert_not_called()

    with pytest.raises(ValueError, match='non-empty expected'):
        runner._action_assert_text({'expected': '', 'match_mode': match_mode, 'timeout': 0})


def test_exact_empty_text_assertions_remain_explicitly_valid(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'OCR_AVAILABLE', True)
    with (
        patch.object(runner, '_parse_ocr_region', return_value=(0, 0, 10, 10)),
        patch.object(runner, '_ocr_recognize_text', return_value=''),
    ):
        runner._assert_text({
            'selector_type': 'region', 'selector': '0,0,10,10', 'expected': '', 'match_mode': 'exact',
        })
    with patch.object(runner, '_read_text_selector', return_value=''):
        runner._action_assert_text({'expected': '', 'match_mode': 'exact', 'timeout': 0})


def test_assert_value_validates_raw_left_but_allows_expression_resolving_empty(tmp_path):
    runner = make_runner(tmp_path)
    with pytest.raises(ValueError, match='non-blank left'):
        runner.run([{'type': 'assert_value', 'left': '   ', 'right': ''}],
                   runtime={'retry_times': 0, 'stop_on_error': True})
    with pytest.raises(ValueError, match='non-blank left'):
        runner.run([{'type': 'assert_value', 'config': {'left': ''}}],
                   runtime={'retry_times': 0, 'stop_on_error': True})

    result = runner.run(
        [{'type': 'assert_value', 'left': '{{local.empty}}', 'operator': '==', 'right': ''}],
        variables=[{'name': 'empty', 'value': ''}],
        runtime={'retry_times': 0, 'stop_on_error': True},
    )
    assert result['failed'] == 0
    assert result['assertions'] == 1


def test_assert_response_requires_both_source_and_path(tmp_path):
    runner = make_runner(tmp_path)
    for step in (
        {'source': 'local.response'},
        {'source': '', 'path': 'status'},
        {'source': 'local.response', 'path': '  '},
        {'config': {'source': 'local.response'}},
    ):
        with pytest.raises(ValueError, match='configured source and path'):
            runner._action_assert_response(step)


def test_foreach_assert_contains_rejects_empty_expected_entry(tmp_path):
    runner = make_runner(tmp_path)
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', True),
        pytest.raises(ValueError, match='expected entries must be non-empty'),
    ):
        runner._action_foreach_assert({
            'assert_type': 'text', 'match_mode': 'contains', 'expected_list': [''],
            'click_selector_type': 'pos', 'click_selector': '1,1', 'ocr_selector': '0,0,10,10',
        })


def test_zero_min_match_keeps_legacy_loop_but_does_not_count_as_business_assertion(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    monkeypatch.setattr(flow_module, 'OCR_AVAILABLE', False)
    with (
        patch.object(runner, '_resolve_selector', return_value=(1, 1)),
        patch.object(runner, '_safe_image_path', return_value=str(tmp_path / 'expected.png')),
        patch.object(flow_module.os.path, 'isfile', return_value=True),
        patch.object(flow_module, 'Template', return_value='expected-template'),
        patch.object(flow_module, 'exists', return_value=True),
        patch.object(flow_module, 'touch') as touch,
    ):
        result = runner.run([{
            'type': 'foreach_assert', 'assert_type': 'image', 'expected_list': ['expected.png'],
            'click_selector_type': 'pos', 'click_selector': '1,1',
            'max_loops': 2, 'min_match': 0, 'interval': 0, 'timeout': 0,
        }], runtime={'retry_times': 0, 'stop_on_error': True})

    assert touch.call_count == 2
    assert result['assertions'] == 0


@pytest.mark.parametrize(('failure_kind', 'failure_type'), [
    ('clear', flow_module.device_actions.DeviceActionUncertainError),
    ('secret', flow_module.secret_input.SecretInputError),
])
def test_nested_uncertain_device_action_quarantines_and_skips_retries_catch_finally(
    tmp_path, monkeypatch, failure_kind, failure_type,
):
    runner = make_runner(tmp_path, execution_id=71)
    device = FakeDevice()
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=device))
    quarantine = Mock(return_value=True)
    monkeypatch.setattr(runner, '_quarantine_execution_device', quarantine)
    clear = Mock(side_effect=failure_type('safe uncertain failure'))
    secret = Mock(side_effect=failure_type('safe uncertain failure'))
    monkeypatch.setattr(flow_module.device_actions, 'clear_focused_input', clear)
    monkeypatch.setattr(flow_module.secret_input, 'enter_sensitive_text', secret)
    monkeypatch.setattr(runner, '_attach_allure', Mock())
    type_text = Mock()
    monkeypatch.setattr(flow_module, 'airtest_text', type_text)

    input_step = {
        'type': 'input', 'value': 'private-fixture', 'retry_times': 4,
        'clear_first': failure_kind == 'clear', 'sensitive': failure_kind == 'secret',
    }
    flow = [{
        'type': 'try',
        'try_steps': [{
            'type': 'sequence', 'steps': [{
                'type': 'loop', 'mode': 'count', 'times': 2, 'steps': [input_step],
            }],
        }],
        'catch_steps': [{'type': 'set_variable', 'name': 'caught', 'value': True}],
        'finally_steps': [{'type': 'set_variable', 'name': 'finalized', 'value': True}],
    }, {'type': 'set_variable', 'name': 'after', 'value': True}]

    with pytest.raises(failure_type):
        runner.run(flow, runtime={'retry_times': 3, 'retry_interval': 0, 'stop_on_error': False})

    (clear if failure_kind == 'clear' else secret).assert_called_once()
    quarantine.assert_called_once_with()
    assert 'caught' not in runner.context['local']
    assert 'finalized' not in runner.context['local']
    assert 'after' not in runner.context['local']
    if failure_kind == 'clear':
        secret.assert_not_called()
    else:
        clear.assert_not_called()
    type_text.assert_not_called()


@pytest.mark.parametrize('error_type', [
    flow_module.secret_input.SecretInputValidationError,
    flow_module.secret_input.SecretInputDeviceError,
])
def test_secret_input_preflight_errors_remain_catchable_and_do_not_quarantine(
    tmp_path, monkeypatch, error_type,
):
    runner = make_runner(tmp_path, execution_id=71)
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=FakeDevice()))
    quarantine = Mock()
    monkeypatch.setattr(runner, '_quarantine_execution_device', quarantine)
    validation = Mock(side_effect=error_type('invalid input'))
    monkeypatch.setattr(flow_module.secret_input, 'enter_sensitive_text', validation)

    result = runner.run([{
        'type': 'try',
        'try_steps': [{'type': 'input', 'value': 'private-fixture', 'sensitive': True, 'retry_times': 0}],
        'catch_steps': [{'type': 'set_variable', 'name': 'caught', 'value': True}],
        'finally_steps': [{'type': 'set_variable', 'name': 'finalized', 'value': True}],
    }], runtime={'retry_times': 0, 'stop_on_error': True})

    validation.assert_called_once()
    quarantine.assert_not_called()
    assert runner.context['local']['caught'] is True
    assert runner.context['local']['finalized'] is True
    assert result['failed'] == 0
