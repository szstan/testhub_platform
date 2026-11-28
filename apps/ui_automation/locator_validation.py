"""Safe format and live validation for stored UI locators."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit, urlunsplit

from django.utils import timezone

from .network_policy import validate_ui_outbound_url


SUPPORTED_BROWSERS = {'chrome', 'edge', 'firefox'}


def _escape_css_identifier(value):
    value = str(value or '')
    escaped = []
    for index, character in enumerate(value):
        codepoint = ord(character)
        if codepoint == 0:
            escaped.append('\ufffd')
        elif (
            1 <= codepoint <= 0x1F
            or codepoint == 0x7F
            or ('0' <= character <= '9' and (index == 0 or (index == 1 and value[0] == '-')))
        ):
            escaped.append(f'\\{codepoint:x} ')
        elif index == 0 and character == '-' and len(value) == 1:
            escaped.append('\\-')
        elif codepoint >= 0x80 or character.isalnum() or character in {'-', '_'}:
            escaped.append(character)
        else:
            escaped.append(f'\\{character}')
    return ''.join(escaped)


def _escape_css_string(value):
    escaped = []
    for character in str(value or ''):
        codepoint = ord(character)
        if codepoint == 0:
            escaped.append('\ufffd')
        elif character in {'"', '\\'}:
            escaped.append(f'\\{character}')
        elif character == '\n':
            escaped.append('\\a ')
        elif character == '\r':
            escaped.append('\\d ')
        elif character == '\f':
            escaped.append('\\c ')
        elif codepoint < 0x20 or codepoint == 0x7F:
            escaped.append(f'\\{codepoint:x} ')
        else:
            escaped.append(character)
    return '"' + ''.join(escaped) + '"'


def _safe_target_context(url):
    parts = urlsplit(url)
    host = parts.hostname or ''
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f'{host}:{port}' if port is not None else host
    return urlunsplit((parts.scheme, netloc, '/', '', ''))


def _format_selector(strategy, value):
    selector = (value or '').strip()
    if not selector:
        return False, '定位器不能为空。'
    normalized = (strategy or '').strip().lower()
    try:
        if normalized in {'css', 'css selector'}:
            from cssselect2.parser import parse

            parsed = list(parse(selector))
            if not parsed:
                raise ValueError('empty selector')
        elif normalized == 'xpath':
            from lxml.etree import XPath

            XPath(selector)
        elif normalized in {'id', 'name', 'text'}:
            return True, '定位器格式有效；尚未在页面验证。'
        else:
            return None, '当前定位策略不支持格式检查。'
    except ImportError:
        return None, '当前服务未安装定位器格式检查依赖。'
    except Exception:
        return False, '定位器语法无效。'
    return True, '定位器格式有效；尚未在页面验证。'


def validate_locator(element, payload):
    checked_at = timezone.now()
    mode = str(payload.get('mode', 'live') or '').lower()
    engine = str(payload.get('engine', 'playwright') or '').lower()
    browser = str(payload.get('browser', 'chrome') or '').lower()
    if mode not in {'live', 'format'}:
        return _result(False, 'UNKNOWN', '验证模式不受支持。', mode, checked_at, engine, browser)
    expected_value = payload.get('expected_locator_value')
    expected_strategy = payload.get('expected_locator_strategy_id')
    if (
        (expected_value is not None and expected_value != element.locator_value)
        or (
            expected_strategy is not None
            and str(expected_strategy) != str(element.locator_strategy_id)
        )
    ):
        return _result(
            False, 'UNKNOWN', '定位器存在未保存的修改或已被更新，请保存并重新加载后再验证。',
            mode, checked_at, engine, browser,
        )
    if mode == 'format':
        valid, message = _format_selector(element.locator_strategy.name, element.locator_value)
        status = 'INVALID' if valid is False else 'UNKNOWN'
        return _result(
            False,
            status,
            message,
            mode,
            checked_at,
            engine,
            browser,
            format_valid=valid,
        )

    if engine != 'playwright' or browser not in SUPPORTED_BROWSERS:
        return _result(
            False, 'UNKNOWN', '当前引擎或浏览器不支持受限的实时定位检查。',
            mode, checked_at, engine, browser,
        )

    target_url = payload.get('target_url') or element.project.base_url
    try:
        guarded_url = validate_ui_outbound_url(target_url)
        safe_context = _safe_target_context(guarded_url)
    except Exception:
        return _result(
            False, 'UNKNOWN', '目标地址被UI网络策略拒绝或无法安全验证。',
            mode, checked_at, engine, browser,
            target_url=_safe_target_context(target_url) if _has_safe_url_parts(target_url) else '',
        )

    try:
        count, visible, enabled = _run_live_probe(guarded_url, browser, element)
    except InvalidLocatorSelector:
        return _result(
            False, 'INVALID', '定位器语法无效。',
            mode, checked_at, engine, browser,
            target_url=safe_context,
        )
    except Exception:
        return _result(
            False, 'UNKNOWN', '浏览器环境、页面导航或定位验证暂不可用。',
            mode, checked_at, engine, browser, target_url=safe_context,
        )

    if count == 0:
        status, valid, message = 'INVALID', False, '页面中没有找到匹配元素。'
    elif element.is_unique and count != 1:
        status, valid, message = 'INVALID', False, f'页面匹配到 {count} 个元素，当前元素要求唯一。'
    elif not visible or not enabled:
        status, valid, message = 'INVALID', False, '匹配元素当前不可见或不可用。'
    else:
        status, valid = 'VALID', True
        message = f'页面中找到 {count} 个可用匹配元素。'

    return _result(
        valid, status, message, mode, checked_at, engine, browser,
        target_url=safe_context, match_count=count,
    )


def _has_safe_url_parts(value):
    try:
        parts = urlsplit(str(value))
        return parts.scheme in {'http', 'https'} and bool(parts.hostname)
    except Exception:
        return False


class InvalidLocatorSelector(Exception):
    pass


def _is_invalid_locator_error(error):
    message = str(error).lower()
    return any(marker in message for marker in (
        'unexpected token', 'invalid selector', 'not a valid selector',
        'not a valid xpath', 'invalid xpath', 'failed to parse selector', 'syntaxerror',
    ))


def _run_live_probe(target_url, browser, element):
    from .playwright_engine import PlaywrightTestEngine

    async def probe():
        engine = PlaywrightTestEngine(browser_type=browser, headless=True)
        try:
            await engine.start()
            await engine.page.goto(target_url, wait_until='domcontentloaded', timeout=15000)
            strategy = element.locator_strategy.name.lower() if element.locator_strategy else 'css'
            value = element.locator_value
            try:
                if strategy in {'css', 'css selector'}:
                    locator = engine.page.locator(value)
                elif strategy == 'xpath':
                    locator = engine.page.locator(f'xpath={value}')
                elif strategy == 'id':
                    locator = engine.page.locator(f'#{_escape_css_identifier(value)}')
                elif strategy == 'name':
                    locator = engine.page.locator(f'[name={_escape_css_string(value)}]')
                elif strategy == 'text':
                    locator = engine.page.get_by_text(value, exact=False)
                else:
                    raise ValueError('unsupported locator strategy')
                count = await locator.count()
            except Exception as exc:
                if _is_invalid_locator_error(exc):
                    raise InvalidLocatorSelector from exc
                raise
            if count == 0:
                return 0, False, False
            first = locator.first
            return count, await first.is_visible(), await first.is_enabled()
        finally:
            await engine.stop()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(probe())
    finally:
        loop.close()


def _result(is_valid, status, message, mode, checked_at, engine, browser, **extra):
    result = {
        'is_valid': bool(is_valid),
        'validation_status': status,
        'validation_message': message,
        'validation_mode': mode,
        'engine': engine,
        'browser': browser,
        'checked_at': checked_at,
        'suggestions': [],
    }
    result.update(extra)
    return result
