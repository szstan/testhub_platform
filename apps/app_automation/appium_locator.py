"""Query Appium locators the same way for inspection and for execution."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .appium_runtime import AppiumInputError, AppiumOutcomeUnknown, AppiumRuntimeError


class AppiumLocatorReadError(AppiumRuntimeError):
    code = 'appium_read_failed'


class AppiumLocatorFailure(AssertionError):
    def __init__(self, code, message, match_count):
        super().__init__(message)
        self.code = code
        self.match_count = match_count


@dataclass(frozen=True)
class LocatorMatch:
    count: int
    element: Any = None
    ambiguous_container: bool = False

    def require_unique(self):
        if self.count == 0:
            raise AppiumLocatorFailure('element_not_found', 'Appium did not find the requested element', 0)
        if self.count != 1:
            raise AppiumLocatorFailure('element_ambiguous', 'Appium found multiple matches for this locator', self.count)
        return self.element


_BY = {'id': 'id', 'accessibility_id': 'accessibility id'}
_INLINE_ATTRIBUTES = {
    'resource_id': 'resource-id', 'content_desc': 'content-desc',
    'text': 'text', 'class': 'class',
}
INLINE_NATIVE_TYPES = tuple(_INLINE_ATTRIBUTES)
_INLINE_EXACT_BY = {'resource_id': 'id', 'content_desc': 'accessibility id', 'class': 'class name'}
_STATES = {'visible', 'hidden', 'enabled', 'disabled', 'checked', 'unchecked', 'selected', 'unselected'}


def _xpath_literal(value):
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = [f"'{part}'" for part in value.split("'")]
    joined = ', "\'", '.join(parts)
    return f'concat({joined})'


def _selector(locator):
    if not isinstance(locator, dict):
        raise AppiumInputError('An Android locator definition is required')
    strategy, value = locator.get('strategy'), locator.get('value')
    if not isinstance(strategy, str) or not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise AppiumInputError('The Android locator strategy or value is invalid')
    if strategy in _BY:
        return _BY[strategy], value
    mode = locator.get('match', 'exact')
    if strategy not in _INLINE_ATTRIBUTES or mode not in ('exact', 'contains'):
        raise AppiumInputError('The Android locator strategy or match mode is invalid')
    value = value.strip()
    if mode == 'exact' and strategy in _INLINE_EXACT_BY:
        return _INLINE_EXACT_BY[strategy], value
    attribute = _INLINE_ATTRIBUTES[strategy]
    literal = _xpath_literal(value)
    predicate = f'contains(@{attribute}, {literal})' if mode == 'contains' else f'@{attribute} = {literal}'
    return 'xpath', f'//*[{predicate}]'


def _read(callback):
    try:
        return callback()
    except Exception:
        raise AppiumLocatorReadError('The Appium session could not read the current screen') from None


def _ensure_before_deadline(deadline, *, code, message):
    if deadline is not None and time.monotonic() >= deadline:
        raise AppiumLocatorFailure(code, message, 0)


def _guarded_read(callback, *, deadline=None, guard=None, code='element_not_found'):
    message = 'The Appium locator lookup exceeded its deadline'
    _ensure_before_deadline(deadline, code=code, message=message)
    if guard is not None:
        guard()
    _ensure_before_deadline(deadline, code=code, message=message)
    value = _read(callback)
    _ensure_before_deadline(deadline, code=code, message=message)
    return value


def query_locator(driver, locator, *, package_name=None, deadline=None, guard=None):
    selector = _selector(locator)
    container = locator.get('container')
    package_name = package_name or locator.get('package_name')
    if package_name and _guarded_read(
        lambda: driver.current_package, deadline=deadline, guard=guard,
    ) != package_name:
        raise AppiumLocatorReadError('The expected Android application is not in the foreground')
    root = driver
    if container is not None:
        container_selector = _selector(container)
        matches = _guarded_read(
            lambda: root.find_elements(*container_selector), deadline=deadline, guard=guard,
        )
        if len(matches) != 1:
            return LocatorMatch(len(matches), ambiguous_container=len(matches) > 1)
        root = matches[0]
    matches = _guarded_read(
        lambda: root.find_elements(*selector), deadline=deadline, guard=guard,
    )
    _ensure_before_deadline(
        deadline, code='element_not_found', message='The Appium locator lookup exceeded its deadline',
    )
    return LocatorMatch(len(matches), matches[0] if len(matches) == 1 else None)


def describe_locator(driver, locator, *, package_name=None, deadline=None, guard=None):
    match = query_locator(
        driver, locator, package_name=package_name, deadline=deadline, guard=guard,
    )
    result = {'count': match.count, 'ambiguous_container': match.ambiguous_container}
    if match.element is not None:
        element = match.element
        result['bounds'] = _guarded_read(
            lambda: dict(element.rect), deadline=deadline, guard=guard, code='element_state_mismatch',
        )
        result['displayed'] = _guarded_read(
            element.is_displayed, deadline=deadline, guard=guard, code='element_state_mismatch',
        )
        result['enabled'] = _guarded_read(
            element.is_enabled, deadline=deadline, guard=guard, code='element_state_mismatch',
        )
    return result


def _flag(value):
    return value is True or isinstance(value, str) and value.lower() == 'true'


def _matches_state(el, state, *, deadline=None, guard=None):
    if state == 'interactable':
        displayed = _guarded_read(
            el.is_displayed, deadline=deadline, guard=guard, code='element_state_mismatch',
        )
        enabled = _guarded_read(
            el.is_enabled, deadline=deadline, guard=guard, code='element_state_mismatch',
        )
        return bool(displayed and enabled)
    if state in {'visible', 'hidden'}:
        return bool(_guarded_read(
            el.is_displayed, deadline=deadline, guard=guard, code='element_state_mismatch',
        )) == (state == 'visible')
    if state in {'enabled', 'disabled'}:
        return bool(_guarded_read(
            el.is_enabled, deadline=deadline, guard=guard, code='element_state_mismatch',
        )) == (state == 'enabled')
    attribute = 'checked' if state in {'checked', 'unchecked'} else 'selected'
    value = _guarded_read(
        lambda: el.get_attribute(attribute), deadline=deadline, guard=guard,
        code='element_state_mismatch',
    )
    if value is None:
        return False
    return _flag(value) == (state == attribute)


def wait_for_locator(driver, locator, *, state, timeout, interval, guard=None, deadline=None):
    if state not in _STATES and state != 'interactable':
        raise AppiumInputError('Unsupported Android element state')
    if deadline is None and timeout > 0:
        deadline = time.monotonic() + timeout
    while True:
        _ensure_before_deadline(
            deadline, code='element_not_found',
            message='The Appium locator lookup exceeded its deadline',
        )
        match = query_locator(driver, locator, deadline=deadline, guard=guard)
        if match.count > 1:
            match.require_unique()
        if match.count == 0 and state == 'hidden':
            return None
        if match.element is not None and _matches_state(
            match.element, state, deadline=deadline, guard=guard,
        ):
            _ensure_before_deadline(
                deadline, code='element_state_mismatch',
                message='The Appium element did not reach the requested state before its deadline',
            )
            return match.element
        if deadline is None:
            match.require_unique()
            raise AppiumLocatorFailure(
                'element_state_mismatch', 'The Appium element did not reach the requested state', 1,
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            match.require_unique()
            raise AppiumLocatorFailure(
                'element_state_mismatch', 'The Appium element did not reach the requested state', 1,
            )
        time.sleep(min(interval, remaining))


def read_native_text(element, *, deadline=None, guard=None):
    value = _guarded_read(
        lambda: element.text, deadline=deadline, guard=guard, code='element_state_mismatch',
    )
    if value:
        return value
    return _guarded_read(
        lambda: element.get_attribute('content-desc'), deadline=deadline, guard=guard,
        code='element_state_mismatch',
    ) or ''


def read_native_center(element, *, deadline=None, guard=None):
    bounds = _guarded_read(lambda: element.rect, deadline=deadline, guard=guard, code='element_state_mismatch')
    if not isinstance(bounds, dict):
        raise AppiumLocatorReadError('Appium did not return Android element bounds')
    coordinates = [bounds.get(key) for key in ('x', 'y', 'width', 'height')]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in coordinates):
        raise AppiumLocatorReadError('Appium returned invalid Android element bounds')
    x, y, width, height = coordinates
    if width <= 0 or height <= 0:
        raise AppiumLocatorReadError('Appium returned an empty Android element')
    return int(x + width / 2), int(y + height / 2)


def apply_native_mutation(command, *, guard=None, deadline=None):
    _ensure_before_deadline(
        deadline, code='element_state_mismatch',
        message='The Appium element exceeded its deadline before mutation dispatch',
    )
    if guard is not None:
        guard()
    _ensure_before_deadline(
        deadline, code='element_state_mismatch',
        message='The Appium element exceeded its deadline before mutation dispatch',
    )
    try:
        return command()
    except Exception:
        raise AppiumOutcomeUnknown('Appium could not confirm the outcome of the device action') from None
