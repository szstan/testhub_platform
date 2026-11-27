import socket
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.core import browser_proxy, network
from apps.core.network import UnsafeOutboundURL


class _Options:
    def __init__(self):
        self.arguments = []
        self.preferences = {}

    def add_argument(self, argument):
        self.arguments.append(argument)

    def set_preference(self, name, value):
        self.preferences[name] = value


def _answer(address, port):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, port))


def test_pinned_proxy_rejects_dns_rebinding_after_url_preflight(monkeypatch):
    answers = iter([
        [_answer('93.184.216.34', 80)],
        [_answer('127.0.0.1', 80)],
    ])
    monkeypatch.setattr(network.socket, 'getaddrinfo', lambda *_args, **_kwargs: next(answers))
    connect = Mock()
    monkeypatch.setattr(browser_proxy.socket, 'create_connection', connect)

    assert network.validate_outbound_url('http://rebinding.example.test') == 'http://rebinding.example.test'
    with pytest.raises(UnsafeOutboundURL, match='受限网络'):
        browser_proxy._open_pinned_connection('rebinding.example.test', 80)
    connect.assert_not_called()


def test_scoped_proxy_blocks_cloud_identity_endpoint_before_connect(monkeypatch):
    connect = Mock()
    monkeypatch.setattr(browser_proxy.socket, 'create_connection', connect)

    with pytest.raises(UnsafeOutboundURL, match='受限网络'):
        browser_proxy._open_pinned_connection(
            'fd00:ec2::23', 80, allowed_private_cidrs=['fc00::/7'],
        )
    connect.assert_not_called()


def test_scoped_proxy_rejects_dns_rebinding_outside_authorized_cidr(monkeypatch):
    answers = iter([
        [_answer('93.184.216.34', 80)],
        [_answer('10.1.2.4', 80)],
    ])
    monkeypatch.setattr(network.socket, 'getaddrinfo', lambda *_args, **_kwargs: next(answers))
    connect = Mock()
    monkeypatch.setattr(browser_proxy.socket, 'create_connection', connect)
    cidrs = ['10.1.2.3/32']

    assert network.validate_outbound_url(
        'http://rebinding.example.test',
        allowed_private_cidrs=cidrs,
    ) == 'http://rebinding.example.test'
    with pytest.raises(UnsafeOutboundURL, match='不在允许的私有网段'):
        browser_proxy._open_pinned_connection(
            'rebinding.example.test',
            80,
            allowed_private_cidrs=cidrs,
        )
    connect.assert_not_called()


def test_pinned_proxy_connects_to_numeric_validated_address(monkeypatch):
    monkeypatch.setattr(
        network.socket,
        'getaddrinfo',
        lambda _host, port, **_kwargs: [_answer('93.184.216.34', port)],
    )
    connection = Mock()
    connect = Mock(return_value=connection)
    monkeypatch.setattr(browser_proxy.socket, 'create_connection', connect)

    assert browser_proxy._open_pinned_connection('public.example.test', 443) is connection
    assert connect.call_args.args[0] == ('93.184.216.34', 443)
    connection.close()


def test_scoped_proxy_pins_authorized_private_socket_and_rejects_other_lan(monkeypatch):
    monkeypatch.setattr(
        network.socket,
        'getaddrinfo',
        lambda _host, port, **_kwargs: [_answer('10.1.2.3', port)],
    )
    connection = Mock()
    connect = Mock(return_value=connection)
    monkeypatch.setattr(browser_proxy.socket, 'create_connection', connect)

    assert browser_proxy._open_pinned_connection(
        'private.example.test',
        443,
        allowed_private_cidrs=['10.1.2.3/32'],
    ) is connection
    assert connect.call_args.args[0] == ('10.1.2.3', 443)
    connect.reset_mock()
    with pytest.raises(UnsafeOutboundURL):
        browser_proxy._open_pinned_connection(
            'private.example.test',
            443,
            allowed_private_cidrs=['10.1.2.4/32'],
        )
    connect.assert_not_called()
    connection.close()


def test_http_proxy_passes_connection_time_addresses_to_pinned_transport(monkeypatch):
    resolved = []
    requested = {}

    def resolve(host, port, *, allowed_private_cidrs=None):
        resolved.append((host, port, allowed_private_cidrs))
        return ('10.3.0.5',)

    def guarded_request(method, url, **kwargs):
        requested.update(method=method, url=url, **kwargs)
        raw = SimpleNamespace(headers={}, stream=lambda *_args, **_kwargs: [b'ok'])
        return SimpleNamespace(status_code=200, raw=raw, headers={}, close=Mock())

    monkeypatch.setattr(browser_proxy, 'resolve_outbound_addresses', resolve)
    monkeypatch.setattr(browser_proxy, 'guarded_request', guarded_request)

    handler = object.__new__(browser_proxy._ProxyHandler)
    handler.path = 'http://private.example.test/resource'
    handler.command = 'GET'
    handler.request_version = 'HTTP/1.1'
    handler.headers = Message()
    handler.headers['Host'] = 'private.example.test'
    handler.server = SimpleNamespace(allowed_private_cidrs=('10.3.0.5/32',))
    handler.wfile = BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.close_connection = False

    handler._forward_http()

    assert resolved == [('private.example.test', 80, ('10.3.0.5/32',))]
    assert requested['pinned_addresses'] == ('10.3.0.5',)
    assert requested['allow_private'] is True
    assert requested['url'] == 'http://private.example.test/resource'


def test_http_proxy_normalizes_idn_and_never_re_resolves_after_authorization(monkeypatch):
    from apps.core import http_transport

    class OriginHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.request_seen = True
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *_args):
            return

    origin = HTTPServer(('127.0.0.1', 0), OriginHandler)
    origin.request_seen = False
    origin_thread = Thread(target=origin.serve_forever, daemon=True)
    origin_thread.start()

    hostname = 'xn--tst-qla.example.test'
    dns_answers = iter([
        [_answer('10.1.2.3', 80)],
        [_answer('127.0.0.1', 80)],
    ])
    dns_queries = []
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host.lower().rstrip('.') == hostname:
            dns_queries.append(host)
            return next(dns_answers)
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(network.socket, 'getaddrinfo', resolve)
    connect_targets = []
    real_connect = http_transport.connection.create_connection

    def connect_to_origin(address, timeout=None, source_address=None, socket_options=None):
        connect_targets.append(address)
        return real_connect(
            ('127.0.0.1', origin.server_port),
            timeout,
            source_address,
            socket_options,
        )

    monkeypatch.setattr(http_transport.connection, 'create_connection', connect_to_origin)

    handler = object.__new__(browser_proxy._ProxyHandler)
    handler.path = 'http://täst.example.test/resource'
    handler.command = 'GET'
    handler.request_version = 'HTTP/1.1'
    handler.headers = Message()
    handler.headers['Host'] = hostname
    handler.server = SimpleNamespace(allowed_private_cidrs=['10.1.2.3/32'])
    handler.wfile = BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.close_connection = False

    try:
        handler._forward_http()
        assert origin.request_seen is True
        assert dns_queries == [hostname]
        assert connect_targets == [('10.1.2.3', 80)]
    finally:
        origin.shutdown()
        origin.server_close()
        origin_thread.join(timeout=2)


def test_connect_and_websocket_proxy_paths_pass_scoped_policy(monkeypatch):
    proxy = Mock()
    monkeypatch.setattr(browser_proxy, '_open_pinned_connection', proxy)
    monkeypatch.setattr(browser_proxy, '_relay', Mock())
    cidrs = ('10.3.0.5/32',)

    connect_handler = object.__new__(browser_proxy._ProxyHandler)
    connect_handler.path = 'private.example.test:443'
    connect_handler.server = SimpleNamespace(allowed_private_cidrs=cidrs)
    connect_handler.connection = Mock()
    connect_handler.send_response = Mock()
    connect_handler.end_headers = Mock()
    connect_handler.close_connection = False
    connect_handler.do_CONNECT()
    assert proxy.call_args.kwargs['allowed_private_cidrs'] == cidrs

    proxy.reset_mock()
    upgrade_handler = object.__new__(browser_proxy._ProxyHandler)
    upgrade_handler.path = 'ws://private.example.test/socket'
    upgrade_handler.command = 'GET'
    upgrade_handler.request_version = 'HTTP/1.1'
    upgrade_handler.headers = Message()
    upgrade_handler.headers['Host'] = 'private.example.test'
    upgrade_handler.headers['Upgrade'] = 'websocket'
    upgrade_handler.headers['Connection'] = 'Upgrade'
    upgrade_handler.server = SimpleNamespace(allowed_private_cidrs=cidrs)
    upgrade_handler.connection = Mock()
    upgrade_handler.close_connection = False
    upgrade_handler._forward_http()
    assert proxy.call_args.kwargs['allowed_private_cidrs'] == cidrs


def test_chromium_proxy_options_route_loopback_targets_through_guard():
    options = _Options()
    browser_proxy._configure_browser_proxy(options, 'chrome', 'http://127.0.0.1:43210')

    assert '--proxy-server=http://127.0.0.1:43210' in options.arguments
    assert '--proxy-bypass-list=<-loopback>' in options.arguments
    assert '--force-webrtc-ip-handling-policy=disable_non_proxied_udp' in options.arguments


def test_firefox_proxy_options_disable_direct_and_webrtc_egress():
    options = _Options()
    browser_proxy._configure_browser_proxy(options, 'firefox', 'http://127.0.0.1:43210')

    assert options.preferences['network.proxy.type'] == 1
    assert options.preferences['network.proxy.http_port'] == 43210
    assert options.preferences['network.proxy.ssl_port'] == 43210
    assert options.preferences['network.proxy.no_proxies_on'] == ''
    assert options.preferences['network.proxy.allow_hijacking_localhost'] is True
    assert options.preferences['media.peerconnection.enabled'] is False


def test_guarded_driver_closes_proxy_when_webdriver_quits():
    options = _Options()
    driver = Mock()
    original_quit = driver.quit

    def create_driver(*, options):
        assert any(arg.startswith('--proxy-server=') for arg in options.arguments)
        return driver

    guarded = browser_proxy.create_guarded_selenium_driver(
        create_driver, options, browser_type='chrome', allow_private=False,
    )
    proxy = guarded._testhub_guarded_proxy
    assert proxy.url.startswith('http://127.0.0.1:')
    guarded.quit()
    original_quit.assert_called_once_with()
    assert proxy._server is None


def test_guarded_driver_scoped_mode_enforces_proxy_with_allow_private_true():
    options = _Options()
    driver = SimpleNamespace(quit=Mock())

    def create_driver(*, options):
        assert any(arg.startswith('--proxy-server=') for arg in options.arguments)
        return driver

    guarded = browser_proxy.create_guarded_selenium_driver(
        create_driver,
        options,
        browser_type='chrome',
        allow_private=True,
        allowed_private_cidrs=['10.1.2.3/32'],
    )
    proxy = guarded._testhub_guarded_proxy
    assert tuple(map(str, proxy.allowed_private_cidrs)) == ('10.1.2.3/32',)
    guarded.quit()
    assert proxy._server is None


def test_legacy_allow_private_driver_still_disables_proxy():
    options = _Options()
    driver = SimpleNamespace(quit=Mock())

    guarded = browser_proxy.create_guarded_selenium_driver(
        lambda *, options: driver,
        options,
        browser_type='chrome',
        allow_private=True,
    )

    assert not any(arg.startswith('--proxy-server=') for arg in options.arguments)
    assert not hasattr(guarded, '_testhub_guarded_proxy')


def test_scoped_driver_configuration_rejects_invalid_cidr_before_driver_creation():
    called = False

    def create_driver(*, options):
        nonlocal called
        called = True
        return Mock()

    with pytest.raises(UnsafeOutboundURL):
        browser_proxy.create_guarded_selenium_driver(
            create_driver,
            _Options(),
            browser_type='chrome',
            allow_private=True,
            allowed_private_cidrs=['0.0.0.0/0'],
        )
    assert called is False


def test_strict_mode_rejects_safari_without_an_enforceable_proxy():
    with pytest.raises(RuntimeError, match='cannot enforce guarded browser egress'):
        browser_proxy._configure_browser_proxy(_Options(), 'safari', 'http://127.0.0.1:43210')
