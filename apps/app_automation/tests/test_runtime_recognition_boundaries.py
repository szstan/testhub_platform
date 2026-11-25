"""Device-free regressions for runtime recognition and mutation boundaries."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

pytest.importorskip('airtest.core.api')
from PIL import Image

from apps.app_automation import device_actions
from apps.app_automation.bounded_inference import BoundedInference, InferenceBusy, InferenceTimeout
from apps.app_automation.runners import ui_flow_runner as flow_module
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner
from apps.app_automation.tests import test_app_flow
from apps.app_automation.utils import ocr_helper as ocr_module
from apps.app_automation.utils.ocr_helper import OCRHelper


@pytest.fixture
def runner(tmp_path, monkeypatch):
    instance = UiFlowRunner.__new__(UiFlowRunner)
    instance.image_base_dir = str(tmp_path / 'images')
    instance.screenshots_dir = str(tmp_path / 'screenshots')
    instance.execution_id = None
    instance._ocr_helper = None
    instance._attach_allure = Mock()
    instance._init_context([], {})
    monkeypatch.setattr(flow_module, '_IMAGE_INFERENCE', BoundedInference('test-image'))
    monkeypatch.setattr(ocr_module, '_OCR_INFERENCE', BoundedInference('test-ocr'))
    monkeypatch.setattr(OCRHelper, '_ocr_cache', {})
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=SimpleNamespace()))
    return instance


def install_image_lookup(monkeypatch, template):
    capture = Mock(return_value=b'in-memory-image')
    monkeypatch.setattr(device_actions, 'capture_screenshot', capture)
    monkeypatch.setattr(flow_module.aircv_utils, 'string_2_img', lambda data: data)
    return capture


def await_worker_exit(worker):
    # Admission must stay held after the caller times out and be reusable on exit.
    assert worker._admission.acquire(timeout=1)
    worker._admission.release()


@pytest.mark.parametrize('mode', ['condition', ' Condition ', 'CONDITION', '{{local.mode}}'])
@pytest.mark.parametrize('nested_config', [False, True])
def test_effective_condition_mode_rechecks_values_after_each_iteration(runner, mode, nested_config):
    options = {
        'mode': mode, 'left': '{{local.enabled}}', 'operator': 'truthy', 'max_loops': 3,
        'steps': [{'type': 'set_variable', 'name': 'enabled', 'value': False}],
    }
    step = {'type': 'loop', 'config': options} if nested_config else {'type': 'loop', **options}
    with patch.object(runner, '_action_set_variable', wraps=runner._action_set_variable) as assign:
        result = runner.run([step], variables=[
            {'name': 'enabled', 'value': True}, {'name': 'mode', 'value': ' CoNdItIoN '},
        ], runtime={'stop_on_error': True})
    assert result['passed'] == 1
    assert assign.call_count == 1
    assert runner.context['local']['enabled'] is False


def test_bounded_inference_timeout_does_not_queue_more_workers():
    worker = BoundedInference('test-blocked-cpu')
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked():
        calls.append(1)
        entered.set()
        release.wait(1)
        return 'late value'

    started = time.monotonic()
    try:
        with pytest.raises(InferenceTimeout):
            worker.run(blocked, started + 0.05)
        assert entered.is_set()
        assert time.monotonic() - started < 0.5
        for _ in range(5):
            with pytest.raises(InferenceBusy):
                worker.run(blocked, time.monotonic() + 0.1)
        assert calls == [1]
    finally:
        release.set()
        await_worker_exit(worker)
    assert worker.run(lambda: 'fresh value', time.monotonic() + 1) == 'fresh value'


def test_image_inference_timeout_is_bounded_and_busy_calls_fail_closed(runner, monkeypatch):
    release = threading.Event()
    template = SimpleNamespace(match_in=Mock(side_effect=lambda _image: release.wait(1)))
    capture = install_image_lookup(monkeypatch, template)
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, device_actions.DeviceActionError), match='deadline'):
            runner._image_lookup_once(template, timeout=0.05)
        assert time.monotonic() - started < 0.5
        with pytest.raises(device_actions.DeviceActionError, match='previous request'):
            runner._image_lookup_once(template, timeout=0.1)
        assert template.match_in.call_count == 1
        assert capture.call_count == 2
    finally:
        release.set()
        await_worker_exit(flow_module._IMAGE_INFERENCE)


def test_slow_screenshot_never_starts_cpu_work_or_mutation(runner, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(device_actions.time, 'monotonic', lambda: now[0])

    def late_command(argv, **kwargs):
        assert threading.current_thread() is threading.main_thread()
        now[0] += kwargs['timeout']
        return b'late PNG'

    adb = SimpleNamespace(cmd=Mock(side_effect=late_command))
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=SimpleNamespace(adb=adb)))
    template = SimpleNamespace(match_in=Mock(return_value=(1, 2)))
    with pytest.raises(device_actions.DeviceActionError, match='screenshot timed out'):
        runner._image_lookup_once(template, timeout=0.1)
    template.match_in.assert_not_called()
    assert adb.cmd.call_args == call(['shell', 'screencap', '-p'], timeout=0.1, ensure_unicode=False)


def test_late_image_match_is_not_accepted(runner, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(flow_module.time, 'monotonic', lambda: now[0])

    def match(_image):
        now[0] = 0.2
        return (1, 2)

    template = SimpleNamespace(match_in=Mock(side_effect=match))
    install_image_lookup(monkeypatch, template)
    with pytest.raises((TimeoutError, device_actions.DeviceActionError), match='deadline'):
        runner._resolve_action_target(template, timeout=0.1)


@pytest.mark.parametrize('method', ['_action_wait', '_action_wait_element', '_action_assert_element'])
@pytest.mark.parametrize('timeout', [0, 0.1])
def test_image_poll_rejects_success_returned_after_outer_deadline(runner, monkeypatch, method, timeout):
    now = [0.0]
    monkeypatch.setattr(flow_module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(runner, '_resolve_selector', Mock(return_value=object()))
    budgets = []

    def late_match(_target, *, timeout):
        budgets.append(timeout)
        now[0] += timeout + 0.01
        return (1, 2)

    monkeypatch.setattr(runner, '_image_lookup_once', late_match)
    with pytest.raises(AssertionError):
        getattr(runner, method)({'selector_type': 'image', 'selector': 'a.png', 'timeout': timeout})
    assert budgets == [2.0 if timeout == 0 else 0.1]


def test_zero_timeout_image_state_performs_one_usable_probe(runner, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(flow_module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(runner, '_resolve_selector', Mock(return_value=object()))

    def quick_match(_target, *, timeout):
        assert timeout == 2.0
        now[0] += 0.05
        return (1, 2)

    match = Mock(side_effect=quick_match)
    monkeypatch.setattr(runner, '_image_lookup_once', match)
    runner._action_assert_element({'selector_type': 'image', 'selector': 'a.png', 'timeout': 0})
    match.assert_called_once()


@pytest.mark.parametrize('late_stage', ['decode', 'readtext'])
def test_ocr_timeout_never_accepts_or_caches_late_text(runner, monkeypatch, late_stage):
    release = threading.Event()
    entered = threading.Event()
    reader = Mock()
    monkeypatch.setattr(ocr_module, 'EASYOCR_AVAILABLE', True)
    monkeypatch.setattr(OCRHelper, 'get_easyocr_reader', Mock(return_value=reader))
    monkeypatch.setattr(ocr_module, 'G', SimpleNamespace(DEVICE=object()))
    capture = Mock(side_effect=lambda *_args, **_kw: (
        b'PNG' if threading.current_thread() is threading.main_thread() else pytest.fail('device I/O in worker')
    ))
    monkeypatch.setattr(device_actions, 'capture_screenshot', capture)
    result = [([[0, 0], [1, 0], [1, 1], [0, 1]], 'private late text', 0.99)]

    def block_then_result(*_args):
        entered.set()
        release.wait(1)
        return result

    if late_stage == 'readtext':
        reader.readtext.side_effect = block_then_result
        run = lambda: OCRHelper().recognize_text(Image.new('RGB', (10, 10)), deadline=time.monotonic() + 0.05)
    else:
        monkeypatch.setattr(ocr_module.cv2, 'imdecode', block_then_result)
        run = lambda: OCRHelper.crop_region((0, 0, 2, 2), deadline=time.monotonic() + 0.05)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match='deadline'):
            run()
        assert entered.is_set()
        assert time.monotonic() - started < 0.5
        with pytest.raises(RuntimeError, match='previous request'):
            run()
    finally:
        release.set()
        await_worker_exit(ocr_module._OCR_INFERENCE)
    assert OCRHelper._ocr_cache == {}
    if late_stage == 'decode':
        assert capture.call_count == 2
        reader.readtext.assert_not_called()
    else:
        reader.readtext.assert_called_once()


def test_region_ocr_shares_one_deadline_and_captures_on_calling_thread(runner, monkeypatch):
    import cv2
    import numpy as np

    success, encoded = cv2.imencode('.png', np.zeros((20, 20, 3), dtype=np.uint8))
    assert success
    monkeypatch.setattr(ocr_module, 'EASYOCR_AVAILABLE', True)
    monkeypatch.setattr(ocr_module, 'G', SimpleNamespace(DEVICE=object()))
    thread_ids = []
    monkeypatch.setattr(device_actions, 'capture_screenshot', lambda *_args, **_kw: (
        thread_ids.append(threading.get_ident()) or encoded.tobytes()
    ))
    reader = SimpleNamespace(readtext=Mock(return_value=[(
        [[0, 0], [1, 0], [1, 1], [0, 1]], 'ready', 0.99,
    )]))
    monkeypatch.setattr(OCRHelper, 'get_easyocr_reader', Mock(return_value=reader))
    assert OCRHelper().recognize_region_text((0, 0, 10, 10), deadline=time.monotonic() + 1) == 'ready'
    assert thread_ids == [threading.get_ident()]
    reader.readtext.assert_called_once()


def test_zero_timeout_permission_handles_a_present_system_button_in_one_dump(monkeypatch):
    xml = '<hierarchy><node resource-id="com.android.permissioncontroller:id/permission_allow_button" bounds="[0,0][20,40]" /></hierarchy>'
    adb = SimpleNamespace(cmd=Mock(return_value=xml))
    device = SimpleNamespace(adb=adb, touch=Mock())
    assert device_actions.handle_permission(device, 'allow', timeout=0) is True
    adb.cmd.assert_called_once_with(['shell', 'uiautomator', 'dump', '/dev/tty'], timeout=2.0)
    device.touch.assert_called_once_with((10, 20))


def test_chain_resolves_fallback_after_main_click(runner, monkeypatch):
    main = SimpleNamespace(match_in=Mock(return_value=(1, 2)))
    fallback = SimpleNamespace(match_in=Mock(return_value=(3, 4)))
    monkeypatch.setattr(runner, '_resolve_selector', Mock(side_effect=[main, fallback]))
    install_image_lookup(monkeypatch, main)
    events = []
    main.match_in.side_effect = lambda _image: events.append('locate-main') or (1, 2)
    fallback.match_in.side_effect = lambda _image: events.append('locate-fallback') or (3, 4)
    monkeypatch.setattr(flow_module, 'touch', lambda point: events.append(('touch', point)))
    with test_app_flow._guard_airtest_mutations():
        runner._action_image_exists_click_chain({})
    assert events == ['locate-main', ('touch', (1, 2)), 'locate-fallback', ('touch', (3, 4))]


@pytest.mark.parametrize('found', [False, True])
def test_real_worker_clears_only_predispatch_image_failure(runner, monkeypatch, found):
    template = SimpleNamespace(match_in=Mock(return_value=(1, 2) if found else None))
    install_image_lookup(monkeypatch, template)
    monkeypatch.setattr(runner, '_resolve_selector', Mock(return_value=template))
    quarantine = Mock()
    monkeypatch.setattr(runner, '_quarantine_execution_device', quarantine)
    case = SimpleNamespace(pk=1, name='image case', app_package=None,
        ui_flow=[{'type': 'click', 'selector_type': 'image', 'selector': 'missing.png'}],
        variables=[], runtime_config={})
    execution = SimpleNamespace(pk=77, dataset=None, user=None)
    clear = Mock()
    touch = Mock(side_effect=AssertionError('synthetic dispatched touch failure'))
    with (
        patch.object(test_app_flow.AppTestCase.objects, 'get', return_value=case),
        patch.object(test_app_flow.AppTestExecution.objects, 'select_related', return_value=SimpleNamespace(get=Mock(return_value=execution))),
        patch.object(test_app_flow, 'prepare_scene_execution', return_value=(case.ui_flow, [])),
        patch.object(test_app_flow, 'UiFlowRunner', return_value=runner),
        patch.object(test_app_flow, '_clear_device_action_marker', clear),
        patch.object(test_app_flow, '_make_progress_callback', return_value=Mock()),
        patch.object(test_app_flow.allure.dynamic, 'title'),
        patch.object(test_app_flow.allure.dynamic, 'suite'),
        patch.object(flow_module, 'touch', touch),
        pytest.raises(Exception) as error,
    ):
        test_app_flow.TestAppFlow().test_execute_ui_flow(1, '', 77, SimpleNamespace(open_app=Mock()), 'fixture-user')
    if found:
        assert isinstance(error.value, device_actions.DeviceActionUncertainError)
        touch.assert_called_once_with((1, 2))
        quarantine.assert_called_once()
        clear.assert_not_called()
    else:
        assert not isinstance(error.value, device_actions.DeviceActionUncertainError)
        touch.assert_not_called()
        quarantine.assert_not_called()
        clear.assert_called_once_with(77)


@pytest.mark.parametrize('selector_type', ['region', 'text'])
def test_assert_text_rejects_late_matching_text(runner, monkeypatch, selector_type):
    now = [0.0]
    monkeypatch.setattr(flow_module.time, 'monotonic', lambda: now[0])

    def late_text(*_args, **_kwargs):
        now[0] += 0.3
        return 'ready'

    monkeypatch.setattr(runner, '_read_text_selector', late_text)
    with pytest.raises(AssertionError, match='timed out'):
        runner._action_assert_text({
            'selector_type': selector_type, 'selector': '0,0,10,10',
            'expected': 'ready', 'timeout': 0.1,
        })


@pytest.mark.parametrize('assert_type', ['image', 'text'])
def test_foreach_assert_does_not_count_a_late_match(runner, monkeypatch, assert_type):
    now = [0.0]
    monkeypatch.setattr(flow_module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(flow_module, 'OCR_AVAILABLE', True)
    monkeypatch.setattr(runner, '_resolve_selector', Mock(return_value=(1, 1)))
    monkeypatch.setattr(flow_module.os.path, 'isfile', lambda _path: True)
    monkeypatch.setattr(flow_module.os.path, 'getsize', lambda _path: 1)
    monkeypatch.setattr(flow_module, 'Template', lambda *_args, **_kwargs: object())
    touch = Mock()
    monkeypatch.setattr(flow_module, 'touch', touch)

    def late_match(*_args, **_kwargs):
        now[0] += 0.3
        return (2, 3) if assert_type == 'image' else 'ready'

    monkeypatch.setattr(runner, '_image_lookup_once', late_match)
    monkeypatch.setattr(runner, '_get_ocr_helper', lambda: SimpleNamespace(recognize_region_text=late_match))
    with pytest.raises(AssertionError, match='got 0'):
        runner._action_foreach_assert({
            'assert_type': assert_type, 'expected_list': ['ready'],
            'click_selector_type': 'pos', 'click_selector': '1,1',
            'ocr_selector': '0,0,10,10', 'max_loops': 5, 'min_match': 1,
            'interval': 0, 'timeout': 0.1,
        })
    touch.assert_called_once_with((1, 1))
