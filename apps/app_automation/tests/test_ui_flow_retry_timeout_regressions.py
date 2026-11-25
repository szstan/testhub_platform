from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest
import yaml

pytest.importorskip('airtest.core.api', reason='Airtest is required to import the runner')

from apps.app_automation.runners import ui_flow_runner as flow_module
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner


def make_runner(tmp_path):
    runner = UiFlowRunner.__new__(UiFlowRunner)
    runner.image_base_dir = str(tmp_path / 'images')
    runner.screenshots_dir = str(tmp_path / 'screenshots')
    runner.context = {'local': {}, 'global': {}, 'outputs': {}, 'steps': {}}
    runner.runtime = {}
    runner._ocr_helper = None
    runner._assertions_executed = 0
    runner.execution_id = None
    return runner


def test_legacy_click_failure_is_dispatched_once_without_retry(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    monkeypatch.setattr(runner, '_resolve_selector', Mock(return_value=(10, 20)))
    monkeypatch.setattr(runner, '_attach_allure', Mock())
    failure = RuntimeError('click failed')

    with (
        patch.object(flow_module, 'touch', side_effect=failure) as touch,
        patch.object(flow_module, 'sleep') as sleep,
    ):
        result = runner.run([{
            'type': 'click', 'selector_type': 'pos', 'selector': '10,20',
        }])

    assert result['failed'] == 1
    touch.assert_called_once_with((10, 20))
    sleep.assert_not_called()


@pytest.mark.parametrize(('runtime', 'step_retry'), [
    ({'retry_times': 1, 'retry_interval': 0}, None),
    ({'retry_times': 0, 'retry_interval': 0}, 1),
])
def test_explicit_global_or_step_retry_is_preserved(tmp_path, monkeypatch, runtime, step_retry):
    runner = make_runner(tmp_path)
    monkeypatch.setattr(runner, '_resolve_selector', Mock(return_value=(10, 20)))
    monkeypatch.setattr(runner, '_attach_allure', Mock())
    touch = Mock(side_effect=[RuntimeError('transient click failure'), None])
    step = {'type': 'click', 'selector_type': 'pos', 'selector': '10,20'}
    if step_retry is not None:
        step.update({'retry_times': step_retry, 'retry_interval': 0})

    with patch.object(flow_module, 'touch', touch), patch.object(flow_module, 'sleep'):
        result = runner.run([step], runtime=runtime)

    assert result['failed'] == 0
    assert result['passed'] == 1
    assert touch.call_args_list == [call((10, 20)), call((10, 20))]


@pytest.mark.parametrize('field', ['timeout', 'interval'])
def test_foreach_assert_rejects_oversized_timing_before_device_access(tmp_path, field):
    runner = make_runner(tmp_path)
    step = {
        'assert_type': 'image',
        'expected_list': ['expected.png'],
        'click_selector_type': 'pos',
        'click_selector': '1,1',
        field: 1e100,
    }

    with (
        patch.object(flow_module, 'OCR_AVAILABLE', False),
        patch.object(runner, '_resolve_selector') as resolve,
        patch.object(flow_module, 'touch') as touch,
        pytest.raises(ValueError, match='foreach_assert interval and timeout'),
    ):
        runner._action_foreach_assert(step)

    resolve.assert_not_called()
    touch.assert_not_called()


def test_foreach_assert_bounds_expectation_list_before_device_access(tmp_path):
    runner = make_runner(tmp_path)
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', False),
        patch.object(runner, '_resolve_selector') as resolve,
        patch.object(flow_module, 'touch') as touch,
        pytest.raises(ValueError, match='at most 256 entries'),
    ):
        runner._action_foreach_assert({
            'assert_type': 'image',
            'expected_list': [f'image-{index}.png' for index in range(257)],
            'click_selector_type': 'pos', 'click_selector': '1,1',
        })

    resolve.assert_not_called()
    touch.assert_not_called()


@pytest.mark.parametrize(('expected_list', 'message'), [
    pytest.param(['x' * 4097], 'at most 4096 UTF-8 bytes', id='entry-size'),
    pytest.param(['x' * 4096] * 17, 'at most 65536 UTF-8 bytes', id='total-size'),
])
def test_foreach_assert_bounds_expected_text_bytes_before_device_access(
    tmp_path, expected_list, message,
):
    runner = make_runner(tmp_path)
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', True),
        patch.object(runner, '_resolve_selector') as resolve,
        patch.object(flow_module, 'touch') as touch,
        pytest.raises(ValueError, match=message),
    ):
        runner._action_foreach_assert({
            'assert_type': 'text', 'expected_list': expected_list,
            'click_selector_type': 'pos', 'click_selector': '1,1',
            'ocr_selector': '0,0,10,10',
        })

    resolve.assert_not_called()
    touch.assert_not_called()


def test_foreach_assert_rejects_oversized_number_before_conversion_or_device_access(tmp_path):
    runner = make_runner(tmp_path)
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', True),
        patch.object(runner, '_resolve_selector') as resolve,
        patch.object(flow_module, 'touch') as touch,
        pytest.raises(ValueError, match='at most 4096 UTF-8 bytes'),
    ):
        runner._action_foreach_assert({
            'assert_type': 'number', 'expected_list': ['9' * 5000],
            'click_selector_type': 'pos', 'click_selector': '1,1',
            'ocr_selector': '0,0,10,10',
        })

    resolve.assert_not_called()
    touch.assert_not_called()


@pytest.mark.parametrize(('exists', 'image_size', 'count', 'message'), [
    (False, 1, 1, 'Expected image does not exist'),
    (True, 16 * 1024 * 1024 + 1, 1, 'Expected image exceeds'),
    (True, 16 * 1024 * 1024, 5, 'Expected images exceed'),
])
def test_foreach_assert_rejects_invalid_images_before_native_lookup(
    tmp_path, exists, image_size, count, message,
):
    runner = make_runner(tmp_path)
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', False),
        patch.object(runner, '_resolve_selector', return_value=(1, 1)) as resolve,
        patch.object(flow_module.os.path, 'isfile', return_value=exists),
        patch.object(flow_module.os.path, 'getsize', return_value=image_size),
        patch.object(flow_module, 'Template', return_value='expected-template'),
        patch.object(flow_module, 'touch') as touch,
        pytest.raises(ValueError, match=message),
    ):
        runner._action_foreach_assert({
            'assert_type': 'image',
            'expected_list': [f'expected-{index}.png' for index in range(count)],
            'click_selector_type': 'resource_id', 'click_selector': 'com.example.fixture:id/submit',
        })

    resolve.assert_not_called()
    touch.assert_not_called()


def test_foreach_assert_expiring_preparation_budget_never_starts_native_lookup(tmp_path):
    runner = make_runner(tmp_path)
    after_template = [None]

    def load_template(*_args, **_kwargs):
        after_template[0] = iter((0.4, 0.4, 0.6))
        return 'expected-template'

    def monotonic():
        return next(after_template[0], 0.6) if after_template[0] is not None else 0.0

    with (
        patch.object(flow_module.time, 'monotonic', side_effect=monotonic),
        patch.object(flow_module, 'OCR_AVAILABLE', False),
        patch.object(flow_module.os.path, 'isfile', return_value=True),
        patch.object(flow_module.os.path, 'getsize', return_value=1),
        patch.object(flow_module, 'Template', side_effect=load_template),
        patch.object(runner, '_require_device', return_value=SimpleNamespace(adb=object())),
        patch.object(flow_module.native_locator, 'find_native_element',
                     return_value=SimpleNamespace(center=(1, 1))) as lookup,
        patch.object(flow_module, 'touch') as touch,
        pytest.raises(TimeoutError, match='preparation exceeded its deadline'),
    ):
        runner._action_foreach_assert({
            'assert_type': 'image', 'expected_list': ['expected.png'],
            'click_selector_type': 'resource_id', 'click_selector': 'com.example.fixture:id/submit',
            'timeout': 0.5,
        })

    lookup.assert_not_called()
    touch.assert_not_called()


def test_foreach_assert_template_preparation_consumes_the_deadline(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    now = [0.0]

    def load_template(*_args, **_kwargs):
        now[0] = 0.6
        return 'expected-template'

    monkeypatch.setattr(flow_module.time, 'monotonic', lambda: now[0])
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', False),
        patch.object(runner, '_resolve_selector', return_value=(1, 1)),
        patch.object(flow_module.os.path, 'isfile', return_value=True),
        patch.object(flow_module.os.path, 'getsize', return_value=1),
        patch.object(flow_module, 'Template', side_effect=load_template),
        patch.object(flow_module, 'touch') as touch,
        pytest.raises(TimeoutError, match='template preparation exceeded its deadline'),
    ):
        runner._action_foreach_assert({
            'assert_type': 'image', 'expected_list': ['expected.png'],
            'click_selector_type': 'pos', 'click_selector': '1,1',
            'max_loops': 1, 'interval': 0, 'timeout': 0.5,
        })

    touch.assert_not_called()


def test_foreach_assert_ocr_setup_consumes_the_deadline(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    now = [0.0]

    def setup_ocr():
        now[0] = 0.6
        return SimpleNamespace(recognize_region_text=Mock(return_value='ready'))

    monkeypatch.setattr(flow_module.time, 'monotonic', lambda: now[0])
    runner._get_ocr_helper = Mock(side_effect=setup_ocr)
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', True),
        patch.object(runner, '_resolve_selector', return_value=(1, 1)),
        patch.object(flow_module, 'touch') as touch,
        pytest.raises(TimeoutError, match='OCR preparation exceeded its deadline'),
    ):
        runner._action_foreach_assert({
            'assert_type': 'text', 'expected_list': ['ready'],
            'click_selector_type': 'pos', 'click_selector': '1,1',
            'ocr_selector': '0,0,10,10', 'max_loops': 1,
            'interval': 0, 'timeout': 0.5,
        })

    runner._get_ocr_helper.assert_called_once()
    touch.assert_not_called()


def test_foreach_assert_checks_deadline_during_expectation_matching(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    matching_started = [False]
    matching_checks = [0]

    def fake_monotonic():
        if not matching_started[0]:
            return 0.0
        matching_checks[0] += 1
        return matching_checks[0] * 0.1

    def recognize(_region, *, deadline):
        matching_started[0] = True
        return 'not-matched'

    monkeypatch.setattr(flow_module.time, 'monotonic', fake_monotonic)
    ocr = SimpleNamespace(recognize_region_text=Mock(side_effect=recognize))
    runner._get_ocr_helper = Mock(return_value=ocr)
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', True),
        patch.object(runner, '_resolve_selector', return_value=(1, 1)),
        patch.object(flow_module, 'touch') as touch,
        pytest.raises(AssertionError, match='got 0'),
    ):
        runner._action_foreach_assert({
            'assert_type': 'text', 'match_mode': 'exact',
            'expected_list': [f'expected-{index}' for index in range(20)],
            'click_selector_type': 'pos', 'click_selector': '1,1',
            'ocr_selector': '0,0,10,10', 'max_loops': 1,
            'interval': 0, 'timeout': 0.5,
        })

    touch.assert_called_once_with((1, 1))
    ocr.recognize_region_text.assert_called_once()
    assert matching_checks[0] == 5


def test_zero_timeout_count_mode_has_a_bounded_deadline(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    now = [0.0]
    waits = []

    def fake_sleep(duration):
        waits.append(duration)
        now[0] += duration

    monkeypatch.setattr(flow_module.time, 'monotonic', lambda: now[0])
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', False),
        patch.object(runner, '_resolve_selector', return_value=(1, 1)),
        patch.object(flow_module.os.path, 'isfile', return_value=True),
        patch.object(flow_module.os.path, 'getsize', return_value=1),
        patch.object(flow_module, 'Template', return_value='expected-template'),
        patch.object(runner, '_image_lookup_once', return_value=(2, 3)),
        patch.object(flow_module, 'touch') as touch,
        patch.object(flow_module.time, 'sleep', side_effect=fake_sleep),
    ):
        runner._action_foreach_assert({
            'assert_type': 'image', 'expected_list': ['expected.png'],
            'click_selector_type': 'pos', 'click_selector': '1,1',
            'max_loops': 5, 'min_match': 0, 'interval': 300, 'timeout': 0,
        })

    assert touch.call_count == 1
    assert waits == [300]


def test_foreach_assert_uses_default_yaml_timing_values(tmp_path):
    pack_path = Path(__file__).resolve().parents[3] / 'apps/core/management/commands/ui-component-pack.yaml'
    pack = yaml.safe_load(pack_path.read_text(encoding='utf-8'))
    defaults = next(
        component['default_config']
        for component in pack['components']
        if component['type'] == 'foreach_assert'
    )
    runner = make_runner(tmp_path)
    ocr = SimpleNamespace(recognize_region_text=Mock(return_value='ready'))
    runner._get_ocr_helper = Mock(return_value=ocr)
    config = {
        **defaults,
        'expected_list': ['ready'],
        'click_selector_type': 'pos',
        'click_selector': '1,1',
        'ocr_selector': '0,0,10,10',
    }

    with (
        patch.object(flow_module, 'OCR_AVAILABLE', True),
        patch.object(runner, '_resolve_selector', return_value=(1, 1)),
        patch.object(flow_module, 'touch') as touch,
        patch.object(flow_module.time, 'sleep') as sleep,
    ):
        runner._action_foreach_assert({'type': 'foreach_assert', 'config': config})

    assert touch.call_count == defaults['max_loops']
    assert sleep.call_args_list == [call(defaults['interval'])] * defaults['max_loops']
    assert ocr.recognize_region_text.call_count == defaults['max_loops']
