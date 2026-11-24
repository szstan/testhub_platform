import logging

import pytest

from apps.app_automation import native_locator
from apps.app_automation.native_locator import (
    NativeLocatorHierarchyError,
    NativeLocatorInputError,
    NativeLocatorUnsupportedDevice,
    find_native_element,
    probe_native_element_any,
)


class FakeADB:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.commands = []
        self.timeouts = []

    def cmd(self, commands, device=True, ensure_unicode=True, timeout=None):
        self.commands.append(commands)
        self.timeouts.append(timeout)
        if not self.outputs:
            return ""
        if len(self.outputs) > 1:
            return self.outputs.pop(0)
        return self.outputs[0]


HIERARCHY = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node text="Pay now" resource-id="app:id/pay" content-desc="Checkout" class="android.widget.Button"
        bounds="[10,20][110,80]" visible-to-user="true" enabled="true" checked="false" selected="1" />
</hierarchy>"""


def test_fixed_airtest_command_and_all_supported_strategies():
    for strategy, value in (
        ("resource_id", "app:id/pay"),
        ("text", "Pay now"),
        ("content_desc", "Checkout"),
        ("class", "android.widget.Button"),
    ):
        adb = FakeADB(HIERARCHY)
        result = find_native_element(adb, strategy, value, timeout=1)

        assert result.strategy == strategy
        assert result.matched_value == value
        assert result.bounds == (10, 20, 110, 80)
        assert result.center == (60, 50)
        assert adb.commands == [["shell", "uiautomator", "dump", "/dev/tty"]]
        assert adb.timeouts[0] == 1


def test_one_shot_any_probe_skips_inactive_nodes_and_uses_one_bounded_dump():
    hierarchy = '''<hierarchy>
      <node resource-id="app:id/permission" bounds="[0,0][10,10]" visible="false" />
      <node resource-id="app:id/permission" bounds="[10,20][30,40]" visible="true" enabled="true" />
    </hierarchy>'''
    adb = FakeADB(hierarchy)

    result = probe_native_element_any(
        adb, 'resource_id', ['app:id/permission', 'other:id/permission'],
        timeout=1.5, interval=0.1, active_only=True,
    )

    assert result is not None
    assert result.center == (20, 30)
    assert adb.commands == [["shell", "uiautomator", "dump", "/dev/tty"]]
    assert adb.timeouts == [1.5]


    for suffix in (
        "\nUI hierchary dumped to: /dev/tty",
        "\r\nUI hierchary dumped to: /dev/tty\r\n",
    ):
        result = find_native_element(
            FakeADB(HIERARCHY + suffix), "text", "Pay now", timeout=1
        )
        assert result is not None
        assert result.center == (60, 50)


def test_unknown_trailing_output_is_not_stripped():
    with pytest.raises(NativeLocatorHierarchyError, match="malformed"):
        find_native_element(
            FakeADB(HIERARCHY + "\nother status"), "text", "Pay now", timeout=1
        )


def test_unicode_and_contains_match_are_preserved():
    hierarchy = '<hierarchy><node text="你好，世界 café" bounds="[0,0][20,10]" /></hierarchy>'
    result = find_native_element(
        FakeADB(hierarchy), "text", "世界 café", timeout=1, match="contains"
    )

    assert result is not None
    assert result.matched_value == "你好，世界 café"
    assert result.center == (10, 5)


def test_match_repr_hides_query_and_matched_text():
    matched_text = "password find-me notforrepr"
    hierarchy = (
        '<hierarchy><node text="' + matched_text
        + '" bounds="[0,0][20,10]" /></hierarchy>'
    )
    result = find_native_element(
        FakeADB(hierarchy), "text", "find-me", timeout=1, match="contains"
    )

    assert result.query == "find-me"
    assert result.matched_value == matched_text
    assert "find-me" not in repr(result)
    assert matched_text not in repr(result)


def test_resource_id_match_returns_text_attributes_without_logging_or_repr_leaks(caplog):
    text = "private visible text"
    content_desc = "private accessibility description"
    resource_id = "app:id/private-node"
    class_name = "com.example.PrivateView"
    hierarchy = (
        '<hierarchy><node text="' + text + '" content-desc="' + content_desc
        + '" resource-id="' + resource_id + '" class="' + class_name
        + '" bounds="[2,4][22,14]" /></hierarchy>'
    )

    with caplog.at_level(logging.DEBUG):
        result = find_native_element(
            FakeADB(hierarchy), "resource_id", resource_id, timeout=1
        )

    assert result is not None
    assert result.text == text
    assert result.content_desc == content_desc
    assert result.resource_id == resource_id
    assert result.class_name == class_name
    representation = repr(result)
    for sensitive_value in (text, content_desc, resource_id, class_name):
        assert sensitive_value not in representation
        assert sensitive_value not in caplog.text


def test_boolean_attributes_are_normalized_and_missing_values_use_documented_defaults():
    hierarchy = """<hierarchy>
      <node text="disabled" bounds="[1,2][5,8]" visible-to-user="0"
            enabled="false" checked="true" selected="1" />
      <node text="defaults" bounds="[3,4][7,10]" />
    </hierarchy>"""

    disabled = find_native_element(FakeADB(hierarchy), "text", "disabled", timeout=1)
    defaults = find_native_element(FakeADB(hierarchy), "text", "defaults", timeout=1)

    assert (disabled.visible, disabled.enabled, disabled.checked, disabled.selected) == (
        False,
        False,
        True,
        True,
    )
    assert (defaults.visible, defaults.enabled, defaults.checked, defaults.selected) == (
        True,
        True,
        False,
        False,
    )


def test_nodes_without_valid_bounds_are_not_returned():
    hierarchy = """<hierarchy>
      <node text="target" />
      <node text="target" bounds="[8,3][2,9]" />
      <node text="target" bounds="[-1,0][5,5]" />
    </hierarchy>"""

    assert find_native_element(FakeADB(hierarchy), "text", "target", timeout=1) is None


def test_malformed_xml_raises_safe_hierarchy_error():
    with pytest.raises(NativeLocatorHierarchyError, match="malformed"):
        find_native_element(FakeADB("<hierarchy><node text='x'></hierarchy>"), "text", "x", timeout=1)


def test_dtd_and_entity_payload_is_rejected_without_echoing_payload(caplog):
    secret = "hierarchy-private-value"
    malicious = (
        '<!DOCTYPE hierarchy [<!ENTITY stolen "' + secret + '">]>'
        '<hierarchy><node text="&stolen;" bounds="[0,0][1,1]" /></hierarchy>'
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(NativeLocatorHierarchyError) as error:
            find_native_element(FakeADB(malicious), "text", secret, timeout=1)

    assert secret not in str(error.value)
    assert secret not in caplog.text


def test_malformed_boolean_attribute_is_rejected():
    hierarchy = '<hierarchy><node text="target" bounds="[0,0][1,1]" enabled="sometimes" /></hierarchy>'
    with pytest.raises(NativeLocatorHierarchyError, match="boolean"):
        find_native_element(FakeADB(hierarchy), "text", "target", timeout=1)


def test_timeout_polls_only_at_the_requested_interval(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(native_locator.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(native_locator.time, "sleep", lambda delay: clock.__setitem__("now", clock["now"] + delay))
    adb = FakeADB("<hierarchy><node text='other' bounds='[0,0][2,2]' /></hierarchy>")

    result = find_native_element(adb, "text", "target", timeout=1.0, interval=0.5)

    assert result is None
    assert len(adb.commands) == 2
    assert adb.timeouts == [1.0, 0.5]
    assert clock["now"] == 1.0


def test_adb_dump_timeout_is_bounded_and_returns_none(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(native_locator.time, "monotonic", lambda: clock["now"])

    class BlockingADB:
        def __init__(self):
            self.timeouts = []

        def cmd(self, _commands, timeout=None):
            self.timeouts.append(timeout)
            clock["now"] += timeout
            raise RuntimeError(
                f"Command ['adb'] timed out after {timeout} seconds: "
                "stdout['private hierarchy secret'], stderr['']"
            )

    adb = BlockingADB()
    result = find_native_element(adb, "text", "target", timeout=0.5, interval=0.1)

    assert result is None
    assert adb.timeouts == [0.5]


def test_timeout_zero_returns_none_without_unbounded_adb_call():
    adb = FakeADB("<hierarchy />")

    assert find_native_element(adb, "text", "target", timeout=0) is None
    assert adb.commands == []


def test_node_and_byte_limits_are_enforced(monkeypatch):
    monkeypatch.setattr(native_locator, "_MAX_HIERARCHY_NODES", 2)
    with pytest.raises(NativeLocatorHierarchyError, match="node limit"):
        find_native_element(
            FakeADB("<hierarchy><node /><node /></hierarchy>"), "text", "target", timeout=1
        )

    monkeypatch.setattr(native_locator, "_MAX_HIERARCHY_BYTES", 10)
    with pytest.raises(NativeLocatorHierarchyError, match="size limit"):
        find_native_element(FakeADB("<hierarchy />"), "text", "target", timeout=1)


@pytest.mark.parametrize(
    ("strategy", "value", "timeout", "interval", "match"),
    [
        ([], "x", 0, 0.5, "exact"),
        ("text", "x", 0, 0.5, []),
        ("xpath", "x", 0, 0.5, "exact"),
        ("text", "", 0, 0.5, "exact"),
        ("text", "x", -1, 0.5, "exact"),
        ("text", "x", 1, 0, "exact"),
        ("text", "x", 1, 0.5, "regex"),
        ("text", "x", 300, 0.01, "exact"),
    ],
)
def test_invalid_inputs_raise_safe_input_errors(strategy, value, timeout, interval, match):
    with pytest.raises(NativeLocatorInputError):
        find_native_element(FakeADB(HIERARCHY), strategy, value, timeout, interval, match)


def test_non_android_objects_and_adb_failures_raise_safe_device_errors():
    class SecretADBFailure:
        def cmd(self, _commands, timeout=None):
            raise RuntimeError("device password=private-value")

    with pytest.raises(NativeLocatorUnsupportedDevice, match="bounded ADB command"):
        find_native_element(object(), "text", "target", timeout=0)
    with pytest.raises(NativeLocatorUnsupportedDevice) as error:
        find_native_element(SecretADBFailure(), "text", "target", timeout=1)
    assert "private-value" not in str(error.value)
