import asyncio
import socket

import pytest

from apps.core.network import (
    UnsafeOutboundURL,
    configure_selenium_network_guard,
    guard_playwright_route,
    validate_browser_request_url,
    validate_outbound_url,
    validate_redirect_url,
    resolve_outbound_addresses,
)


class _Route:
    def __init__(self, url):
        self.request = type('Request', (), {'url': url})()
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


def test_playwright_route_guard_aborts_private_request():
    route = _Route('http://127.0.0.1:8080/metadata')
    asyncio.run(guard_playwright_route(route))
    assert route.aborted is True
    assert route.continued is False


def test_playwright_route_guard_continues_public_request(monkeypatch):
    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port)),
        ],
    )
    route = _Route('https://public.example.test/app.js')
    asyncio.run(guard_playwright_route(route))
    assert route.aborted is False
    assert route.continued is True


def test_validate_outbound_url_rejects_private_targets_by_default():
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url('http://127.0.0.1:8000/health')


@pytest.mark.parametrize('url', [
    'http://10.0.0.1/',
    'http://169.254.169.254/latest/meta-data',
    'http://192.168.1.1/',
    'http://172.16.0.1/',
    'http://[::1]/',
    'http://[fc00::1]/',
    'http://224.0.0.1/',
])
def test_validate_outbound_url_rejects_non_global_literal_targets(url):
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url(url)


def test_validate_outbound_url_rejects_dns_rebinding_style_mixed_answers(monkeypatch):
    def fake_getaddrinfo(host, port, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port)),
        ]

    monkeypatch.setattr('apps.core.network.socket.getaddrinfo', fake_getaddrinfo)
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url('https://public.example.test/api')


def test_validate_outbound_url_allows_public_target(monkeypatch):
    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port)),
        ],
    )
    assert validate_outbound_url('https://public.example.test/api') == 'https://public.example.test/api'


def test_selenium_fetch_guard_blocks_private_redirect_before_browser_access(monkeypatch):
    from types import SimpleNamespace

    class Fetch:
        RequestPaused = SimpleNamespace(event_class='Fetch.requestPaused')

        @staticmethod
        def RequestPattern(**kwargs):
            return kwargs

        @staticmethod
        def enable(**kwargs):
            return ('enable', kwargs)

        @staticmethod
        def fail_request(request_id, error_reason):
            return ('fail', request_id, error_reason)

        @staticmethod
        def continue_request(request_id):
            return ('continue', request_id)

    class Connection:
        def add_callback(self, _event, callback):
            self.callback = callback

        def execute(self, command):
            self.commands.append(command)

        def __init__(self):
            self.commands = []

    connection = Connection()
    devtools = SimpleNamespace(
        fetch=Fetch,
        network=SimpleNamespace(ErrorReason=SimpleNamespace(BLOCKED_BY_CLIENT='blocked')),
    )

    class Driver:
        def execute_cdp_cmd(self, *_args):
            return None

        def start_devtools(self):
            return devtools, connection

    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.1.2.3', port)),
        ],
    )
    driver = Driver()
    driver._testhub_guarded_proxy = object()
    configure_selenium_network_guard(driver)
    connection.callback(SimpleNamespace(
        request=SimpleNamespace(url='https://internal.example.test/admin'),
        request_id='redirect-hop',
    ))
    assert connection.commands[-1] == ('fail', 'redirect-hop', 'blocked')


def test_selenium_fetch_guard_continues_a_public_redirect(monkeypatch):
    from types import SimpleNamespace

    class Fetch:
        RequestPaused = SimpleNamespace(event_class='Fetch.requestPaused')
        RequestPattern = lambda **kwargs: kwargs
        enable = lambda **kwargs: ('enable', kwargs)
        fail_request = lambda request_id, error_reason: ('fail', request_id, error_reason)
        continue_request = lambda request_id: ('continue', request_id)

    class Connection:
        def __init__(self):
            self.commands = []

        def add_callback(self, _event, callback):
            self.callback = callback

        def execute(self, command):
            self.commands.append(command)

    connection = Connection()
    devtools = SimpleNamespace(fetch=Fetch, network=SimpleNamespace())

    class Driver:
        def execute_cdp_cmd(self, *_args):
            return None

        def start_devtools(self):
            return devtools, connection

    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port)),
        ],
    )
    driver = Driver()
    driver._testhub_guarded_proxy = object()
    configure_selenium_network_guard(driver)
    connection.callback(SimpleNamespace(
        request=SimpleNamespace(url='https://public.example.test/redirected'),
        request_id='public-hop',
    ))
    assert connection.commands[-1] == ('continue', 'public-hop')


def test_validate_redirect_url_rejects_private_location():
    with pytest.raises(UnsafeOutboundURL):
        validate_redirect_url(
            'https://public.example.test/start',
            'http://127.0.0.1:8080/admin',
        )


def test_validate_browser_request_url_allows_internal_browser_urls_and_normalizes_websocket():
    assert validate_browser_request_url('about:blank') == 'about:blank'
    assert validate_browser_request_url('data:text/plain,ok') == 'data:text/plain,ok'
    assert validate_browser_request_url('blob:https://public.example.test/id', allow_private=True).startswith('blob:')
    assert validate_browser_request_url('ws://127.0.0.1/socket', allow_private=True) == 'http://127.0.0.1/socket'


def test_validate_outbound_url_rejects_credentials_and_invalid_ports():
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url('https://user:password@example.com/api')
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url('https://example.com:99999/api')


def test_validate_outbound_url_allows_private_targets_only_when_explicit():
    assert validate_outbound_url('http://127.0.0.1:8000/health', allow_private=True) == 'http://127.0.0.1:8000/health'


def test_scoped_cidr_allows_only_the_authorized_private_host_even_with_legacy_flag():
    assert validate_outbound_url(
        'http://10.20.30.40/health',
        allow_private=True,
        allowed_private_cidrs=['10.20.30.40/32'],
    ) == 'http://10.20.30.40/health'
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url(
            'http://10.20.30.41/health',
            allow_private=True,
            allowed_private_cidrs=['10.20.30.40/32'],
        )


def test_scoped_empty_cidr_list_blocks_private_destinations():
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url(
            'http://192.168.1.10/',
            allow_private=True,
            allowed_private_cidrs=[],
        )


def test_scoped_mode_still_allows_public_dns_answers(monkeypatch):
    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port)),
        ],
    )
    assert validate_outbound_url(
        'https://public.example.test/',
        allowed_private_cidrs=[],
    ) == 'https://public.example.test/'


@pytest.mark.parametrize('cidrs', [
    ['0.0.0.0/0'],
    ['192.168.0.0/15'],
    ['100.64.0.0/10'],
    ['10.1.2.3/8'],
    ['not-a-cidr'],
])
def test_scoped_mode_rejects_invalid_or_overbroad_authorization(cidrs):
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url(
            'http://93.184.216.34/',
            allowed_private_cidrs=cidrs,
        )


def test_browser_internal_url_still_rejects_invalid_scoped_policy():
    with pytest.raises(UnsafeOutboundURL):
        validate_browser_request_url(
            'about:blank',
            allowed_private_cidrs=['0.0.0.0/0'],
        )


@pytest.mark.parametrize('address', [
    '127.0.0.1',
    '169.254.169.254',
    '0.0.0.0',
    '224.0.0.1',
    '255.255.255.255',
    '::1',
    'fe80::1',
    'ff02::1',
    'fd00:ec2::23',
    'fd00:0ec2:0:0:0:0:0:23',
    'fd00:ec2::254',
    'fd20:ce::254',
    'fd20:00ce:0:0:0:0:0:254',
])
def test_scoped_mode_always_blocks_special_addresses(address):
    with pytest.raises(UnsafeOutboundURL):
        resolve_outbound_addresses(
            address,
            80,
            allow_private=True,
            allowed_private_cidrs=['10.0.0.0/8', 'fc00::/7'],
        )


@pytest.mark.parametrize('blocked_address', ['fd20:ce::254', 'fd00:ec2::23'])
def test_scoped_mode_always_blocks_cloud_service_dns_answer(monkeypatch, blocked_address):
    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', (blocked_address, port, 0, 0)),
        ],
    )
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url(
            'https://metadata.example.test/',
            allowed_private_cidrs=['fc00::/7'],
        )


@pytest.mark.parametrize('blocked_address', [
    'fd00:ec2::254',
    'fd20:ce::254',
    'fd00:ec2::23',
])
def test_scoped_mode_always_blocks_scoped_metadata_literals(blocked_address):
    with pytest.raises(UnsafeOutboundURL):
        resolve_outbound_addresses(
            f'{blocked_address}%1',
            80,
            allowed_private_cidrs=['fc00::/7'],
        )


@pytest.mark.parametrize('blocked_address', [
    'fd00:ec2::254',
    'fd20:ce::254',
    'fd00:ec2::23',
])
def test_scoped_mode_always_blocks_scoped_metadata_dns_answers(monkeypatch, blocked_address):
    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', (f'{blocked_address}%1', port, 0, 0)),
        ],
    )
    with pytest.raises(UnsafeOutboundURL):
        resolve_outbound_addresses(
            'metadata.example.test',
            80,
            allowed_private_cidrs=['fc00::/7'],
        )


@pytest.mark.parametrize('address', ['::1%1', 'fe80::1%1', '::%1', 'ff02::1%1', '100::1%1'])
def test_scoped_mode_still_blocks_scoped_ipv6_special_addresses(address):
    with pytest.raises(UnsafeOutboundURL):
        resolve_outbound_addresses(
            address,
            80,
            allowed_private_cidrs=['fc00::/7'],
        )


def test_scoped_cidr_authorizes_scoped_ipv6_ula_and_preserves_return_value():
    address = 'fd12:3456::10%1'
    assert resolve_outbound_addresses(
        address,
        80,
        allowed_private_cidrs=['fd12:3456::/48'],
    ) == (address,)


def test_scoped_mode_rejects_mixed_public_and_unauthorized_private_dns_answers(monkeypatch):
    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.10.0.8', port)),
        ],
    )
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url(
            'https://mixed.example.test/',
            allowed_private_cidrs=['10.10.0.9/32'],
        )


def test_scoped_mode_authorizes_ipv6_ula_subnet():
    assert validate_outbound_url(
        'http://[fd12:3456::10]/',
        allowed_private_cidrs=['fd12:3456::/48'],
    ) == 'http://[fd12:3456::10]/'


def test_scoped_playwright_route_continues_authorized_private_request():
    route = _Route('http://10.20.30.40/health')
    asyncio.run(guard_playwright_route(
        route,
        allow_private=True,
        allowed_private_cidrs=['10.20.30.40/32'],
    ))
    assert route.aborted is False
    assert route.continued is True


def test_scoped_playwright_route_aborts_unauthorized_private_request():
    route = _Route('http://10.20.30.41/health')
    asyncio.run(guard_playwright_route(
        route,
        allow_private=True,
        allowed_private_cidrs=['10.20.30.40/32'],
    ))
    assert route.aborted is True
    assert route.continued is False


def test_scoped_selenium_guard_allows_authorized_private_and_keeps_static_special_blocks(monkeypatch):
    from types import SimpleNamespace

    class Fetch:
        RequestPaused = SimpleNamespace(event_class='Fetch.requestPaused')
        RequestPattern = lambda **kwargs: kwargs
        enable = lambda **kwargs: ('enable', kwargs)
        fail_request = lambda request_id, error_reason: ('fail', request_id, error_reason)
        continue_request = lambda request_id: ('continue', request_id)

    class Connection:
        def __init__(self):
            self.commands = []

        def add_callback(self, _event, callback):
            self.callback = callback

        def execute(self, command):
            self.commands.append(command)

    connection = Connection()
    calls = []
    devtools = SimpleNamespace(fetch=Fetch, network=SimpleNamespace())

    class Driver:
        def execute_cdp_cmd(self, method, params):
            calls.append((method, params))

        def start_devtools(self):
            return devtools, connection

    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.1.2.3', port)),
        ],
    )
    driver = Driver()
    driver._testhub_guarded_proxy = SimpleNamespace(allowed_private_cidrs=['10.1.2.3/32'])
    configure_selenium_network_guard(
        driver,
        allow_private=True,
        allowed_private_cidrs=['10.1.2.3/32'],
    )
    connection.callback(SimpleNamespace(
        request=SimpleNamespace(url='https://internal.example.test/admin'),
        request_id='authorized',
    ))
    assert connection.commands[-1] == ('continue', 'authorized')
    blocked_urls = next(params['urls'] for method, params in calls if method == 'Network.setBlockedURLs')
    assert 'http://127.*/*' in blocked_urls
    assert 'http://169.254.*/*' in blocked_urls
    assert 'http://10.*/*' not in blocked_urls
    assert not any('172.' in pattern or '192.168.' in pattern for pattern in blocked_urls)


def test_legacy_allow_private_flag_still_allows_loopback_without_scoped_policy():
    assert resolve_outbound_addresses('127.0.0.1', 80, allow_private=True) == ('127.0.0.1',)
