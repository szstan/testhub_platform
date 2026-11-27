"""Bounded Appium client adapter for server-owned Android device sessions."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlsplit

from django.conf import settings

from .constants import NATIVE_ELEMENT_STRATEGIES


_PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
_ALLOWED_ENDPOINT_PATHS = {"", "/", "/wd/hub"}
_MAX_TREE_BYTES = 2 * 1024 * 1024
_MAX_TREE_NODES = 10_000
_MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
_DEFAULT_SYSTEM_PORT_BASE = 8200
_DEFAULT_COMMAND_TIMEOUT = 15
_DEFAULT_NEW_SESSION_TIMEOUT = 45


class AppiumRuntimeError(RuntimeError):
    """Base error with a safe, stable code for API and worker responses."""

    code = "appium_unavailable"


class AppiumConfigurationError(AppiumRuntimeError):
    code = "appium_configuration_invalid"


class AppiumClientUnavailable(AppiumRuntimeError):
    code = "appium_client_unavailable"


class AppiumOutcomeUnknown(AppiumRuntimeError):
    code = "appium_outcome_unknown"


class AppiumInputError(AppiumRuntimeError, ValueError):
    code = "appium_input_invalid"


@dataclass(frozen=True)
class AppiumClientBindings:
    webdriver: object
    options_type: type
    client_config_type: type
    by: object


def _load_client() -> AppiumClientBindings:
    """Import the optional client only when an Appium session is actually used."""
    try:
        from appium import webdriver
        from appium.options.android import UiAutomator2Options
        from appium.webdriver.client_config import AppiumClientConfig
        from appium.webdriver.common.appiumby import AppiumBy
    except ImportError:
        raise AppiumClientUnavailable(
            "Appium support is not installed on this worker"
        ) from None
    return AppiumClientBindings(webdriver, UiAutomator2Options, AppiumClientConfig, AppiumBy)


def _configured_endpoints():
    configured = getattr(settings, "APP_APPIUM_ENDPOINTS", None)
    if configured is None:
        raw = getattr(settings, "APP_APPIUM_ENDPOINTS_JSON", "")
        if not isinstance(raw, str):
            raise AppiumConfigurationError("Appium endpoint configuration is invalid")
        if raw.strip():
            try:
                configured = json.loads(raw)
            except (TypeError, ValueError):
                raise AppiumConfigurationError("Appium endpoint configuration is invalid") from None
        else:
            configured = {}
    if not isinstance(configured, dict) or len(configured) > 2048:
        raise AppiumConfigurationError("Appium endpoint configuration is invalid")

    endpoints = {}
    for device_key, endpoint in configured.items():
        if (
            not isinstance(device_key, str)
            or not device_key
            or device_key.strip() != device_key
            or len(device_key) > 255
        ):
            raise AppiumConfigurationError("Appium endpoint configuration is invalid")
        endpoints[device_key] = validate_loopback_endpoint(endpoint)
    return endpoints


def validate_loopback_endpoint(value: object) -> str:
    """Accept only plain HTTP Appium URLs on a literal loopback IP."""
    if not isinstance(value, str) or not value or len(value) > 255:
        raise AppiumConfigurationError("Appium endpoint configuration is invalid")
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except (ValueError, TypeError):
        raise AppiumConfigurationError("Appium endpoint configuration is invalid") from None
    if (
        parsed.scheme != "http"
        or not address.is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in _ALLOWED_ENDPOINT_PATHS
        or port is None
        or not 1 <= port <= 65535
    ):
        raise AppiumConfigurationError("Appium endpoint must be a configured loopback HTTP URL")
    return value.rstrip("/")


def endpoint_for_device(device) -> str:
    """Resolve a configured endpoint by database id, then stable serial."""
    endpoints = _configured_endpoints()
    value = endpoints.get(str(device.pk))
    if value is None:
        value = endpoints.get(str(device.device_id))
    if value is None:
        raise AppiumConfigurationError("No Appium endpoint is configured for this device")
    return validate_loopback_endpoint(value)


def system_port_for_device(device) -> int:
    """Assign a deterministic port unique to each persisted device row."""
    base = getattr(settings, "APP_APPIUM_SYSTEM_PORT_BASE", _DEFAULT_SYSTEM_PORT_BASE)
    if type(base) is not int or base < 1024 or base > 65535:
        raise AppiumConfigurationError("Appium system port configuration is invalid")
    port = base + int(device.pk)
    if port > 65535:
        raise AppiumConfigurationError("Appium system port range is exhausted")
    return port


def _command_timeout():
    value = getattr(settings, "APP_APPIUM_COMMAND_TIMEOUT_SECONDS", _DEFAULT_COMMAND_TIMEOUT)
    if type(value) is not int or not 1 <= value <= 120:
        raise AppiumConfigurationError("Appium command timeout configuration is invalid")
    return value


def _new_session_timeout():
    value = getattr(settings, "APP_APPIUM_NEW_SESSION_TIMEOUT_SECONDS", _DEFAULT_NEW_SESSION_TIMEOUT)
    if type(value) is not int or not 1 <= value <= 180:
        raise AppiumConfigurationError("Appium session timeout configuration is invalid")
    return value


def _package_name(package):
    value = getattr(package, "package_name", package)
    if not isinstance(value, str) or len(value) > 255 or not _PACKAGE_RE.fullmatch(value):
        raise AppiumInputError("The selected Android package is invalid")
    return value


def build_capabilities(device, package, system_port=None, *, auto_launch=True):
    """Build capabilities from persisted device/package records, never request data."""
    port = system_port_for_device(device) if system_port is None else system_port
    if type(port) is not int or not 1024 <= port <= 65535:
        raise AppiumConfigurationError("Appium system port is invalid")
    serial = getattr(device, "device_id", None)
    if not isinstance(serial, str) or not serial or len(serial) > 255:
        raise AppiumInputError("The selected Android device is invalid")
    return {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:udid": serial,
        "appium:systemPort": port,
        "appium:appPackage": _package_name(package),
        "appium:noReset": True,
        "appium:autoLaunch": auto_launch,
        "appium:newCommandTimeout": 180,
    }


def _options(bindings, capabilities):
    options = bindings.options_type()
    options.load_capabilities(capabilities)
    return options


def _client_config(bindings, endpoint, timeout):
    return bindings.client_config_type(
        remote_server_addr=endpoint,
        timeout=timeout,
        direct_connection=False,
    )


def _trust_driver_close_target(driver, endpoint, session_id):
    driver._testhub_appium_endpoint = validate_loopback_endpoint(endpoint)
    driver._testhub_appium_session_id = session_id
    return driver


def create_driver(device, package, *, system_port=None, auto_launch=True):
    """Create a new remote session; transport errors are conservatively uncertain."""
    endpoint = endpoint_for_device(device)
    capabilities = build_capabilities(device, package, system_port, auto_launch=auto_launch)
    bindings = _load_client()
    options = _options(bindings, capabilities)
    client_config = _client_config(bindings, endpoint, _new_session_timeout())
    try:
        driver = bindings.webdriver.Remote(
            command_executor=endpoint,
            options=options,
            client_config=client_config,
        )
    except Exception:
        raise AppiumOutcomeUnknown("Appium session creation could not be confirmed") from None
    if not isinstance(getattr(driver, "session_id", None), str) or not driver.session_id:
        try:
            driver.quit()
        except Exception:
            raise AppiumOutcomeUnknown("Appium session creation could not be confirmed") from None
        raise AppiumOutcomeUnknown("Appium session creation returned no session identifier")
    return _trust_driver_close_target(driver, endpoint, driver.session_id)


def attach_driver(session):
    """Attach a fresh client to a persisted remote session without creating another."""
    endpoint = validate_loopback_endpoint(session.appium_endpoint)
    session_id = session.remote_session_id
    if not isinstance(session_id, str) or not session_id or len(session_id) > 255:
        raise AppiumOutcomeUnknown("The Appium session identifier is unavailable")
    capabilities = build_capabilities(session.device, session.package, session.system_port)
    bindings = _load_client()
    options = _options(bindings, capabilities)
    client_config = _client_config(bindings, endpoint, _command_timeout())
    existing_caps = dict(getattr(session, "capabilities", None) or capabilities)

    class AttachedDriver(bindings.webdriver.Remote):
        def start_session(self, _capabilities):
            self.session_id = session_id
            self.caps = existing_caps

    try:
        driver = AttachedDriver(
            command_executor=endpoint,
            options=options,
            client_config=client_config,
        )
    except Exception:
        raise AppiumOutcomeUnknown("The active Appium session could not be reached") from None
    return _trust_driver_close_target(driver, endpoint, session_id)


def _is_trusted_invalid_session_response(driver, error, *, expected_endpoint=None, expected_session_id=None):
    try:
        from selenium.common.exceptions import InvalidSessionIdException
    except ImportError:
        return False
    if not isinstance(error, InvalidSessionIdException):
        return False

    trusted_endpoint = getattr(driver, "_testhub_appium_endpoint", None)
    trusted_session_id = getattr(driver, "_testhub_appium_session_id", None)
    try:
        trusted_endpoint = validate_loopback_endpoint(trusted_endpoint)
        if expected_endpoint is not None:
            expected_endpoint = validate_loopback_endpoint(expected_endpoint)
    except AppiumConfigurationError:
        return False
    if (
        not isinstance(trusted_session_id, str) or not trusted_session_id
        or getattr(driver, "session_id", None) != trusted_session_id
        or (expected_endpoint is not None and expected_endpoint != trusted_endpoint)
        or (expected_session_id is not None and expected_session_id != trusted_session_id)
    ):
        return False
    return True


def quit_driver(driver, *, expected_endpoint=None, expected_session_id=None):
    """Delete a remote session; only a typed invalid-session response proves it is already absent."""
    try:
        driver.quit()
    except Exception as exc:
        if _is_trusted_invalid_session_response(
            driver, exc,
            expected_endpoint=expected_endpoint,
            expected_session_id=expected_session_id,
        ):
            return
        raise AppiumOutcomeUnknown("Appium session close could not be confirmed") from None


def locate_element(driver, strategy, value, *, container=None, package_name=None):
    """Perform one read-only locator query with the execution matching rules."""
    from .appium_locator import describe_locator

    locator = {"strategy": strategy, "value": value, "container": container}
    return describe_locator(driver, locator, package_name=package_name)


def click_element(driver, strategy, value, *, container=None, package_name=None):
    """Click a uniquely matched native element. Ambiguity cannot dispatch a click."""
    from .appium_locator import AppiumLocatorFailure, apply_native_mutation, wait_for_locator

    locator = {"strategy": strategy, "value": value, "container": container, "package_name": package_name}
    try:
        element = wait_for_locator(driver, locator, state="interactable", timeout=0, interval=0.1)
    except AppiumLocatorFailure:
        raise AppiumInputError("The Android target must resolve to one visible and enabled element") from None
    apply_native_mutation(element.click)
    return {"action": "click", "locator": {"strategy": strategy, "value": value}}


def screenshot(driver):
    """Return a size-bounded PNG data URI without logging image content."""
    try:
        encoded = driver.get_screenshot_as_base64()
        if not isinstance(encoded, str) or not encoded or len(encoded) > ((_MAX_SCREENSHOT_BYTES + 2) // 3) * 4:
            raise ValueError
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise AppiumRuntimeError("Appium returned invalid screenshot data") from None
    except Exception:
        raise AppiumOutcomeUnknown("Appium screenshot could not be confirmed") from None
    if len(image) > _MAX_SCREENSHOT_BYTES or not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AppiumRuntimeError("Appium returned invalid screenshot data")
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")


def _redact_node_attributes(node):
    attrs = dict(node.attrib)
    class_name = attrs.get("class", "").lower()
    resource_id = attrs.get("resource-id", "").lower()
    sensitive = (
        attrs.get("password", "false").lower() == "true"
        or any(part in resource_id for part in ("password", "passwd", "secret", "token"))
    )
    editable = "edittext" in class_name or "textinput" in class_name
    text = "" if sensitive or editable else attrs.get("text", "")
    description = "" if sensitive else attrs.get("content-desc", "")
    return {
        "class_name": attrs.get("class", "")[:255],
        "resource_id": attrs.get("resource-id", "")[:512],
        "text": text[:512],
        "content_desc": description[:512],
        "bounds": attrs.get("bounds", "")[:128],
        "clickable": attrs.get("clickable", "false").lower() == "true",
        "enabled": attrs.get("enabled", "true").lower() == "true",
        "displayed": attrs.get("displayed", attrs.get("visible-to-user", "true")).lower() == "true",
        "password": sensitive,
        "redact_pixels": sensitive or editable,
    }


def hierarchy(driver):
    """Return a bounded, privacy-filtered native hierarchy snapshot."""
    try:
        source = driver.page_source
        if not isinstance(source, str) or len(source.encode("utf-8")) > _MAX_TREE_BYTES:
            raise ValueError
        root = ET.fromstring(source)
    except Exception:
        raise AppiumRuntimeError("Appium returned an invalid or oversized UI hierarchy") from None

    nodes = []
    for index, node in enumerate(root.iter()):
        if index >= _MAX_TREE_NODES:
            raise AppiumRuntimeError("Appium UI hierarchy exceeded the node limit")
        nodes.append({"node_id": index, **_redact_node_attributes(node)})
    return {"nodes": nodes}


def inspect_screen(driver, *, package_name):
    """Match the hierarchy and screenshot under one operation and mask editable values."""
    try:
        current_package = driver.current_package
    except Exception:
        raise AppiumRuntimeError("Appium could not check the foreground app") from None
    if current_package != package_name:
        raise AppiumRuntimeError("The selected Android application is not in the foreground")
    result = hierarchy(driver)
    image = screenshot(driver)
    private = [node for node in result["nodes"] if node.pop("redact_pixels", False) and node["displayed"]]
    if private:
        from PIL import Image, ImageDraw

        data = base64.b64decode(image.split(",", 1)[1], validate=True)
        with Image.open(BytesIO(data)) as bitmap:
            if bitmap.width * bitmap.height > 20_000_000:
                raise AppiumRuntimeError("The Android screenshot is too large")
            canvas = bitmap.convert("RGB")
        draw = ImageDraw.Draw(canvas)
        for node in private:
            match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", node["bounds"])
            if match is None:
                return {**result, "screenshot": ""}
            x1, y1, x2, y2 = map(int, match.groups())
            if x2 > x1 and y2 > y1:
                draw.rectangle((x1, y1, x2, y2), fill="#202124")
        buffer = BytesIO()
        canvas.save(buffer, format="PNG")
        if buffer.tell() > _MAX_SCREENSHOT_BYTES:
            raise AppiumRuntimeError("The Android screenshot is too large")
        image = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    return {**result, "screenshot": image}


def validate_native_locator(strategy, value):
    """Validate an AppElement native locator using the persisted strategy contract."""
    if strategy not in NATIVE_ELEMENT_STRATEGIES:
        raise AppiumInputError("Unsupported Android locator strategy")
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise AppiumInputError("Android locator value is invalid")
    return strategy, value


__all__ = [
    "AppiumClientUnavailable",
    "AppiumConfigurationError",
    "AppiumInputError",
    "AppiumOutcomeUnknown",
    "AppiumRuntimeError",
    "attach_driver",
    "build_capabilities",
    "click_element",
    "create_driver",
    "endpoint_for_device",
    "hierarchy",
    "locate_element",
    "quit_driver",
    "screenshot",
    "system_port_for_device",
    "validate_loopback_endpoint",
    "validate_native_locator",
]
