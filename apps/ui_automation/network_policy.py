"""Scoped outbound policy shared by UI automation browser engines."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from apps.core.browser_proxy import (
    GuardedBrowserProxy,
    create_guarded_selenium_driver as create_core_guarded_selenium_driver,
)
from apps.core.network import UnsafeOutboundURL, validate_outbound_url


def get_ui_network_policy():
    """Return a scoped policy; UI never inherits the performance-test exemption."""
    configured = getattr(settings, 'UI_ALLOWED_PRIVATE_CIDRS', ()) or ()
    if isinstance(configured, str):
        configured = tuple(item.strip() for item in configured.split(',') if item.strip())
    try:
        networks = GuardedBrowserProxy(allowed_private_cidrs=configured).allowed_private_cidrs
    except UnsafeOutboundURL as exc:
        raise ImproperlyConfigured(
            'UI 受限网络配置无效：UI_ALLOWED_PRIVATE_CIDRS 只能包含有效的 RFC1918 或 IPv6 ULA CIDR'
        ) from exc

    enabled = bool(getattr(settings, 'UI_ALLOW_PRIVATE_TARGETS', False))
    return {
        'allow_private': False,
        'allowed_private_cidrs': networks if enabled else (),
    }


def validate_ui_outbound_url(url):
    """Validate one UI target and make scoped-network denials actionable."""
    try:
        return validate_outbound_url(url, **get_ui_network_policy())
    except UnsafeOutboundURL as exc:
        message = str(exc)
        if '始终受限网络' in message:
            raise UnsafeOutboundURL(
                f'受限网络策略拒绝目标：{message}。回环、链路本地和云元数据地址不可授权。'
            ) from exc
        if '不在允许的私有网段' in message:
            enabled = bool(getattr(settings, 'UI_ALLOW_PRIVATE_TARGETS', False))
            guidance = (
                '请仅将确需访问的 RFC1918/IPv6 ULA 网段加入 UI_ALLOWED_PRIVATE_CIDRS。'
                if enabled else
                '需同时设置 UI_ALLOW_PRIVATE_TARGETS=True，并仅将确需访问的 RFC1918/IPv6 ULA 网段加入 UI_ALLOWED_PRIVATE_CIDRS。'
            )
            raise UnsafeOutboundURL(f'受限网络策略拒绝目标：{message}。{guidance}') from exc
        raise


def playwright_launch_network_options(browser_type, proxy_url):
    """Return browser settings that force supported Playwright engines through the proxy."""
    normalized = str(browser_type or '').lower()
    if normalized in {'chrome', 'chromium', 'edge'}:
        return {
            'proxy': {'server': proxy_url},
            'args': [
                '--proxy-bypass-list=<-loopback>',
                '--disable-quic',
                '--dns-prefetch-disable',
                '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
            ],
        }
    if normalized == 'firefox':
        return {
            'proxy': {'server': proxy_url},
            'firefox_user_prefs': {
                'network.proxy.allow_hijacking_localhost': True,
                'network.proxy.no_proxies_on': '',
                'network.dns.disablePrefetch': True,
                'network.http.http3.enable': False,
                'media.peerconnection.enabled': False,
                'dom.serviceWorkers.enabled': False,
            },
        }
    raise RuntimeError(
        f'受限网络策略无法保证 {normalized or "unknown"} 的代理和 WebRTC 防护，已拒绝启动浏览器'
    )


def configure_selenium_options(options, browser_type):
    """Apply pre-launch network restrictions not covered by the Selenium proxy."""
    normalized = str(browser_type or '').lower()
    if normalized == 'firefox':
        options.set_preference('dom.serviceWorkers.enabled', False)
        options.set_preference('network.dns.disablePrefetch', True)
        options.set_preference('network.http.http3.enable', False)
        options.set_preference('media.peerconnection.enabled', False)
        options.set_preference('network.proxy.allow_hijacking_localhost', True)
    elif normalized in {'chrome', 'edge'}:
        options.add_argument('--dns-prefetch-disable')
    else:
        raise RuntimeError(
            f'受限网络策略无法保证 {normalized or "unknown"} 的代理和 WebRTC 防护，已拒绝启动浏览器'
        )
    return options


def create_ui_guarded_selenium_driver(driver_factory, options=None, *, browser_type, **kwargs):
    """Create a Selenium browser with UI-only scoped proxy enforcement."""
    # UI policy is server-controlled; legacy caller flags must not widen it.
    kwargs.pop('allow_private', None)
    kwargs.pop('allowed_private_cidrs', None)
    configure_selenium_options(options, browser_type)
    driver = create_core_guarded_selenium_driver(
        driver_factory,
        options,
        browser_type=browser_type,
        **get_ui_network_policy(),
        **kwargs,
    )
    try:
        configure_selenium_network_guard(driver)
    except BaseException:
        try:
            driver.quit()
        except BaseException:
            pass
        raise
    return driver


def configure_selenium_network_guard(driver):
    """Require a matching scoped proxy and bypass service-worker interception."""
    from apps.core.network import configure_selenium_network_guard as configure_core_guard

    policy = get_ui_network_policy()
    configure_core_guard(driver, **policy)

    browser_name = str(getattr(driver, 'capabilities', {}).get('browserName', '')).lower()
    if browser_name in {'chrome', 'msedge', 'microsoftedge', 'edge'}:
        execute_cdp_cmd = getattr(driver, 'execute_cdp_cmd', None)
        if not callable(execute_cdp_cmd):
            raise RuntimeError('受限网络策略无法禁用 Chrome service worker，已拒绝启动浏览器')
        try:
            execute_cdp_cmd('Network.setBypassServiceWorker', {'bypass': True})
        except Exception as exc:
            raise RuntimeError('受限网络策略无法禁用 Chrome service worker，已拒绝启动浏览器') from exc
    elif browser_name == 'firefox':
        pass
    else:
        raise RuntimeError(
            f'受限网络策略无法确认 {browser_name or "unknown"} 的浏览器连接防护，已拒绝启动浏览器'
        )
