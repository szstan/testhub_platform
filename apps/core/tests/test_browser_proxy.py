import socket
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


def test_strict_mode_rejects_safari_without_an_enforceable_proxy():
    with pytest.raises(RuntimeError, match='cannot enforce guarded browser egress'):
        browser_proxy._configure_browser_proxy(_Options(), 'safari', 'http://127.0.0.1:43210')
