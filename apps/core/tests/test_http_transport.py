import asyncio
import ipaddress
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import Mock

import anyio
import httpcore
import httpx
import pytest
import requests
import urllib3.connection as urllib3_connection

from apps.core import network
from apps.core.http_transport import (
    _GuardedAsyncBackend,
    _PinnedHTTPAdapter,
    _PinnedHTTPConnection,
    _PinnedHTTPSConnection,
    guarded_get,
    guarded_post,
    guarded_request,
    outbound_httpx_options,
)
from apps.core.network import UnsafeOutboundURL, resolve_outbound_addresses


def _answer(address, port):
    ip = ipaddress.ip_address(address)
    if ip.version == 4:
        return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (str(ip), port))
    return (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (str(ip), port, 0, 0))


class _FakeSocket:
    def selected_alpn_protocol(self):
        return None

    def close(self):
        pass


class _FakeAsyncStream:
    def __init__(self):
        self.tls = None

    async def start_tls(self, *, ssl_context, server_hostname, timeout):
        self.tls = {
            "ssl_context": ssl_context,
            "server_hostname": server_hostname,
            "timeout": timeout,
        }
        return self


class _FakeAsyncBackend:
    def __init__(self, stream=None, error=None):
        self.stream = stream or _FakeAsyncStream()
        self.error = error
        self.calls = []

    async def connect_tcp(self, host, port, **kwargs):
        self.calls.append((host, port, kwargs))
        if self.error:
            raise self.error
        return self.stream


def test_resolver_returns_numeric_addresses_and_rejects_all_if_one_is_restricted(monkeypatch):
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            _answer("93.184.216.34", port),
            _answer("2606:4700:4700::1111", port),
        ],
    )
    assert resolve_outbound_addresses("public.example.test", 443) == (
        "93.184.216.34",
        "2606:4700:4700::1111",
    )

    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            _answer("93.184.216.34", port),
            _answer("127.0.0.1", port),
        ],
    )
    with pytest.raises(UnsafeOutboundURL):
        resolve_outbound_addresses("public.example.test", 443)


@pytest.mark.parametrize(
    ("address", "version"),
    [
        ("93.184.216.34", 4),
        ("2606:4700:4700::1111", 6),
    ],
)
def test_requests_connection_uses_validated_numeric_address(monkeypatch, address, version):
    getaddrinfo = Mock(return_value=[_answer(address, 80)])
    connect = Mock(return_value=_FakeSocket())
    monkeypatch.setattr(network.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(urllib3_connection.connection, "create_connection", connect)

    connection = _PinnedHTTPConnection("public.example.test", 80, timeout=2)
    assert isinstance(connection._new_conn(), _FakeSocket)
    assert getaddrinfo.call_args.args[:2] == ("public.example.test", 80)
    assert connect.call_args.args[0] == (address, 80)
    assert connect.call_args.args[1] == 2
    assert ipaddress.ip_address(connect.call_args.args[0][0]).version == version


def test_requests_blocks_rebound_dns_before_socket_connect(monkeypatch):
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            _answer("93.184.216.34", port),
            _answer("127.0.0.1", port),
        ],
    )
    connect = Mock()
    monkeypatch.setattr(urllib3_connection.connection, "create_connection", connect)

    with pytest.raises(requests.ConnectionError):
        guarded_get("https://public.example.test/private")
    connect.assert_not_called()


def test_requests_preserves_tls_identity_and_verified_tls(monkeypatch):
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_answer("93.184.216.34", port)],
    )
    connect = Mock(return_value=_FakeSocket())
    monkeypatch.setattr(urllib3_connection.connection, "create_connection", connect)
    tls = {}

    def fake_wrap(**kwargs):
        tls.update(kwargs)
        return SimpleNamespace(socket=kwargs["sock"], is_verified=True)

    monkeypatch.setattr(urllib3_connection, "_ssl_wrap_socket_and_match_hostname", fake_wrap)
    connection = _PinnedHTTPSConnection("public.example.test", 443, timeout=2)
    connection.connect()
    connection.putrequest("GET", "/resource")

    assert connect.call_args.args[0] == ("93.184.216.34", 443)
    assert connection.host == "public.example.test"
    assert tls["server_hostname"] == "public.example.test"
    assert tls["cert_reqs"] == ssl.CERT_REQUIRED
    assert b"Host: public.example.test" in connection._buffer


def test_requests_connection_timeout_remains_a_connect_timeout(monkeypatch):
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_answer("93.184.216.34", port)],
    )
    monkeypatch.setattr(
        urllib3_connection.connection,
        "create_connection",
        Mock(side_effect=socket.timeout("connect timed out")),
    )

    with pytest.raises(urllib3_connection.ConnectTimeoutError):
        _PinnedHTTPConnection("public.example.test", 80, timeout=0.1)._new_conn()


def test_requests_connection_refusal_remains_a_connection_error(monkeypatch):
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_answer("93.184.216.34", port)],
    )
    monkeypatch.setattr(
        urllib3_connection.connection,
        "create_connection",
        Mock(side_effect=ConnectionRefusedError("connection refused")),
    )

    with pytest.raises(urllib3_connection.NewConnectionError):
        _PinnedHTTPConnection("public.example.test", 80, timeout=1)._new_conn()


def test_guarded_requests_redirects_stay_on_the_pinned_adapter(monkeypatch):
    calls = []

    def fake_send(adapter, prepared_request, **kwargs):
        calls.append((adapter, prepared_request.url))
        response = requests.Response()
        response.status_code = 302 if len(calls) == 1 else 200
        if len(calls) == 1:
            response.headers["Location"] = "https://second.example.test/target"
        response.url = prepared_request.url
        response.request = prepared_request
        response._content = b""
        return response

    monkeypatch.setattr(_PinnedHTTPAdapter, "send", fake_send)
    response = guarded_get("http://first.example.test/start")

    assert response.status_code == 200
    assert len(response.history) == 1
    assert [url for _, url in calls] == [
        "http://first.example.test/start",
        "https://second.example.test/target",
    ]
    assert calls[0][0] is calls[1][0]


def test_guarded_requests_disable_environment_proxies_and_keep_redirect_flag(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    observed = {}

    def fake_send(session, prepared_request, **kwargs):
        observed["trust_env"] = session.trust_env
        observed["proxies"] = kwargs["proxies"]
        observed["allow_redirects"] = kwargs["allow_redirects"]
        observed["https_pool"] = session.get_adapter(prepared_request.url).poolmanager.pool_classes_by_scheme["https"]
        return requests.Response()

    monkeypatch.setattr(requests.Session, "send", fake_send)
    guarded_get("https://public.example.test/data", allow_redirects=False)

    assert observed["trust_env"] is False
    assert observed["proxies"] == {}
    assert observed["allow_redirects"] is False
    assert observed["https_pool"].ConnectionCls is _PinnedHTTPSConnection


def test_guarded_requests_reject_explicit_proxies():
    with pytest.raises(ValueError, match="proxies"):
        guarded_request("GET", "https://public.example.test/", proxies={"https": "http://proxy.invalid"})


def test_allow_private_requests_use_ordinary_requests_functions(monkeypatch):
    request = Mock(return_value="request")
    get = Mock(return_value="get")
    post = Mock(return_value="post")
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)

    assert guarded_request("PATCH", "http://127.0.0.1/", allow_private=True, json={"ok": True}) == "request"
    assert guarded_get("http://127.0.0.1/", allow_private=True, timeout=3) == "get"
    assert guarded_post("http://127.0.0.1/", allow_private=True, data="body") == "post"
    request.assert_called_once_with(method="PATCH", url="http://127.0.0.1/", json={"ok": True})
    get.assert_called_once_with("http://127.0.0.1/", timeout=3)
    post.assert_called_once_with("http://127.0.0.1/", data="body")


def test_httpx_options_install_guarded_transport_without_changing_origin():
    assert outbound_httpx_options(allow_private=True) == {}
    options = outbound_httpx_options(allow_private=False)
    assert options["trust_env"] is False
    transport = options["transport"]
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    assert isinstance(transport._pool._network_backend, _GuardedAsyncBackend)

    client = httpx.AsyncClient(**options)
    request = client.build_request("GET", "https://public.example.test/path")
    assert request.url.host == "public.example.test"
    assert request.headers["host"] == "public.example.test"
    asyncio.run(client.aclose())


def test_httpx_guarded_transport_preserves_verify_http2_and_limits():
    limits = httpx.Limits(
        max_connections=7,
        max_keepalive_connections=3,
        keepalive_expiry=11,
    )
    options = outbound_httpx_options(
        allow_private=False,
        verify=True,
        http2=True,
        limits=limits,
    )
    transport = options["transport"]
    pool = transport._pool

    assert options["trust_env"] is False
    assert pool._max_connections == 7
    assert pool._max_keepalive_connections == 3
    assert pool._keepalive_expiry == 11
    assert pool._http2 is True
    assert pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
    asyncio.run(transport.aclose())


def test_httpx_backend_pins_numeric_destination_and_preserves_tls_sni(monkeypatch):
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_answer("2606:4700:4700::1111", port)],
    )
    options = outbound_httpx_options(allow_private=False)
    transport = options["transport"]
    backend = transport._pool._network_backend
    fake = _FakeAsyncBackend()
    backend._backend = fake
    origin = httpcore.Origin(b"https", b"public.example.test", 443)
    connection = httpcore.AsyncHTTPConnection(
        origin=origin,
        ssl_context=transport._pool._ssl_context,
        network_backend=backend,
    )
    request = httpcore.Request("GET", "https://public.example.test/path")

    asyncio.run(connection._connect(request))

    assert connection._origin.host == b"public.example.test"
    assert fake.calls[0][0:2] == ("2606:4700:4700::1111", 443)
    assert fake.stream.tls["server_hostname"] == "public.example.test"
    assert fake.stream.tls["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    asyncio.run(transport.aclose())


def test_httpx_backend_rejects_rebound_dns_before_connect(monkeypatch):
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            _answer("93.184.216.34", port),
            _answer("127.0.0.1", port),
        ],
    )
    backend = _GuardedAsyncBackend()
    fake = _FakeAsyncBackend()
    backend._backend = fake

    with pytest.raises(UnsafeOutboundURL):
        asyncio.run(backend.connect_tcp("public.example.test", 443))
    assert fake.calls == []


def test_requests_socket_timeout_maps_to_requests_connect_timeout(monkeypatch):
    monkeypatch.setattr(network.socket, 'getaddrinfo', lambda host, port, **kwargs: [_answer('93.184.216.34', port)])
    monkeypatch.setattr(urllib3_connection.connection, 'create_connection', Mock(side_effect=socket.timeout('connect timed out')))
    with pytest.raises(requests.ConnectTimeout):
        guarded_get('http://public.example.test/', timeout=0.1)


def test_httpx_tries_other_validated_addresses_without_resolving_again(monkeypatch):
    lookup = Mock(return_value=[_answer('2606:4700:4700::1111', 443), _answer('93.184.216.34', 443)])
    monkeypatch.setattr(network.socket, 'getaddrinfo', lookup)
    backend = _GuardedAsyncBackend()
    fake = _FakeAsyncBackend()
    async def connect(host, port, **kwargs):
        fake.calls.append((host, port, kwargs))
        if ':' in host:
            raise httpcore.ConnectError('IPv6 unavailable')
        return fake.stream
    fake.connect_tcp = connect
    backend._backend = fake
    result = asyncio.run(backend.connect_tcp('public.example.test', 443, timeout=1))
    assert result is fake.stream
    assert [row[0] for row in fake.calls] == ['2606:4700:4700::1111', '93.184.216.34']
    lookup.assert_called_once()


def test_httpx_dns_resolution_obeys_the_connect_timeout(monkeypatch):
    async def stalled_resolver(*args, **kwargs):
        await anyio.sleep(10)
    monkeypatch.setattr(anyio.to_thread, 'run_sync', stalled_resolver)
    backend = _GuardedAsyncBackend()
    fake = _FakeAsyncBackend()
    backend._backend = fake
    with pytest.raises(httpcore.ConnectTimeout):
        asyncio.run(backend.connect_tcp('public.example.test', 443, timeout=0.01))
    assert fake.calls == []


@pytest.mark.parametrize('url', ['http://public.example.test:0/', 'https://public.example.test:0/'])
def test_explicit_zero_port_is_rejected_before_dns(url):
    with pytest.raises(UnsafeOutboundURL, match='端口'):
        network.validate_outbound_url(url)


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("proxy", "http://proxy.invalid:8080", "proxies"),
        ("uds", "/tmp/http.sock", "Unix sockets"),
    ],
)
def test_httpx_secure_options_reject_route_bypass_options(option, value, message):
    with pytest.raises(ValueError, match=message):
        outbound_httpx_options(allow_private=False, **{option: value})
