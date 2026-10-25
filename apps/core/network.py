"""Outbound URL validation for server-side test runners."""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


class UnsafeOutboundURL(ValueError):
    pass


def _is_restricted_address(address: str) -> bool:
    """Return whether an address is not safe for an outbound request."""
    try:
        ip = ipaddress.ip_address(str(address).strip('[]'))
    except ValueError as exc:
        raise UnsafeOutboundURL('目标地址格式非法') from exc
    # ``is_global`` excludes most non-routable ranges, but Python treats
    # multicast as global in some versions; keep every restricted class explicit.
    return (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _resolve_addresses(hostname: str, port: int):
    try:
        infos = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise UnsafeOutboundURL('目标主机无法解析') from exc

    addresses = {item[4][0] for item in infos if item and item[4]}
    if not addresses:
        raise UnsafeOutboundURL('目标主机无法解析')
    return addresses


def validate_outbound_url(url: str, *, allow_private: bool = False) -> str:
    """Validate and return an HTTP(S) URL safe for an outbound request.

    Validation intentionally happens against the rendered URL at the call site.
    Every resolved address must be globally routable unless private targets are
    explicitly enabled for a controlled test environment.  Callers that follow
    redirects must validate each ``Location`` with :func:`validate_redirect_url`.
    """
    raw_url = str(url or '')
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
        raise UnsafeOutboundURL('URL 不允许包含控制字符')
    raw_url = raw_url.strip()

    try:
        parsed = urlparse(raw_url)
    except ValueError as exc:
        raise UnsafeOutboundURL('目标 URL 格式非法') from exc
    try:
        hostname = parsed.hostname
    except ValueError as exc:
        raise UnsafeOutboundURL('目标主机格式非法') from exc
    if parsed.scheme.lower() not in {'http', 'https'} or not hostname:
        raise UnsafeOutboundURL('只允许 http/https URL')
    if parsed.username or parsed.password:
        raise UnsafeOutboundURL('URL 不允许包含认证信息')

    try:
        port = parsed.port or (443 if parsed.scheme.lower() == 'https' else 80)
    except ValueError as exc:
        raise UnsafeOutboundURL('URL 端口无效') from exc
    if not 1 <= port <= 65535:
        raise UnsafeOutboundURL('URL 端口无效')

    if not allow_private:
        hostname = hostname.rstrip('.')
        if not hostname:
            raise UnsafeOutboundURL('目标主机格式非法')
        # Check literals before DNS as well; getaddrinfo behaviour differs by OS
        # for unusual IPv4/IPv6 spellings.
        try:
            literal = ipaddress.ip_address(hostname.strip('[]'))
        except ValueError:
            literal = None
        if literal is not None:
            addresses = {str(literal)}
        else:
            addresses = _resolve_addresses(hostname, port)
        for address in addresses:
            if _is_restricted_address(address):
                raise UnsafeOutboundURL('目标地址属于受限网络')

    return parsed.geturl()


def validate_redirect_url(base_url: str, location: str, *, allow_private: bool = False) -> str:
    """Resolve and validate one HTTP redirect target.

    Redirects are an independent trust boundary: a public initial URL may point
    to loopback/private/link-local space, so callers must invoke this before the
    next request/navigation rather than relying on the initial validation.
    """
    if not location:
        raise UnsafeOutboundURL('重定向地址为空')
    location_raw = str(location)
    if any(ord(char) < 32 or ord(char) == 127 for char in location_raw):
        raise UnsafeOutboundURL('重定向地址不允许包含控制字符')
    target = urljoin(str(base_url or ''), location_raw.strip())
    return validate_outbound_url(target, allow_private=allow_private)


def validate_browser_request_url(url: str, *, allow_private: bool = False) -> str:
    """Validate a Playwright/Selenium network request URL.

    Browser-internal/data URLs do not create server-side outbound connections;
    WebSocket URLs are validated using their HTTP(S) equivalent.
    """
    raw_url = str(url or '')
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
        raise UnsafeOutboundURL('URL 不允许包含控制字符')
    raw_url = raw_url.strip()
    try:
        scheme = urlparse(raw_url).scheme.lower()
    except ValueError:
        return validate_outbound_url(raw_url, allow_private=allow_private)
    if scheme in {'about', 'data', 'blob'}:
        return raw_url
    if scheme in {'ws', 'wss'}:
        prefix = 'http' if scheme == 'ws' else 'https'
        raw_url = f'{prefix}{raw_url[len(scheme):]}'
    return validate_outbound_url(raw_url, allow_private=allow_private)


async def guard_playwright_route(route, *, allow_private: bool = False):
    """Abort unsafe Playwright requests, including redirect/subresource hops."""
    try:
        validate_browser_request_url(route.request.url, allow_private=allow_private)
    except UnsafeOutboundURL as exc:
        logger.warning('Blocked unsafe browser request: %s (%s)', route.request.url, exc)
        await route.abort()
        return
    await route.continue_()


def guard_playwright_route_sync(route, *, allow_private: bool = False):
    """Synchronous Playwright counterpart used by the legacy test executor."""
    try:
        validate_browser_request_url(route.request.url, allow_private=allow_private)
    except UnsafeOutboundURL as exc:
        logger.warning('Blocked unsafe browser request: %s (%s)', route.request.url, exc)
        route.abort()
        return
    route.continue_()


def configure_selenium_network_guard(driver) -> None:
    """Best-effort block obvious private redirect targets in Chromium CDP.

    Selenium does not expose a portable request interception API.  The initial
    URL is still validated synchronously by ``navigate``; this CDP deny list adds
    protection for common Chromium redirect forms before the browser fetches
    them. Firefox/Safari simply rely on the synchronous URL checks.
    """
    execute_cdp_cmd = getattr(driver, 'execute_cdp_cmd', None)
    if not callable(execute_cdp_cmd):
        return
    blocked = [
        'http://localhost/*', 'https://localhost/*',
        'http://localhost.*/*', 'https://localhost.*/*',
        'http://127.*/*', 'https://127.*/*',
        'http://0.0.0.0/*', 'https://0.0.0.0/*',
        'http://[::1]/*', 'https://[::1]/*',
        'http://10.*/*', 'https://10.*/*',
        'http://169.254.*/*', 'https://169.254.*/*',
        'http://192.168.*/*', 'https://192.168.*/*',
    ]
    blocked.extend(
        pattern
        for prefix in ('http://172.', 'https://172.')
        for pattern in (f'{prefix}{octet}.*/*' for octet in range(16, 32))
    )
    try:
        execute_cdp_cmd('Network.enable', {})
        execute_cdp_cmd('Network.setBlockedURLs', {'urls': blocked})
    except Exception as exc:  # noqa: BLE001 - browser/version dependent
        logger.debug('Selenium private-target deny list unavailable: %s', exc)
