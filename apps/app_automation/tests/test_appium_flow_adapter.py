from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.app_automation import appium_locator, appium_runtime
from apps.app_automation.runners.ui_flow_runner import AppiumPartialInputError, UiFlowRunner


PACKAGE = 'org.example.fixture'
LOCATOR = {'element_id': 1, 'package_name': PACKAGE, 'strategy': 'id', 'value': f'{PACKAGE}:id/target'}


def prepare(step_type, **config):
    return {'type': step_type, 'config': {'element_id': 1, 'timeout': 0, **config},
            'resolved_appium_elements': {'config.element_id': LOCATOR}}


@pytest.fixture
def driver():
    element = Mock(text='Signed in')
    element.is_displayed.return_value = True
    element.is_enabled.return_value = True
    element.get_attribute.return_value = 'true'
    return SimpleNamespace(
        current_package=PACKAGE,
        find_elements=Mock(return_value=[element]),
        element=element,
        activate_app=Mock(),
        terminate_app=Mock(),
    )


def make_runner(tmp_path, driver):
    return UiFlowRunner(image_base_dir=str(tmp_path), appium_driver=driver,
                        load_visual_device=Mock(side_effect=AssertionError('visual I/O was requested')))


def test_appium_runs_native_elements_without_visual_or_coordinate_actions(tmp_path, driver):
    runner = make_runner(tmp_path, driver)
    result = runner.run([
        {'type': 'app_start'},
        prepare('click'),
        prepare('input', value='qa-fixture', clear_first=True),
        prepare('clear_input'),
        prepare('wait_element', state='visible'),
        prepare('assert_element', state='checked'),
        prepare('assert_text', expected='Signed in', match_mode='exact'),
        prepare('extract_text', name='screen_text'),
        {'type': 'app_stop'},
    ], runtime={'package_name': PACKAGE, 'stop_on_error': True})
    assert result['failed'] == 0
    assert result['assertions'] == 2
    driver.element.click.assert_called_once_with()
    driver.element.send_keys.assert_called_once_with('qa-fixture')
    assert driver.element.clear.call_count == 2
    driver.activate_app.assert_called_once_with(PACKAGE)
    driver.terminate_app.assert_called_once_with(PACKAGE)
    runner.load_visual_device.assert_not_called()


def test_scoped_lookup_distinguishes_zero_and_multiple_matches(driver):
    row = Mock()
    row.find_elements.return_value = [driver.element]
    driver.find_elements.return_value = [row, Mock()]
    scoped = {**LOCATOR, 'container': {'strategy': 'accessibility_id', 'value': 'target order row'}}
    ambiguous = appium_locator.query_locator(driver, scoped)
    assert ambiguous.ambiguous_container is True
    assert ambiguous.count == 2
    row.find_elements.assert_not_called()
    driver.find_elements.return_value = [row]
    assert appium_locator.query_locator(driver, scoped).require_unique() is driver.element
    driver.find_elements.return_value = []
    with pytest.raises(appium_locator.AppiumLocatorFailure) as failure:
        appium_locator.query_locator(driver, scoped).require_unique()
    assert failure.value.code == 'element_not_found'


def test_transport_unknown_after_click_never_retries(tmp_path, driver):
    runner = make_runner(tmp_path, driver)
    runner._quarantine_execution_device = Mock()
    driver.element.click.side_effect = OSError('sensitive remote response')
    with pytest.raises(appium_runtime.AppiumOutcomeUnknown, match='could not confirm'):
        runner.run([prepare('click', retry_times=2, retry_interval=0)])
    driver.element.click.assert_called_once_with()
    runner._quarantine_execution_device.assert_called_once_with()


def test_ambiguous_native_click_is_not_sent(tmp_path, driver):
    runner = make_runner(tmp_path, driver)
    driver.find_elements.return_value = [driver.element, Mock()]
    with pytest.raises(appium_locator.AppiumLocatorFailure) as failure:
        runner.run([prepare('click')], runtime={'stop_on_error': True})
    assert failure.value.code == 'element_ambiguous'
    assert failure.value.match_count == 2
    driver.element.click.assert_not_called()


def test_native_locator_cannot_fall_through_to_visual_handler(tmp_path, driver):
    runner = make_runner(tmp_path, driver)
    step = {
        'type': 'screenshot',
        'resolved_appium_elements': {'element_id': LOCATOR},
    }

    with pytest.raises(ValueError, match='explicit visual or coordinate target'):
        runner._dispatch_step(step, 'screenshot')

    runner.load_visual_device.assert_not_called()
    driver.element.click.assert_not_called()


def test_appium_inline_locator_uses_unique_appium_matches(tmp_path, driver):
    runner = make_runner(tmp_path, driver)
    step = {'type': 'click', 'config': {'selector_type': 'resource_id', 'selector': f'{PACKAGE}:id/target'}}
    runtime = {'package_name': PACKAGE, 'stop_on_error': True}
    driver.find_elements.return_value = [driver.element, Mock()]
    runner._attach_allure = Mock()
    with pytest.raises(appium_locator.AppiumLocatorFailure) as failure:
        runner.run([step], runtime=runtime)
    assert failure.value.code == 'element_ambiguous'
    driver.element.click.assert_not_called()
    runner.load_visual_device.assert_not_called()

    driver.find_elements.return_value = [driver.element]
    assert runner.run([step], runtime=runtime)['passed'] == 1
    driver.find_elements.assert_called_with('id', f'{PACKAGE}:id/target')
    driver.element.click.assert_called_once_with()
    runner.load_visual_device.assert_not_called()


def test_appium_inline_text_contains_handles_both_quote_types(driver):
    locator = {
        'strategy': 'text', 'value': 'QA\'s "entry"',
        'match': 'contains', 'package_name': PACKAGE,
    }
    result = appium_locator.query_locator(driver, locator)
    assert result.require_unique() is driver.element
    driver.find_elements.assert_called_once_with('xpath', """//*[contains(@text, concat('QA', "'", 's "entry"'))]""")


def test_appium_input_is_not_replayed_when_text_succeeded_before_enter_timeout(tmp_path, driver, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(appium_locator.time, 'monotonic', lambda: now[0])
    driver.element.send_keys.side_effect = lambda _value: now.__setitem__(0, 2.0)
    driver.press_keycode = Mock()
    runner = make_runner(tmp_path, driver)
    runner._attach_allure = Mock()

    with pytest.raises(AppiumPartialInputError, match='partially completed'):
        runner.run([prepare('input', value='qa-fixture', send_enter=True,
                            timeout=1, retry_times=1, retry_interval=0)], runtime={'stop_on_error': True})

    driver.element.send_keys.assert_called_once_with('qa-fixture')
    driver.press_keycode.assert_not_called()


def test_late_appium_match_is_rejected_before_status_or_mutation(tmp_path, driver, monkeypatch):
    runner = make_runner(tmp_path, driver)
    now = [0.0]
    monkeypatch.setattr(appium_locator.time, 'monotonic', lambda: now[0])

    def late_match(*_args):
        now[0] = 1.0
        return [driver.element]

    driver.find_elements.side_effect = late_match
    with pytest.raises(appium_locator.AppiumLocatorFailure) as failure:
        runner.run([prepare('click', timeout=1)], runtime={'stop_on_error': True})

    assert failure.value.code == 'element_not_found'
    driver.element.is_displayed.assert_not_called()
    driver.element.click.assert_not_called()


def test_late_appium_state_read_is_rejected_before_mutation(tmp_path, driver, monkeypatch):
    runner = make_runner(tmp_path, driver)
    now = [0.0]
    monkeypatch.setattr(appium_locator.time, 'monotonic', lambda: now[0])

    def late_displayed():
        now[0] = 1.0
        return True

    driver.element.is_displayed.side_effect = late_displayed
    with pytest.raises(appium_locator.AppiumLocatorFailure) as failure:
        runner.run([prepare('click', timeout=1)], runtime={'stop_on_error': True})

    assert failure.value.code == 'element_state_mismatch'
    driver.element.is_enabled.assert_not_called()
    driver.element.click.assert_not_called()


def test_expired_deadline_after_guard_blocks_final_mutation(tmp_path, driver, monkeypatch):
    now = [0.0]
    calls = [0]
    monkeypatch.setattr(appium_locator.time, 'monotonic', lambda: now[0])

    def guard():
        calls[0] += 1
        if calls[0] == 6:
            now[0] = 1.0

    runner = UiFlowRunner(
        image_base_dir=str(tmp_path), appium_driver=driver, device_guard=guard,
        load_visual_device=Mock(side_effect=AssertionError('visual I/O was requested')),
    )
    step = prepare('click', timeout=1)
    step.update(step['config'])
    with pytest.raises(appium_locator.AppiumLocatorFailure) as failure:
        runner._dispatch_step(step, 'click')

    assert failure.value.code == 'element_state_mismatch'
    assert calls[0] == 6
    driver.element.click.assert_not_called()


def test_zero_timeout_performs_one_bounded_probe(tmp_path, driver, monkeypatch):
    runner = make_runner(tmp_path, driver)
    now = [0.0]
    monkeypatch.setattr(appium_locator.time, 'monotonic', lambda: now[0])

    def bounded_match(*_args):
        now[0] += 0.25
        return [driver.element]

    driver.find_elements.side_effect = bounded_match
    runner.run([prepare('click', timeout=0)])

    driver.find_elements.assert_called_once_with('id', LOCATOR['value'])
    driver.element.click.assert_called_once_with()


def test_worker_creates_session_after_admission_and_closes_after_pytest_exit(tmp_path, monkeypatch):
    from apps.app_automation import appium_execution
    from apps.app_automation.executors import test_executor

    executor = test_executor.AppTestExecutor(base_path=str(tmp_path))
    executor._preflight_worker = Mock(return_value={'adb_path': str(tmp_path / 'adb')})
    monkeypatch.setattr(appium_execution, 'preflight_execution_session', Mock())
    executor._get_allure_results_dir = Mock(return_value=str(tmp_path / 'allure'))
    executor._parse_allure_results = Mock(return_value={'total': 1, 'passed': 1})
    executor._generate_allure_report = Mock(return_value='fixture/report')
    events = []

    def start(execution_id, device_id, package_name):
        assert (execution_id, device_id, package_name) == (12, 'fake-device', PACKAGE)
        events.append('start')
        return SimpleNamespace(id='fixture-session')

    def spawn(args, **kwargs):
        assert 'no:django' in args
        assert kwargs['env']['APP_APPIUM_EXECUTION_SESSION'] == 'fixture-session'
        events.append('spawn')
        return SimpleNamespace(wait=lambda **_kwargs: events.append('exit') or 0)

    def close(owned_session, *, child_exit_confirmed):
        assert (owned_session.id, child_exit_confirmed) == ('fixture-session', True)
        events.append('close')
        return True

    monkeypatch.setattr(appium_execution, 'start_execution_session', start)
    monkeypatch.setattr(appium_execution, 'finish_execution_session', close)
    monkeypatch.setattr(test_executor.subprocess, 'Popen', spawn)
    result = executor.run_tests(1, 'fake-device', PACKAGE, execution_id=12, engine='appium',
                                before_device_io=lambda: events.append('admit'))
    assert events == ['admit', 'start', 'spawn', 'exit', 'close']
    assert result['appium_session_closed'] is True
    assert result['success'] is True
