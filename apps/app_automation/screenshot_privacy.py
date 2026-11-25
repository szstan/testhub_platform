"""Process-latched protection for screenshots after sensitive UI input."""
from __future__ import annotations

from functools import wraps
import threading

from airtest.core.api import ST
from airtest.core import api as airtest_api
from airtest.core import cv as airtest_cv


_lock = threading.RLock()
_persistence_disabled = False


class ScreenshotPrivacyError(RuntimeError):
    """Raised when screenshot persistence cannot be disabled reliably."""


def persistence_disabled() -> bool:
    with _lock:
        return _persistence_disabled


def _guard_log_screen(module) -> None:
    log_screen = getattr(module, 'try_log_screen', None)
    if not callable(log_screen) or getattr(log_screen, '_testhub_privacy_guard', False):
        return

    @wraps(log_screen)
    def guarded(*args, **kwargs):
        with _lock:
            if _persistence_disabled:
                ST.SAVE_IMAGE = False
                return None
            return log_screen(*args, **kwargs)

    guarded._testhub_privacy_guard = True
    module.try_log_screen = guarded


def _guard_device_snapshot(device) -> None:
    original = getattr(device, 'snapshot', None)
    if getattr(original, '_testhub_privacy_guard', False):
        return
    if not callable(original):
        return

    @wraps(original)
    def guarded(*args, **kwargs):
        with _lock:
            if _persistence_disabled:
                ST.SAVE_IMAGE = False
                if args:
                    args = (None, *args[1:])
                else:
                    kwargs.pop('filename', None)
            return original(*args, **kwargs)

    guarded._testhub_privacy_guard = True
    try:
        device.snapshot = guarded
    except Exception:
        raise ScreenshotPrivacyError(
            'Device screenshot persistence cannot be guarded'
        ) from None


def _enforce(device=None) -> None:
    ST.SAVE_IMAGE = False
    _guard_log_screen(airtest_api)
    _guard_log_screen(airtest_cv)
    if device is not None:
        _guard_device_snapshot(device)


def latch(device) -> None:
    """Permanently disable persisted screenshots for this worker process."""
    global _persistence_disabled
    with _lock:
        _persistence_disabled = True
        _enforce(device)


def enforce(device=None) -> None:
    """Reapply the latched policy after Airtest or runner reinitialization."""
    with _lock:
        if _persistence_disabled:
            _enforce(device)


def enforce_current_device() -> None:
    try:
        device = airtest_api.G.DEVICE
    except Exception:
        device = None
    enforce(device)


def capture_to_file(snapshot_function, filename):
    """Run an explicit persisted screenshot only while the worker is unprotected."""
    with _lock:
        if _persistence_disabled:
            _enforce()
            return False, None
        return True, snapshot_function(filename=filename)
