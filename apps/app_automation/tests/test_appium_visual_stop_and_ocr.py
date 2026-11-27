from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

pytest.importorskip('airtest.core.api', reason='Airtest is required to import the runner')

from apps.app_automation import constants
from apps.app_automation.models import (
    AppElement,
    AppPackage,
    AppProject,
    AppTestCase,
    AppTestExecution,
)
from apps.app_automation.runners import ui_flow_runner as flow_module
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner
from apps.app_automation.scene_execution import check_appium_prerequisites, prepare_scene_execution
from apps.users.models import User


PACKAGE = 'com.example.visualstop'


@pytest.fixture
def appium_ocr_scene(db):
    owner = User.objects.create_user(username='appium-visual-stop-owner')
    project = AppProject.objects.create(
        name='appium visual stop project', owner=owner, appium_enabled=True,
    )
    package = AppPackage.objects.create(
        name='Visual stop app', package_name=PACKAGE, created_by=owner,
    )
    case = AppTestCase.objects.create(
        project=project, app_package=package, created_by=owner,
        name='linked OCR region case', ui_flow=[],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=owner)
    return SimpleNamespace(owner=owner, project=project, package=package, case=case, execution=execution)


def make_appium_runner(tmp_path, device_guard):
    runner = UiFlowRunner(
        image_base_dir=str(tmp_path), appium_driver=SimpleNamespace(), device_guard=device_guard,
    )
    runner._require_device = Mock(return_value=SimpleNamespace())
    runner._attach_allure = Mock()
    return runner


def test_appium_visual_chain_checks_stop_between_touches(tmp_path, monkeypatch):
    stopped = [False]

    def guard():
        if stopped[0]:
            raise RuntimeError('Execution has stopped')

    runner = make_appium_runner(tmp_path, guard)
    touches = Mock(side_effect=lambda _point: stopped.__setitem__(0, True))
    monkeypatch.setattr(flow_module, 'touch', touches)
    monkeypatch.setattr(flow_module.time, 'sleep', Mock())

    with pytest.raises(RuntimeError, match='Execution has stopped'):
        runner.run([{
            'type': 'image_exists_click_chain',
            'selector_type': 'pos', 'selector': '10,20',
            'fallback_selector_type': 'pos', 'fallback_selector': '30,40',
        }], runtime={'stop_on_error': True})

    touches.assert_called_once_with((10, 20))


def native_click_chain():
    return {
        'type': 'image_exists_click_chain',
        'selector_type': 'resource_id', 'selector': f'{PACKAGE}:id/next',
        'fallback_selector_type': 'resource_id', 'fallback_selector': f'{PACKAGE}:id/confirm',
        'timeout': 0,
    }


@pytest.mark.parametrize('visible_before_navigation', [False, True])
def test_appium_chain_resolves_the_second_native_target_on_the_new_screen(
    tmp_path, monkeypatch, visible_before_navigation,
):
    order = []
    first = Mock()
    old_confirm = Mock()
    current_confirm = Mock()
    for element in (first, old_confirm, current_confirm):
        element.is_displayed.return_value = True
        element.is_enabled.return_value = True
    first.click.side_effect = lambda: order.append('navigate')
    current_confirm.click.side_effect = lambda: order.append('confirm')

    def find_elements(by, value):
        assert by == 'id'
        if value == f'{PACKAGE}:id/next':
            return [first]
        assert value == f'{PACKAGE}:id/confirm'
        order.append('lookup confirm')
        if 'navigate' in order:
            return [current_confirm]
        return [old_confirm] if visible_before_navigation else []

    runner = make_appium_runner(tmp_path, Mock())
    runner.appium_driver = SimpleNamespace(current_package=PACKAGE, find_elements=find_elements)
    monkeypatch.setattr(flow_module.time, 'sleep', Mock())
    airtest_touch = Mock()
    monkeypatch.setattr(flow_module, 'touch', airtest_touch)

    runner.run([native_click_chain()], runtime={'stop_on_error': True, 'package_name': PACKAGE})

    assert order == ['navigate', 'lookup confirm', 'confirm']
    old_confirm.click.assert_not_called()
    airtest_touch.assert_not_called()
    runner._require_device.assert_not_called()


def test_appium_chain_uses_current_resolution_for_relative_fallback(tmp_path, monkeypatch):
    resolution = [(100, 200)]
    first = Mock()
    first.is_displayed.return_value = True
    first.is_enabled.return_value = True
    first.click.side_effect = lambda: resolution.__setitem__(0, (200, 100))
    runner = make_appium_runner(tmp_path, Mock())
    runner.appium_driver = SimpleNamespace(current_package=PACKAGE, find_elements=Mock(return_value=[first]))
    runner._device_resolution = Mock(side_effect=lambda: resolution[0])
    monkeypatch.setattr(flow_module.time, 'sleep', Mock())
    touch = Mock()
    monkeypatch.setattr(flow_module, 'touch', touch)
    step = {**native_click_chain(), 'fallback_selector_type': 'relative', 'fallback_selector': '0.8,0.5'}

    runner.run([step], runtime={'stop_on_error': True, 'package_name': PACKAGE})

    first.click.assert_called_once_with()
    runner._device_resolution.assert_called_once_with()
    touch.assert_called_once_with((159, 50))


def relative_visual_click_chain():
    return {
        'type': 'image_exists_click_chain',
        'selector_type': 'pos', 'selector': '10,20',
        'fallback_selector_type': 'relative', 'fallback_selector': '0.8,0.5',
    }


@pytest.fixture(params=['legacy', 'appium'])
def visual_chain_runner(tmp_path, monkeypatch, request):
    if request.param == 'appium':
        runner = make_appium_runner(tmp_path, Mock())
    else:
        runner = UiFlowRunner(image_base_dir=str(tmp_path))
        runner._attach_allure = Mock()
    touches = Mock()
    monkeypatch.setattr(flow_module, 'touch', touches)
    monkeypatch.setattr(flow_module.time, 'sleep', Mock())
    monkeypatch.setattr(flow_module, 'sleep', Mock())
    return runner, touches


def test_click_chain_does_not_replay_main_touch_after_relative_resolution_failure(visual_chain_runner):
    runner, touches = visual_chain_runner
    runner._device_resolution = Mock(side_effect=[RuntimeError('transient resolution failure'), (200, 100)])

    with pytest.raises(RuntimeError, match='transient resolution failure'):
        runner.run([relative_visual_click_chain()], runtime={'retry_times': 1, 'retry_interval': 0, 'stop_on_error': True})

    touches.assert_called_once_with((10, 20))
    runner._device_resolution.assert_called_once_with()


@pytest.mark.parametrize('error_handler', ['catch_steps', 'finally_steps'])
def test_nested_click_chain_does_not_replay_when_error_handler_raises(visual_chain_runner, error_handler):
    runner, touches = visual_chain_runner
    runner._device_resolution = Mock(side_effect=[RuntimeError('transient resolution failure'), (200, 100)])
    flow = [
        {'type': 'set_variable', 'name': 'fallback_reached', 'value': False},
        {'type': 'sequence', 'steps': [{
            'type': 'try',
            'try_steps': [
                relative_visual_click_chain(),
                {'type': 'set_variable', 'name': 'fallback_reached', 'value': True},
            ],
            error_handler: [{
                'type': 'assert_value', 'left': '{{local.fallback_reached}}', 'right': True, 'retry_times': 0,
            }],
        }]},
    ]

    with pytest.raises(AssertionError, match='Value assertion failed'):
        runner.run(flow, runtime={'retry_times': 1, 'retry_interval': 0, 'stop_on_error': True})

    touches.assert_called_once_with((10, 20))
    runner._device_resolution.assert_called_once_with()


def test_handled_click_chain_failure_allows_later_step_to_retry_before_touch(visual_chain_runner):
    runner, touches = visual_chain_runner
    runner._device_resolution = Mock(side_effect=[
        RuntimeError('first chain failed after the main touch'),
        RuntimeError('second chain failed before the main touch'),
        (200, 100),
        (200, 100),
    ])
    flow = [
        {
            'type': 'try',
            'try_steps': [relative_visual_click_chain()],
            'catch_steps': [{'type': 'set_variable', 'name': 'handled', 'value': True}],
            'finally_steps': [{'type': 'set_variable', 'name': 'finalized', 'value': True}],
        },
        {**relative_visual_click_chain(), 'selector_type': 'relative', 'selector': '0.1,0.1'},
    ]

    result = runner.run(flow, runtime={'retry_times': 1, 'retry_interval': 0, 'stop_on_error': True})

    assert result['passed'] == 2
    assert runner.context['local']['handled'] is True
    assert runner.context['local']['finalized'] is True
    assert [call.args[0] for call in touches.call_args_list] == [(10, 20), (20, 10), (159, 50)]
    assert runner._device_resolution.call_count == 4


def test_appium_chain_rejects_bad_relative_fallback_before_the_first_click(tmp_path):
    runner = make_appium_runner(tmp_path, Mock())
    find_elements = Mock()
    runner.appium_driver = SimpleNamespace(current_package=PACKAGE, find_elements=find_elements)
    runner._device_resolution = Mock()
    step = {**native_click_chain(), 'fallback_selector_type': 'relative', 'fallback_selector': '1.2,0.5'}

    with pytest.raises(ValueError, match='Relative selector'):
        runner.run([step], runtime={'stop_on_error': True, 'package_name': PACKAGE})

    find_elements.assert_not_called()
    runner._device_resolution.assert_not_called()


def test_appium_chain_clicks_fallback_when_main_is_absent(tmp_path):
    fallback = Mock()
    fallback.is_displayed.return_value = True
    fallback.is_enabled.return_value = True
    find_elements = Mock(side_effect=[[], [fallback]])
    runner = make_appium_runner(tmp_path, Mock())
    runner.appium_driver = SimpleNamespace(current_package=PACKAGE, find_elements=find_elements)

    runner.run([native_click_chain()], runtime={'stop_on_error': True, 'package_name': PACKAGE})

    fallback.click.assert_called_once_with()
    assert find_elements.call_count == 2


def test_appium_chain_does_not_retry_after_dispatching_the_first_click(tmp_path, monkeypatch):
    first = Mock()
    first.is_displayed.return_value = True
    first.is_enabled.return_value = True
    find_elements = Mock(side_effect=lambda _by, value: [first] if value.endswith('/next') else [])
    runner = make_appium_runner(tmp_path, Mock())
    runner.appium_driver = SimpleNamespace(current_package=PACKAGE, find_elements=find_elements)
    monkeypatch.setattr(flow_module.time, 'sleep', Mock())

    with pytest.raises(flow_module.appium_locator.AppiumLocatorFailure, match='did not find'):
        runner.run([native_click_chain()], runtime={
            'stop_on_error': True, 'package_name': PACKAGE, 'retry_times': 2,
        })

    first.click.assert_called_once_with()
    assert find_elements.call_count == 2


def test_appium_swipe_search_checks_stop_before_each_swipe(tmp_path, monkeypatch):
    stopped = [False]

    def guard():
        if stopped[0]:
            raise RuntimeError('Execution has stopped')

    runner = make_appium_runner(tmp_path, guard)
    monkeypatch.setattr(runner, '_resolve_selector', Mock(return_value='target'))
    monkeypatch.setattr(runner, '_resolve_action_target', Mock(return_value=None))
    monkeypatch.setattr(
        flow_module, 'G', SimpleNamespace(
            DEVICE=SimpleNamespace(get_current_resolution=lambda: (100, 200)),
        ),
    )
    swipes = Mock(side_effect=lambda *_args, **_kwargs: stopped.__setitem__(0, True))
    monkeypatch.setattr(flow_module, 'swipe', swipes)

    with pytest.raises(RuntimeError, match='Execution has stopped'):
        runner.run([{
            'type': 'swipe_to', 'target_selector_type': 'pos', 'target_selector': '1,2',
            'max_swipes': 2, 'interval': 0,
        }], runtime={'stop_on_error': True})

    swipes.assert_called_once()


def test_appium_visual_input_checks_stop_between_focus_and_typing(tmp_path, monkeypatch):
    stopped = [False]

    def guard():
        if stopped[0]:
            raise RuntimeError('Execution has stopped')

    runner = make_appium_runner(tmp_path, guard)
    touches = Mock(side_effect=lambda _point: stopped.__setitem__(0, True))
    text = Mock()
    monkeypatch.setattr(flow_module, 'touch', touches)
    monkeypatch.setattr(flow_module, 'airtest_text', text)
    monkeypatch.setattr(flow_module.time, 'sleep', Mock())
    monkeypatch.setattr('apps.core.variable_resolver.resolve_variables', lambda value: value)

    with pytest.raises(RuntimeError, match='Execution has stopped'):
        runner.run([{
            'type': 'input', 'selector_type': 'pos', 'selector': '10,20', 'value': 'typed',
        }], runtime={'stop_on_error': True})

    touches.assert_called_once_with((10, 20))
    text.assert_not_called()


def test_appium_clear_input_checks_stop_between_select_all_and_delete(tmp_path, monkeypatch):
    stopped = [False]

    def guard():
        if stopped[0]:
            raise RuntimeError('Execution has stopped')

    runner = make_appium_runner(tmp_path, guard)
    device = SimpleNamespace()
    runner._require_device = Mock(return_value=device)
    select_all = Mock(side_effect=lambda *_args, **_kwargs: stopped.__setitem__(0, True))
    send_key = Mock()
    monkeypatch.setattr(flow_module.device_actions, '_command', select_all)
    monkeypatch.setattr(flow_module.device_actions, 'send_key', send_key)

    with pytest.raises(RuntimeError, match='Execution has stopped'):
        runner._action_clear_input({})

    select_all.assert_called_once_with(
        device, ['input', 'keycombination', 'KEYCODE_CTRL_LEFT', 'KEYCODE_A'],
        timeout=5, mutation=True,
    )
    send_key.assert_not_called()


def test_appium_restart_checks_stop_before_relaunch(tmp_path):
    stopped = [False]

    def guard():
        if stopped[0]:
            raise RuntimeError('Execution has stopped')

    def terminate(_package):
        stopped[0] = True

    driver = SimpleNamespace(terminate_app=Mock(side_effect=terminate), activate_app=Mock())
    runner = UiFlowRunner(image_base_dir=str(tmp_path), appium_driver=driver, device_guard=guard)
    runner.runtime = {'package_name': PACKAGE}

    with pytest.raises(RuntimeError, match='Execution has stopped'):
        runner._appium_app_lifecycle({}, 'app_restart')

    driver.terminate_app.assert_called_once_with(PACKAGE)
    driver.activate_app.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize('action', ['assert_text', 'extract_text'])
def test_linked_same_project_region_runs_through_ocr_on_saved_coordinates(
    appium_ocr_scene, tmp_path, monkeypatch, action,
):
    scene = appium_ocr_scene
    region = AppElement.objects.create(
        project=scene.project,
        name=f'{action} linked region',
        element_type=constants.ElementType.REGION,
        config={'x1': 12, 'y1': 34, 'x2': 56, 'y2': 78},
        created_by=scene.owner,
    )
    selector = '12, 34, 56, 78'
    config = {
        'element_id': region.pk,
        'selector_type': 'region',
        'selector': selector,
        'timeout': 0,
    }
    if action == 'assert_text':
        config.update(expected='Ready', match_mode='exact')
    else:
        config.update(name='captured_text', scope='local')
    scene.case.ui_flow = [{'type': action, 'config': config}]
    scene.case.save(update_fields=['ui_flow'])

    report = check_appium_prerequisites(scene.case, user=scene.owner)
    assert report['ready'] is True, report['issues']
    prepared, _variables = prepare_scene_execution(scene.case, scene.execution)
    assert prepared[0]['config']['selector'] == selector

    monkeypatch.setattr(flow_module, 'OCR_AVAILABLE', True)
    runner = UiFlowRunner(image_base_dir=str(tmp_path), appium_driver=SimpleNamespace())
    runner._require_device = Mock(return_value=SimpleNamespace())
    ocr = SimpleNamespace(recognize_region_text=Mock(return_value='Ready'))
    runner._get_ocr_helper = Mock(return_value=ocr)

    result = runner.run(prepared, runtime={'stop_on_error': True})

    assert result['failed'] == 0
    ocr.recognize_region_text.assert_called_once()
    assert ocr.recognize_region_text.call_args.args[0] == (12, 34, 56, 78)
    if action == 'extract_text':
        assert runner.context['local']['captured_text'] == 'Ready'


@pytest.mark.django_db
@pytest.mark.parametrize('element_type', [
    constants.ElementType.IMAGE,
    constants.ElementType.POS,
    constants.ElementType.NATIVE,
    'payment',
])
def test_ocr_region_reference_rejects_inappropriate_element_types(
    appium_ocr_scene, element_type,
):
    scene = appium_ocr_scene
    config = {'x1': 1, 'y1': 2, 'x2': 30, 'y2': 40}
    if element_type == constants.ElementType.NATIVE:
        config = {'package_name': PACKAGE, 'strategy': 'id', 'value': f'{PACKAGE}:id/title'}
    elif element_type == constants.ElementType.POS:
        config = {'x': 1, 'y': 2}
    element = AppElement.objects.create(
        project=scene.project,
        name=f'invalid OCR link {element_type}',
        element_type=element_type,
        config=config,
        created_by=scene.owner,
    )
    scene.case.ui_flow = [{
        'type': 'assert_text',
        'config': {
            'element_id': element.pk,
            'selector_type': 'region',
            'selector': '1, 2, 30, 40',
            'expected': 'Ready',
        },
    }]
    scene.case.save(update_fields=['ui_flow'])

    report = check_appium_prerequisites(scene.case, user=scene.owner)

    assert report['ready'] is False
    assert any(issue['code'] in {
        'unsupported_element_reference', 'native_element_unavailable',
    } for issue in report['issues'])


@pytest.mark.django_db
def test_linked_region_reference_remains_project_scoped(appium_ocr_scene):
    scene = appium_ocr_scene
    other_project = AppProject.objects.create(
        name='foreign OCR region project', owner=scene.owner, appium_enabled=True,
    )
    region = AppElement.objects.create(
        project=other_project, name='foreign OCR region',
        element_type=constants.ElementType.REGION,
        config={'x1': 1, 'y1': 2, 'x2': 30, 'y2': 40}, created_by=scene.owner,
    )
    scene.case.ui_flow = [{
        'type': 'extract_text',
        'config': {
            'element_id': region.pk,
            'selector_type': 'region',
            'selector': '1, 2, 30, 40',
            'name': 'captured_text',
        },
    }]
    scene.case.save(update_fields=['ui_flow'])

    report = check_appium_prerequisites(scene.case, user=scene.owner)

    assert report['ready'] is False
    assert any(issue['code'] == 'native_element_unavailable' for issue in report['issues'])


@pytest.mark.django_db
def test_native_text_element_is_still_accepted_without_visual_region_type(
    appium_ocr_scene,
):
    scene = appium_ocr_scene
    native = AppElement.objects.create(
        project=scene.project, name='native text element',
        element_type=constants.ElementType.NATIVE,
        config={'package_name': PACKAGE, 'strategy': 'id', 'value': f'{PACKAGE}:id/title'},
        created_by=scene.owner,
    )
    scene.case.ui_flow = [{
        'type': 'assert_text',
        'config': {
            'element_id': native.pk,
            'selector_type': 'resource_id',
            'selector': f'{PACKAGE}:id/title',
            'expected': 'Ready',
        },
    }]
    scene.case.save(update_fields=['ui_flow'])

    report = check_appium_prerequisites(scene.case, user=scene.owner)
    prepared, _variables = prepare_scene_execution(scene.case, scene.execution)

    assert report['ready'] is True, report['issues']
    assert prepared[0]['resolved_appium_elements']['config.element_id']['element_id'] == native.pk
