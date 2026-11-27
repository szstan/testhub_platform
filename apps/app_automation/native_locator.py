"""Read and match Android UIAutomator hierarchy nodes through Airtest ADB.

Public API:
    find_native_element(adb, strategy, value, timeout=10.0, interval=0.5,
                        match="exact")

The adapter has no runner or device-installation side effects. It invokes only
``adb.cmd(["shell", "uiautomator", "dump", "/dev/tty"], timeout=remaining)``
and returns the first matching node with usable bounds. The Airtest command
receives the remaining overall deadline, so a blocked dump is interrupted too.
"""

from dataclasses import dataclass, field
import math
import re
import time
from typing import Optional, Tuple
from xml.parsers import expat


_HIERARCHY_DUMP_ARGS = ["shell", "uiautomator", "dump", "/dev/tty"]
_MAX_HIERARCHY_BYTES = 2 * 1024 * 1024
_MAX_HIERARCHY_NODES = 20_000
_MAX_POLL_ATTEMPTS = 1_000
_MAX_TIMEOUT_SECONDS = 300.0
_MAX_INTERVAL_SECONDS = 300.0
_MAX_COORDINATE = 1_000_000
_TRAILING_DUMP_STATUS = re.compile(
    rb"\r?\nUI hierchary dumped to: /dev/tty(?:\r?\n)?\Z"
)
_AIRTEST_TIMEOUT_MESSAGE = re.compile(
    r"^Command(?: .+)? timed out after [0-9]+(?:\.[0-9]+)? seconds: stdout\[.*\], stderr\[.*\]$",
    re.DOTALL,
)

Bounds = Tuple[int, int, int, int]
Point = Tuple[int, int]


class NativeLocatorError(Exception):
    """Base exception for safe native hierarchy lookup failures."""


class NativeLocatorInputError(NativeLocatorError, ValueError):
    """Raised when a lookup argument is unsupported or invalid."""


class NativeLocatorUnsupportedDevice(NativeLocatorError):
    """Raised when the supplied object cannot provide a bounded Android ADB command."""


class NativeLocatorTimeout(NativeLocatorError):
    """Raised when a bounded Android hierarchy probe times out."""


class NativeLocatorHierarchyError(NativeLocatorError):
    """Raised when the device hierarchy is missing, unsafe, or malformed."""


@dataclass(frozen=True)
class NativeElementMatch:
    """A matching hierarchy node with validated screen coordinates."""

    strategy: str
    query: str = field(repr=False)
    matched_value: str = field(repr=False)
    text: Optional[str] = field(repr=False)
    content_desc: Optional[str] = field(repr=False)
    resource_id: Optional[str] = field(repr=False)
    class_name: Optional[str] = field(repr=False)
    bounds: Bounds
    center: Point
    visible: bool
    enabled: bool
    checked: bool
    selected: bool


def find_native_element(
    adb,
    strategy: str,
    value: str,
    timeout: float = 10.0,
    interval: float = 0.5,
    match: str = "exact",
    *,
    active_only: bool = False,
    _one_shot: bool = False,
    _all_matches: bool = False,
) -> Optional[NativeElementMatch]:
    """Find an Android hierarchy node using a fixed, timed Airtest ADB call.

    ``strategy`` is one of ``resource_id``, ``text``, ``content_desc``, or
    ``class``. ``match`` is ``exact`` or ``contains``. The returned
    :class:`NativeElementMatch` uses ``(left, top, right, bottom)`` bounds and
    an integer ``(x, y)`` center. Nodes lacking valid bounds are ignored.

    Missing boolean attributes use Android view defaults: visible/enabled are
    true; checked/selected are false. A ``visible-to-user`` attribute takes
    precedence over ``visible`` when present. ``active_only`` skips hidden or
    disabled nodes. Each fixed ``adb.cmd`` invocation receives the remaining
    overall timeout.
    """
    _validate_lookup(strategy, value, timeout, interval, match)
    if _one_shot:
        if timeout <= 0:
            raise NativeLocatorInputError("timeout must be greater than zero for a hierarchy probe")
        return _probe_native_element(
            adb, strategy, value, timeout, match, all_matches=_all_matches,
        )
    return _find_native_element(adb, strategy, value, timeout, interval, match, active_only)


def probe_native_element(
    adb, strategy: str, value: str, timeout: float = 2.0, match: str = "exact",
) -> Optional[NativeElementMatch]:
    """Read one hierarchy and return None only when valid XML has no match.

    The single ADB invocation receives ``timeout``. Transport failures, timeouts,
    and malformed hierarchy output raise sanitized :class:`NativeLocatorError`
    subclasses instead of being treated as an absent element.
    """
    _validate_lookup(strategy, value, timeout, 0.5, match)
    if timeout <= 0:
        raise NativeLocatorInputError("timeout must be greater than zero for a hierarchy probe")
    return find_native_element(
        adb, strategy, value, timeout=timeout, interval=0.5, match=match, _one_shot=True,
    )


def probe_native_elements(
    adb, strategy: str, value: str, timeout: float = 2.0, match: str = "exact",
) -> Tuple[NativeElementMatch, ...]:
    """Read one hierarchy and return every matching node with validated bounds."""
    _validate_lookup(strategy, value, timeout, 0.5, match)
    if timeout <= 0:
        raise NativeLocatorInputError("timeout must be greater than zero for a hierarchy probe")
    result = find_native_element(
        adb, strategy, value, timeout=timeout, interval=0.5, match=match,
        _one_shot=True, _all_matches=True,
    )
    if result is None:
        return ()
    if isinstance(result, (list, tuple)):
        return tuple(result)
    return (result,)


def probe_native_element_any(
    adb, strategy: str, values, timeout: float = 2.0, interval: float = 0.5,
    match: str = "exact", *, active_only: bool = False,
) -> Optional[NativeElementMatch]:
    """Read one hierarchy and find the first active match among identifiers."""
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 32:
        raise NativeLocatorInputError("values must contain between 1 and 32 identifiers")
    if not isinstance(active_only, bool):
        raise NativeLocatorInputError("active_only must be a boolean")
    for value in values:
        _validate_lookup(strategy, value, timeout, interval, match)
    if timeout <= 0:
        raise NativeLocatorInputError("timeout must be greater than zero for a hierarchy probe")
    return _probe_native_element(
        adb, strategy, tuple(values), timeout, match, active_only=active_only,
    )


def _probe_native_element(
    adb, strategy, value, timeout, match, *, all_matches=False, active_only=False,
):
    try:
        command = getattr(adb, "cmd", None)
    except Exception:
        command = None
    if not callable(command):
        raise NativeLocatorUnsupportedDevice(
            "An Android device with an Airtest bounded ADB command is required"
        )

    timeout = float(timeout)
    deadline = time.monotonic() + timeout
    try:
        output = command(list(_HIERARCHY_DUMP_ARGS), timeout=timeout)
    except Exception as exc:
        if isinstance(exc, TimeoutError) or _is_airtest_timeout(exc):
            raise NativeLocatorTimeout("Reading the Android UI hierarchy timed out") from None
        raise NativeLocatorUnsupportedDevice(
            "Unable to read the Android UI hierarchy from this device"
        ) from None

    hierarchy = _parse_hierarchy(
        output, strategy, value, match, active_only=active_only,
        require_match_bounds=True, collect_all=all_matches,
    )
    if time.monotonic() >= deadline:
        raise NativeLocatorTimeout("Reading the Android UI hierarchy timed out")
    return hierarchy


def _is_airtest_timeout(error):
    if not isinstance(error, RuntimeError):
        return False
    try:
        return _AIRTEST_TIMEOUT_MESSAGE.fullmatch(str(error)) is not None
    except Exception:
        return False


def find_native_element_any(
    adb, strategy: str, values, timeout: float = 10.0, interval: float = 0.5,
    match: str = "exact", *, active_only: bool = False,
) -> Optional[NativeElementMatch]:
    """Find the first of several identifiers in each single hierarchy dump.

    All candidates share one deadline. When ``active_only`` is true, invisible
    or disabled matches are skipped, so they cannot mask a usable later node.
    """
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 32:
        raise NativeLocatorInputError("values must contain between 1 and 32 identifiers")
    if not isinstance(active_only, bool):
        raise NativeLocatorInputError("active_only must be a boolean")
    for value in values:
        _validate_lookup(strategy, value, timeout, interval, match)
    return _find_native_element(adb, strategy, tuple(values), timeout, interval, match, active_only)


def _find_native_element(adb, strategy, value, timeout, interval, match, active_only):
    try:
        command = getattr(adb, "cmd", None)
    except Exception:
        command = None
    if not callable(command):
        raise NativeLocatorUnsupportedDevice(
            "An Android device with an Airtest bounded ADB command is required"
        )

    timeout = float(timeout)
    interval = float(interval)
    poll_count = max(1, math.ceil(timeout / interval))
    if poll_count > _MAX_POLL_ATTEMPTS:
        raise NativeLocatorInputError(
            "timeout and interval allow too many hierarchy polls"
        )

    deadline = time.monotonic() + timeout
    for _attempt in range(poll_count):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            output = command(list(_HIERARCHY_DUMP_ARGS), timeout=remaining)
        except Exception as exc:
            if isinstance(exc, TimeoutError) or _is_airtest_timeout(exc):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                time.sleep(min(interval, remaining))
                continue
            raise NativeLocatorUnsupportedDevice(
                "Unable to read the Android UI hierarchy from this device"
            ) from None

        hierarchy = _parse_hierarchy(output, strategy, value, match, active_only=active_only)
        if hierarchy is not None:
            if time.monotonic() < deadline:
                return hierarchy
            return None

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    return None


def _validate_lookup(strategy, value, timeout, interval, match):
    if not isinstance(strategy, str) or strategy not in {
        "resource_id", "text", "content_desc", "class"
    }:
        raise NativeLocatorInputError("Unsupported native locator strategy")
    if not isinstance(value, str) or not value:
        raise NativeLocatorInputError("value must be a non-empty string")
    if not isinstance(match, str) or match not in {"exact", "contains"}:
        raise NativeLocatorInputError("match must be 'exact' or 'contains'")
    _validate_number(timeout, "timeout", minimum=0.0, maximum=_MAX_TIMEOUT_SECONDS)
    _validate_number(interval, "interval", minimum=0.000001, maximum=_MAX_INTERVAL_SECONDS)


def _validate_number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeLocatorInputError(f"{name} must be a finite number in range")
    if value < minimum or value > maximum or not math.isfinite(value):
        raise NativeLocatorInputError(f"{name} must be a finite number in range")


def _strip_trailing_dump_status(xml_bytes):
    status = _TRAILING_DUMP_STATUS.search(xml_bytes)
    if status is None:
        return xml_bytes
    document_prefix = xml_bytes[:status.start()]
    if document_prefix.rstrip().endswith(b"</hierarchy>"):
        return document_prefix.rstrip()
    return xml_bytes


def _parse_hierarchy(
    output, strategy, value, match, *, active_only=False, require_match_bounds=False,
    collect_all=False,
):
    xml_bytes = _strip_trailing_dump_status(_as_bounded_bytes(output))
    root_start = xml_bytes.find(b"<hierarchy")
    if root_start < 0:
        raise NativeLocatorHierarchyError("The device returned no Android hierarchy")

    # Airtest prefixes the XML with a short status line. Retain any XML
    # declaration or DTD before the hierarchy root so Expat validates it.
    starts = [root_start]
    declaration_start = xml_bytes.find(b"<?xml", 0, root_start)
    if declaration_start >= 0:
        starts.append(declaration_start)
    doctype = re.search(rb"<!\s*DOCTYPE\b", xml_bytes[:root_start], re.IGNORECASE)
    if doctype:
        starts.append(doctype.start())
    document = xml_bytes[min(starts):]

    attribute_name = {
        "resource_id": "resource-id",
        "text": "text",
        "content_desc": "content-desc",
        "class": "class",
    }[strategy]
    state = {
        "depth": 0, "nodes": 0, "result": None, "results": [], "root_seen": False,
        "matched_without_bounds": False,
    }
    parser = expat.ParserCreate()

    def reject_markup(*_args):
        raise NativeLocatorHierarchyError(
            "DTD and entity declarations are not allowed in Android hierarchies"
        )

    def on_start(name, attributes):
        if state["depth"] == 0:
            if name != "hierarchy":
                raise NativeLocatorHierarchyError("The device returned an invalid Android hierarchy")
            state["root_seen"] = True
        state["nodes"] += 1
        if state["nodes"] > _MAX_HIERARCHY_NODES:
            raise NativeLocatorHierarchyError("The Android hierarchy exceeds the node limit")

        if collect_all or state["result"] is None:
            candidate = attributes.get(attribute_name)
            if candidate is not None and _matches(candidate, value, match):
                bounds = _parse_bounds(attributes.get("bounds"))
                if bounds is None:
                    if require_match_bounds:
                        state["matched_without_bounds"] = True
                else:
                    visible = _boolean_attribute(attributes, ("visible-to-user", "visible"), True)
                    enabled = _boolean_attribute(attributes, ("enabled",), True)
                    checked = _boolean_attribute(attributes, ("checked",), False)
                    selected = _boolean_attribute(attributes, ("selected",), False)
                    if not active_only or (visible and enabled):
                        left, top, right, bottom = bounds
                        result = NativeElementMatch(
                            strategy=strategy,
                            query=candidate if isinstance(value, tuple) else value,
                            matched_value=candidate,
                            text=attributes.get("text"),
                            content_desc=attributes.get("content-desc"),
                            resource_id=attributes.get("resource-id"),
                            class_name=attributes.get("class"),
                            bounds=bounds,
                            center=((left + right) // 2, (top + bottom) // 2),
                            visible=visible,
                            enabled=enabled,
                            checked=checked,
                            selected=selected,
                        )
                        if collect_all:
                            state["results"].append(result)
                        else:
                            state["result"] = result
        state["depth"] += 1

    def on_end(_name):
        state["depth"] -= 1

    parser.StartElementHandler = on_start
    parser.EndElementHandler = on_end
    parser.StartDoctypeDeclHandler = reject_markup
    parser.EntityDeclHandler = reject_markup
    parser.UnparsedEntityDeclHandler = reject_markup
    parser.ExternalEntityRefHandler = reject_markup
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    try:
        parser.Parse(document, True)
    except NativeLocatorHierarchyError:
        raise
    except expat.ExpatError:
        raise NativeLocatorHierarchyError("The Android hierarchy XML is malformed") from None
    except Exception:
        raise NativeLocatorHierarchyError("The Android hierarchy XML is malformed") from None

    if not state["root_seen"] or state["depth"] != 0:
        raise NativeLocatorHierarchyError("The Android hierarchy XML is incomplete")
    if state["matched_without_bounds"] and (
        collect_all or state["result"] is None
    ):
        raise NativeLocatorHierarchyError("A matching Android element has invalid bounds")
    return state["results"] if collect_all else state["result"]


def _as_bounded_bytes(output):
    if isinstance(output, str):
        if len(output) > _MAX_HIERARCHY_BYTES:
            raise NativeLocatorHierarchyError("The Android hierarchy exceeds the size limit")
        try:
            output = output.encode("utf-8")
        except UnicodeEncodeError:
            raise NativeLocatorHierarchyError("The Android hierarchy is not valid UTF-8") from None
    elif not isinstance(output, bytes):
        raise NativeLocatorHierarchyError("The device returned an invalid Android hierarchy")

    if len(output) > _MAX_HIERARCHY_BYTES:
        raise NativeLocatorHierarchyError("The Android hierarchy exceeds the size limit")
    return output


def _matches(candidate, value, match):
    if isinstance(value, tuple):
        return any(_matches(candidate, item, match) for item in value)
    if match == "exact":
        return candidate == value
    return value in candidate


def _parse_bounds(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\[(\d{1,7}),(\d{1,7})\]\[(\d{1,7}),(\d{1,7})\]", value)
    if not match:
        return None
    left, top, right, bottom = (int(coordinate) for coordinate in match.groups())
    if max(left, top, right, bottom) > _MAX_COORDINATE:
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _boolean_attribute(attributes, names, default):
    for name in names:
        if name not in attributes:
            continue
        value = attributes[name].strip().lower()
        if value in {"true", "1"}:
            return True
        if value in {"false", "0"}:
            return False
        raise NativeLocatorHierarchyError("The Android hierarchy has an invalid boolean attribute")
    return default
