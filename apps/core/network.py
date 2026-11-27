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


_BLOCKED_IPV6_METADATA_ADDRESSES = frozenset({
    ipaddress.ip_address('fd00:ec2::23'),  # AWS EKS Pod Identity credentials
    ipaddress.ip_address('fd00:ec2::254'),
    ipaddress.ip_address('fd20:ce::254'),
})


_PRIVATE_AUTHORIZATION_NETWORKS = (
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('fc00::/7'),
)


def _normalize_allowed_private_cidrs(allowed_private_cidrs):
    """Parse narrowly scoped RFC1918/ULA CIDRs or reject the whole policy."""
    if allowed_private_cidrs is None:
        return None
    if isinstance(allowed_private_cidrs, (str, bytes)):
        entries = (allowed_private_cidrs,)
    else:
        try:
            entries = tuple(allowed_private_cidrs)
        except TypeError as exc:
            raise UnsafeOutboundURL('允许的私有网段配置无效') from exc

    networks = []
    for entry in entries:
        try:
            if isinstance(entry, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                network = entry
            else:
                value = str(entry).strip()
                if '/' not in value:
                    raise ValueError
                network = ipaddress.ip_network(value, strict=True)
        except (TypeError, ValueError) as exc:
            raise UnsafeOutboundURL('允许的私有网段配置无效') from exc
        if not any(
            network.version == allowed.version and network.subnet_of(allowed)
            for allowed in _PRIVATE_AUTHORIZATION_NETWORKS
        ):
            raise UnsafeOutboundURL('只允许配置 RFC1918 或 IPv6 ULA 网段')
        networks.append(network)
    return tuple(networks)


def _validate_resolved_addresses(addresses, *, allow_private, allowed_private_cidrs):
    if allowed_private_cidrs is None:
        if not allow_private:
            for address in addresses:
                if _is_restricted_address(address):
                    raise UnsafeOutboundURL('目标地址属于受限网络')
        return

    networks = _normalize_allowed_private_cidrs(allowed_private_cidrs)
    for address in addresses:
        try:
            ip = ipaddress.ip_address(str(address).strip('[]'))
        except ValueError as exc:
            raise UnsafeOutboundURL('目标地址格式非法') from exc
        if isinstance(ip, ipaddress.IPv6Address):
            # Zone identifiers are interface metadata, not part of address policy.
            ip = ipaddress.IPv6Address(ip.packed)
        if (ip in _BLOCKED_IPV6_METADATA_ADDRESSES or ip.is_loopback or ip.is_link_local
                or ip.is_unspecified or ip.is_multicast or ip.is_reserved):
            raise UnsafeOutboundURL('目标地址属于始终受限网络')
        if _is_restricted_address(str(ip)) and not any(
            ip.version == network.version and ip in network for network in networks
        ):
            raise UnsafeOutboundURL('目标地址属于受限网络（不在允许的私有网段中）')


def resolve_outbound_addresses(
    hostname: str,
    port: int,
    *,
    allow_private: bool = False,
    allowed_private_cidrs=None,
) -> tuple[str, ...]:
    """Resolve numeric addresses and reject unsafe or unauthorized DNS answers.

    ``allowed_private_cidrs=None`` preserves the legacy ``allow_private`` policy.
    Supplying a collection enables scoped mode: public addresses remain allowed,
    while restricted addresses must be inside an authorized RFC1918/ULA network.
    """
    allowed_private_cidrs = _normalize_allowed_private_cidrs(allowed_private_cidrs)
    hostname = str(hostname or '').strip().strip('[]').rstrip('.')
    if not hostname:
        raise UnsafeOutboundURL('目标主机格式非法')

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = (str(literal),)
    else:
        try:
            infos = socket.getaddrinfo(
                hostname,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise UnsafeOutboundURL('目标主机无法解析') from exc

        addresses = tuple(dict.fromkeys(
            str(item[4][0]) for item in infos if item and item[4]
        ))
        if not addresses:
            raise UnsafeOutboundURL('目标主机无法解析')

    _validate_resolved_addresses(
        addresses,
        allow_private=allow_private,
        allowed_private_cidrs=allowed_private_cidrs,
    )
    return addresses


def validate_outbound_url(
    url: str,
    *,
    allow_private: bool = False,
    allowed_private_cidrs=None,
) -> str:
    """Validate and return an HTTP(S) URL safe for an outbound request.

    Validation intentionally happens against the rendered URL at the call site.
    Every resolved address must be globally routable unless private targets are
    explicitly enabled for a controlled test environment. Supplying
    ``allowed_private_cidrs`` instead enables scoped RFC1918/ULA authorization.
    Callers that follow redirects must validate each ``Location`` with
    :func:`validate_redirect_url`.
    """
    allowed_private_cidrs = _normalize_allowed_private_cidrs(allowed_private_cidrs)
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
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme.lower() == 'https' else 80
    except ValueError as exc:
        raise UnsafeOutboundURL('URL 端口无效') from exc
    if not 1 <= port <= 65535:
        raise UnsafeOutboundURL('URL 端口无效')

    if allowed_private_cidrs is not None or not allow_private:
        resolve_outbound_addresses(
            hostname,
            port,
            allow_private=allow_private,
            allowed_private_cidrs=allowed_private_cidrs,
        )

    return parsed.geturl()


def validate_redirect_url(
    base_url: str,
    location: str,
    *,
    allow_private: bool = False,
    allowed_private_cidrs=None,
) -> str:
    """Resolve and validate one HTTP redirect target."""
    if not location:
        raise UnsafeOutboundURL('重定向地址为空')
    location_raw = str(location)
    if any(ord(char) < 32 or ord(char) == 127 for char in location_raw):
        raise UnsafeOutboundURL('重定向地址不允许包含控制字符')
    try:
        target = urljoin(str(base_url or ''), location_raw.strip())
    except ValueError as exc:
        raise UnsafeOutboundURL('重定向地址格式非法') from exc
    return validate_outbound_url(
        target,
        allow_private=allow_private,
        allowed_private_cidrs=allowed_private_cidrs,
    )


def validate_browser_request_url(
    url: str,
    *,
    allow_private: bool = False,
    allowed_private_cidrs=None,
) -> str:
    """Validate a Playwright/Selenium network request URL.

    Browser-internal/data URLs do not create server-side outbound connections;
    WebSocket URLs are validated using their HTTP(S) equivalent.
    """
    raw_url = str(url or '')
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
        raise UnsafeOutboundURL('URL 不允许包含控制字符')
    raw_url = raw_url.strip()
    allowed_private_cidrs = _normalize_allowed_private_cidrs(allowed_private_cidrs)
    try:
        scheme = urlparse(raw_url).scheme.lower()
    except ValueError:
        return validate_outbound_url(
            raw_url,
            allow_private=allow_private,
            allowed_private_cidrs=allowed_private_cidrs,
        )
    if scheme in {'about', 'data', 'blob'}:
        return raw_url
    if scheme in {'ws', 'wss'}:
        prefix = 'http' if scheme == 'ws' else 'https'
        raw_url = f'{prefix}{raw_url[len(scheme):]}'
    return validate_outbound_url(
        raw_url,
        allow_private=allow_private,
        allowed_private_cidrs=allowed_private_cidrs,
    )


async def guard_playwright_route(
    route,
    *,
    allow_private: bool = False,
    allowed_private_cidrs=None,
):
    """Abort unsafe requests after preflight URL/DNS validation.

    This does not pin the browser's eventual socket connection. Use it together
    with a DNS-pinning proxy for connection-level protection.
    """
    try:
        validate_browser_request_url(
            route.request.url,
            allow_private=allow_private,
            allowed_private_cidrs=allowed_private_cidrs,
        )
    except UnsafeOutboundURL as exc:
        logger.warning('Blocked unsafe browser request: %s (%s)', route.request.url, exc)
        await route.abort()
        return
    await route.continue_()


def guard_playwright_route_sync(
    route,
    *,
    allow_private: bool = False,
    allowed_private_cidrs=None,
):
    """Synchronous preflight guard; it does not pin the browser's connection.

    Pair it with a DNS-pinning proxy for connection-level protection.
    """
    try:
        validate_browser_request_url(
            route.request.url,
            allow_private=allow_private,
            allowed_private_cidrs=allowed_private_cidrs,
        )
    except UnsafeOutboundURL as exc:
        logger.warning('Blocked unsafe browser request: %s (%s)', route.request.url, exc)
        route.abort()
        return
    route.continue_()


def configure_selenium_network_guard(
    driver,
    *,
    allow_private: bool = False,
    allowed_private_cidrs=None,
) -> None:
    """Require the DNS-pinned proxy for Selenium and add optional CDP filtering."""
    scoped_networks = _normalize_allowed_private_cidrs(allowed_private_cidrs)
    scoped_mode = allowed_private_cidrs is not None
    requested_policy = (
        ('scoped', scoped_networks)
        if scoped_mode
        else ('legacy-private' if allow_private else 'strict', None)
    )
    if getattr(driver, '_testhub_network_guard_configured', False):
        if scoped_mode:
            proxy = getattr(driver, '_testhub_guarded_proxy', None)
            if proxy is None:
                raise RuntimeError('Scoped Selenium network guard requires the guarded outbound proxy')
            proxy_networks = _normalize_allowed_private_cidrs(
                getattr(proxy, 'allowed_private_cidrs', None)
            )
            if proxy_networks != scoped_networks:
                raise RuntimeError('Selenium guard and proxy private-network policies do not match')
            if getattr(driver, '_testhub_network_guard_policy', None) != requested_policy:
                raise RuntimeError('Selenium network guard is already configured with a different policy')
        return
    if allow_private and not scoped_mode:
        driver._testhub_network_guard_configured = True
        driver._testhub_network_guard_policy = requested_policy
        return
    if getattr(driver, '_testhub_guarded_proxy', None) is None:
        raise RuntimeError('Selenium network guard requires the guarded outbound proxy')
    if scoped_mode:
        proxy_networks = _normalize_allowed_private_cidrs(
            getattr(driver._testhub_guarded_proxy, 'allowed_private_cidrs', None)
        )
        if proxy_networks != scoped_networks:
            raise RuntimeError('Selenium guard and proxy private-network policies do not match')

    execute_cdp_cmd = getattr(driver, 'execute_cdp_cmd', None)
    if callable(execute_cdp_cmd):
        blocked = [
            'http://localhost/*', 'https://localhost/*',
            'http://localhost.*/*', 'https://localhost.*/*',
            'http://127.*/*', 'https://127.*/*',
            'http://0.0.0.0/*', 'https://0.0.0.0/*',
            'http://[::]/*', 'https://[::]/*',
            'http://[::1]/*', 'https://[::1]/*',
            'http://169.254.*/*', 'https://169.254.*/*',
            'http://[fe80::*]/*', 'https://[fe80::*]/*',
        ]
        if not scoped_mode:
            blocked.extend([
                'http://10.*/*', 'https://10.*/*',
                'http://192.168.*/*', 'https://192.168.*/*',
            ])
            blocked.extend(
                pattern
                for prefix in ('http://172.', 'https://172.')
                for pattern in (f'{prefix}{octet}.*/*' for octet in range(16, 32))
            )
        try:
            execute_cdp_cmd('Network.enable', {})
            execute_cdp_cmd('Network.setBlockedURLs', {'urls': blocked})
        except Exception as exc:  # noqa: BLE001 - the guarded proxy remains authoritative
            logger.debug('Selenium URL-pattern deny list unavailable: %s', exc)

    start_devtools = getattr(driver, 'start_devtools', None)
    if callable(start_devtools):
        try:
            devtools, connection = start_devtools()
            fetch = devtools.fetch

            def validate_paused_request(event):
                try:
                    validate_browser_request_url(
                        event.request.url,
                        allow_private=allow_private,
                        allowed_private_cidrs=scoped_networks,
                    )
                except Exception as exc:  # noqa: BLE001 - fail closed on validation errors
                    logger.warning('Blocked unsafe Selenium request: %s (%s)', event.request.url, exc)
                    connection.execute(fetch.fail_request(
                        event.request_id,
                        devtools.network.ErrorReason.BLOCKED_BY_CLIENT,
                    ))
                    return
                connection.execute(fetch.continue_request(event.request_id))

            connection.add_callback(fetch.RequestPaused, validate_paused_request)
            connection.execute(fetch.enable(
                patterns=[fetch.RequestPattern(url_pattern='*')],
            ))
        except Exception as exc:  # noqa: BLE001 - pinned proxy protects actual connections
            logger.warning('Selenium Fetch interception unavailable; guarded proxy remains active: %s', exc)
    driver._testhub_network_guard_configured = True
    driver._testhub_network_guard_policy = requested_policy
