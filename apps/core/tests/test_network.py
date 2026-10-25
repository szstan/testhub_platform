import asyncio
import socket

import pytest

from apps.core.network import (
    UnsafeOutboundURL,
    guard_playwright_route,
    validate_browser_request_url,
    validate_outbound_url,
    validate_redirect_url,
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
