from types import SimpleNamespace

import pytest

from apps.app_automation import native_locator
from apps.app_automation.native_locator import (
    NativeLocatorError,
    NativeLocatorHierarchyError,
    NativeLocatorTimeout,
    NativeLocatorUnsupportedDevice,
    probe_native_element,
)


HIERARCHY_WITHOUT_TARGET = (
    '<hierarchy><node text="other" bounds="[0,0][10,10]" /></hierarchy>'
)


class FakeADB:
    def __init__(self, outcome):
        self.outcome = outcome
        self.commands = []
        self.timeouts = []

    def cmd(self, commands, timeout=None):
        self.commands.append(commands)
        self.timeouts.append(timeout)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_one_shot_probe_returns_absence_only_after_parsing_a_hierarchy():
    adb = FakeADB(HIERARCHY_WITHOUT_TARGET)

    result = probe_native_element(adb, 'text', 'target', timeout=0.4)

    assert result is None
    assert adb.commands == [['shell', 'uiautomator', 'dump', '/dev/tty']]
    assert adb.timeouts == [0.4]


@pytest.mark.parametrize(
    ('failure', 'error_type'),
    [
        (TimeoutError('private hierarchy timeout'), NativeLocatorTimeout),
        (RuntimeError('private device transport details'), NativeLocatorUnsupportedDevice),
    ],
)
def test_one_shot_probe_failures_are_sanitized_and_never_look_absent(failure, error_type):
    adb = FakeADB(failure)

    with pytest.raises(error_type) as error:
        probe_native_element(adb, 'text', 'target', timeout=0.4)

    assert len(adb.commands) == 1
    assert adb.timeouts == [0.4]
    assert 'private' not in str(error.value)


def test_one_shot_probe_rejects_a_matching_node_without_usable_bounds():
    adb = FakeADB('<hierarchy><node text="target" /></hierarchy>')

    with pytest.raises(NativeLocatorHierarchyError, match='invalid bounds'):
        probe_native_element(adb, 'text', 'target', timeout=0.4)

    assert len(adb.commands) == 1


def test_one_shot_probe_rejects_malformed_hierarchy_instead_of_returning_absence():
    adb = FakeADB('<hierarchy><node text="other"></hierarchy>')

    with pytest.raises(NativeLocatorHierarchyError):
        probe_native_element(adb, 'text', 'target', timeout=0.4)

    assert len(adb.commands) == 1


def make_runner(monkeypatch, adb):
    pytest.importorskip('airtest.core.api', reason='Airtest is required to import the runner')
    from apps.app_automation.runners import ui_flow_runner as flow_module
    from apps.app_automation.runners.ui_flow_runner import UiFlowRunner

    runner = UiFlowRunner.__new__(UiFlowRunner)
    runner.context = {'local': {}, 'global': {}, 'outputs': {}, 'steps': {}}
    runner.runtime = {}
    runner._ocr_helper = None
    monkeypatch.setattr(flow_module, 'G', SimpleNamespace(DEVICE=SimpleNamespace(adb=adb)))
    return flow_module, runner


@pytest.mark.parametrize('action_name', ['_action_wait_element', '_action_assert_element'])
def test_hidden_state_passes_when_fake_hierarchy_proves_target_absent(
    monkeypatch, action_name,
):
    adb = FakeADB(HIERARCHY_WITHOUT_TARGET)
    flow_module, runner = make_runner(monkeypatch, adb)
    step = {
        'selector_type': 'text', 'selector': 'target', 'state': 'hidden', 'timeout': 0,
    }

    getattr(runner, action_name)(step)

    assert adb.timeouts == [flow_module._MAX_NATIVE_STATE_PROBE_TIMEOUT]


@pytest.mark.parametrize('action_name', ['_action_wait_element', '_action_assert_element'])
@pytest.mark.parametrize(
    'failure',
    [TimeoutError('private hierarchy timeout'), RuntimeError('private transport details')],
)
def test_hidden_state_fails_closed_on_adb_errors(monkeypatch, action_name, failure):
    adb = FakeADB(failure)
    _flow_module, runner = make_runner(monkeypatch, adb)
    step = {
        'selector_type': 'text', 'selector': 'target', 'state': 'hidden', 'timeout': 0,
    }

    with pytest.raises(NativeLocatorError) as error:
        getattr(runner, action_name)(step)

    assert adb.timeouts and 0 < adb.timeouts[0] <= 2.0
    assert 'private' not in str(error.value)


def test_hidden_state_fails_closed_when_adb_returns_after_probe_deadline(monkeypatch):
    clock = {'now': 0.0}
    monkeypatch.setattr(native_locator.time, 'monotonic', lambda: clock['now'])

    class LateADB(FakeADB):
        def cmd(self, commands, timeout=None):
            self.commands.append(commands)
            self.timeouts.append(timeout)
            clock['now'] += timeout
            return self.outcome

    adb = LateADB(HIERARCHY_WITHOUT_TARGET)
    _flow_module, runner = make_runner(monkeypatch, adb)
    step = {
        'selector_type': 'text', 'selector': 'target', 'state': 'hidden', 'timeout': 0,
    }

    with pytest.raises(NativeLocatorTimeout):
        runner._action_assert_element(step)

    assert adb.timeouts == [2.0]


def test_visible_wait_respects_overall_deadline_with_repeated_valid_absence(
    monkeypatch,
):
    clock = {'now': 0.0}
    monkeypatch.setattr(native_locator.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(
        native_locator.time,
        'sleep',
        lambda delay: clock.__setitem__('now', clock['now'] + delay),
    )

    class SlowHierarchyADB(FakeADB):
        def cmd(self, commands, timeout=None):
            self.commands.append(commands)
            self.timeouts.append(timeout)
            clock['now'] += 0.03
            return self.outcome

    adb = SlowHierarchyADB(HIERARCHY_WITHOUT_TARGET)
    _flow_module, runner = make_runner(monkeypatch, adb)

    with pytest.raises(AssertionError, match='did not reach'):
        runner._action_wait_element({
            'selector_type': 'text', 'selector': 'target', 'state': 'visible',
            'timeout': 0.25, 'interval': 0.1,
        })

    assert clock['now'] <= 0.250001
    assert len(adb.commands) == 2
    assert adb.timeouts[0] == pytest.approx(0.25)
    assert adb.timeouts[1] == pytest.approx(0.12)
