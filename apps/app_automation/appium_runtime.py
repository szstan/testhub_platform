"""Bounded Appium client adapter for server-owned Android device sessions."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlsplit

import anyio
import httpx
from asgiref.sync import async_to_sync
from django.conf import settings

from .constants import NATIVE_ELEMENT_STRATEGIES


_PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
_ALLOWED_ENDPOINT_PATHS = {"", "/", "/wd/hub"}
_MAX_TREE_BYTES = 2 * 1024 * 1024
_MAX_TREE_NODES = 10_000
_MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
_MAX_HTTP_RESPONSE_BYTES = 24 * 1024 * 1024
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
    connection_type: object = None


def _load_client() -> AppiumClientBindings:
    """Import the optional client only when an Appium session is actually used."""
    try:
        from appium import webdriver
        from appium.options.android import UiAutomator2Options
        from appium.webdriver.client_config import AppiumClientConfig
        from appium.webdriver.common.appiumby import AppiumBy
        from appium.webdriver.appium_connection import AppiumConnection
    except ImportError:
        raise AppiumClientUnavailable(
            "Appium support is not installed on this worker"
        ) from None
    return AppiumClientBindings(
        webdriver, UiAutomator2Options, AppiumClientConfig, AppiumBy, AppiumConnection,
    )


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


def _bounded_connection(bindings, endpoint, client_config, *, deadline=None, cleanup_deadline=None):
    connection_type = getattr(bindings, 'connection_type', None)
    if not isinstance(connection_type, type):
        raise AppiumClientUnavailable('Appium bounded transport is unavailable')
    origin = urlsplit(validate_loopback_endpoint(endpoint))

    class DeadlineConnection(connection_type):
        def _request(self, method, url, body=None):
            target = urlsplit(url)
            prefix = origin.path.rstrip('/') + '/'
            if (
                (target.scheme.lower(), target.netloc.lower())
                != (origin.scheme.lower(), origin.netloc.lower())
                or not target.path.startswith(prefix)
                or target.fragment
            ):
                raise AppiumConfigurationError('Appium request target changed')
            relative_parts = target.path[len(prefix):].strip('/').split('/')
            closing_session = method == 'DELETE' and len(relative_parts) == 2 and relative_parts[0] == 'session'
            headers = self.get_remote_connection_headers(target, self._client_config.keep_alive)
            get_auth = getattr(self._client_config, 'get_auth_header', None)
            if callable(get_auth):
                headers.update(get_auth() or {})
            if method not in ('POST', 'PUT'):
                body = None

            async def request():
                now = time.monotonic()
                timeout = _command_timeout() if closing_session else float(client_config.timeout)
                # Cleanup has its own budget even after a case deadline expires.
                request_deadline = cleanup_deadline if closing_session else deadline
                if request_deadline is not None:
                    timeout = min(timeout, request_deadline - now)
                if timeout <= 0:
                    raise AppiumOutcomeUnknown('Appium operation deadline exceeded')
                # This cancels a pending headers/body read even if bytes keep
                # arriving. SDK/urllib3 socket timeouts alone only bound idle IO.
                with anyio.fail_after(timeout):
                    async with httpx.AsyncClient(
                        timeout=timeout, trust_env=False, follow_redirects=False,
                    ) as client:
                        async with client.stream(method, url, content=body, headers=headers) as response:
                            chunks = []
                            size = 0
                            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                                size += len(chunk)
                                if size > _MAX_HTTP_RESPONSE_BYTES:
                                    raise AppiumOutcomeUnknown('Appium response exceeds its size budget')
                                chunks.append(chunk)
                            return response.status_code, response.headers, b''.join(chunks)

            try:
                status, response_headers, raw = async_to_sync(request)()
                text = raw.decode('utf-8')
                if status == 401:
                    return {'status': status, 'value': 'Authorization Required'}
                if status >= 400:
                    # Preserve Selenium's error-handler input so a trusted
                    # InvalidSessionIdException still proves a closed session.
                    return {'status': status, 'value': text.strip()}
                if 300 <= status < 400:
                    raise AppiumOutcomeUnknown('Appium redirects are not permitted')
                if response_headers.get('Content-Type', '').startswith('image/png'):
                    return {'status': 0, 'value': text}
                try:
                    result = json.loads(text.strip())
                except ValueError:
                    return {'status': 0 if 200 <= status < 300 else 13, 'value': text.strip()}
                if not isinstance(result, dict):
                    raise AppiumOutcomeUnknown('Appium returned an invalid response')
                result.setdefault('value', None)
                return result
            except AppiumRuntimeError:
                raise
            except Exception:
                raise AppiumOutcomeUnknown('Appium request outcome could not be confirmed') from None

    return DeadlineConnection(client_config=client_config)


def _trust_driver_close_target(driver, endpoint, session_id):
    driver._testhub_appium_endpoint = validate_loopback_endpoint(endpoint)
    driver._testhub_appium_session_id = session_id
    return driver


def create_driver(device, package, *, system_port=None, auto_launch=True, deadline=None):
    """Create a new remote session; transport errors are conservatively uncertain."""
    endpoint = endpoint_for_device(device)
    capabilities = build_capabilities(device, package, system_port, auto_launch=auto_launch)
    bindings = _load_client()
    options = _options(bindings, capabilities)
    timeout = _new_session_timeout()
    if deadline is not None:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise AppiumInputError('Appium execution deadline is invalid')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppiumInputError('Appium execution deadline has expired')
        timeout = min(timeout, remaining)
    client_config = _client_config(bindings, endpoint, timeout)
    if deadline is not None:
        # Retrying a new-session POST can both exceed the case budget and create
        # a second remote session after an uncertain response.
        client_config.init_args_for_pool_manager = {
            'init_args_for_pool_manager': {'retries': False},
        }
    try:
        driver = bindings.webdriver.Remote(
            command_executor=_bounded_connection(bindings, endpoint, client_config, deadline=deadline),
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


def attach_driver(session, *, deadline=None):
    """Attach to a persisted session with an optional absolute cleanup deadline."""
    endpoint = validate_loopback_endpoint(session.appium_endpoint)
    session_id = session.remote_session_id
    if not isinstance(session_id, str) or not session_id or len(session_id) > 255:
        raise AppiumOutcomeUnknown("The Appium session identifier is unavailable")
    capabilities = build_capabilities(session.device, session.package, session.system_port)
    bindings = _load_client()
    options = _options(bindings, capabilities)
    timeout = _command_timeout()
    if deadline is not None:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise AppiumInputError('Appium cleanup deadline is invalid')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppiumOutcomeUnknown('Appium cleanup deadline has expired')
        timeout = min(timeout, remaining)
    client_config = _client_config(bindings, endpoint, timeout)
    existing_caps = dict(getattr(session, "capabilities", None) or capabilities)

    class AttachedDriver(bindings.webdriver.Remote):
        def start_session(self, _capabilities):
            self.session_id = session_id
            self.caps = existing_caps

    try:
        driver = AttachedDriver(
            command_executor=_bounded_connection(
                bindings, endpoint, client_config, deadline=deadline, cleanup_deadline=deadline,
            ),
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
