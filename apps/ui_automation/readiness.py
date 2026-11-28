"""Short-lived process-local cache for UI readiness probes only.

Worker execution preflight deliberately keeps calling the engines directly.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from threading import Event, RLock
from time import monotonic

logger = logging.getLogger(__name__)
READY_TTL_SECONDS = 15.0
UNAVAILABLE_TTL_SECONDS = 3.0
MAX_CACHE_ENTRIES = 32
PROBE_WAIT_SECONDS = 30.0

_clock = monotonic
_lock = RLock()
_cache = OrderedDict()
_inflight = {}


def clear_readiness_cache():
    """Clear completed entries; in-flight owners still release their waiters."""
    with _lock:
        _cache.clear()


def _probe_target(engine, browser):
    if engine == 'playwright':
        from .playwright_engine import PlaywrightTestEngine

        return PlaywrightTestEngine.normalize_browser_type(browser), PlaywrightTestEngine.check_execution_environment_sync
    if engine == 'selenium':
        from .selenium_engine import SeleniumTestEngine

        return browser, SeleniumTestEngine.check_execution_environment
    raise ValueError('Unsupported readiness engine')


def cached_environment_check(engine, browser, policy):
    """Reuse equivalent checks, isolate policy changes and collapse concurrent misses."""
    normalized, probe = _probe_target(engine, browser)
    policy_json = json.dumps(policy, sort_keys=True, default=str, separators=(',', ':'))
    signature = hashlib.sha256(policy_json.encode('utf-8')).hexdigest()
    # Including the callable avoids retaining a result across adapter changes
    # (and makes isolated tests independent without sharing mocked outcomes).
    key = (engine, normalized, signature, probe)
    while True:
        with _lock:
            cached = _cache.get(key)
            if cached is not None:
                expires_at, result = cached
                if expires_at > _clock():
                    _cache.move_to_end(key)
                    return result
                _cache.pop(key, None)
            waiting = _inflight.get(key)
            if waiting is None:
                waiting = Event()
                _inflight[key] = waiting
                break
        if not waiting.wait(PROBE_WAIT_SECONDS):
            return False, '浏览器环境检查仍在进行，请稍后重试。'

    try:
        try:
            ready, message = probe(normalized)
            result = bool(ready), message
        except Exception:
            logger.exception('UI readiness probe failed engine=%s browser=%s', engine, normalized)
            result = False, '浏览器执行环境暂时无法检查，请稍后重试。'
        ttl = READY_TTL_SECONDS if result[0] else UNAVAILABLE_TTL_SECONDS
        with _lock:
            _cache[key] = (_clock() + ttl, result)
            _cache.move_to_end(key)
            while len(_cache) > MAX_CACHE_ENTRIES:
                _cache.popitem(last=False)
        return result
    finally:
        with _lock:
            _inflight.pop(key, None)
            waiting.set()
