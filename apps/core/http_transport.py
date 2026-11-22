"""Connection-time-pinned HTTP transports for guarded outbound requests."""

from __future__ import annotations

import ipaddress
import socket
import sys
from socket import timeout as SocketTimeout

import anyio
import httpcore
import httpx
import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection, connection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    NameResolutionError,
    NewConnectionError,
)

from apps.core.network import UnsafeOutboundURL, resolve_outbound_addresses


class _PinnedConnectionMixin:
    def __init__(self, *args, pinned_addresses=None, pinned_host=None,
                 allow_private=False, **kwargs):
        self._pinned_addresses = tuple(pinned_addresses) if pinned_addresses is not None else None
        self._pinned_host = str(pinned_host or '').strip('[]').lower().rstrip('.')
        self._allow_private = allow_private
        super().__init__(*args, **kwargs)

    def _new_conn(self):
        try:
            hostname = str(self._dns_host).strip('[]').lower().rstrip('.')
            if self._pinned_addresses is not None and hostname == self._pinned_host:
                numeric_addresses = []
                for pinned in self._pinned_addresses:
                    try:
                        numeric_addresses.append(str(ipaddress.ip_address(str(pinned).strip('[]'))))
                    except ValueError as exc:
                        raise UnsafeOutboundURL('固定目标地址必须是数字 IP') from exc
                addresses = tuple(
                    address
                    for pinned in numeric_addresses
                    for address in resolve_outbound_addresses(
                        pinned, self.port, allow_private=self._allow_private,
                    )
                )
            else:
                addresses = resolve_outbound_addresses(
                    self._dns_host, self.port, allow_private=self._allow_private,
                )
        except UnsafeOutboundURL as exc:
            raise NewConnectionError(
                self, f"Blocked unsafe outbound connection: {exc}"
            ) from exc
        last_error = None
        for address in addresses:
            try:
                sock = connection.create_connection(
                    (address, self.port),
                    self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options,
                )
                break
            except socket.gaierror as exc:
                raise NameResolutionError(self.host, self, exc) from exc
            except SocketTimeout as exc:
                last_error = exc
            except OSError as exc:
                last_error = exc
        else:
            if isinstance(last_error, SocketTimeout):
                raise ConnectTimeoutError(
                    self,
                    f"Connection to {self.host} timed out (connect timeout={self.timeout}).",
                ) from last_error
            raise NewConnectionError(
                self, f"Failed to establish a connection to a validated address: {last_error}"
            ) from last_error

        sys.audit("http.client.connect", self, self.host, self.port)
        return sock


class _PinnedHTTPConnection(_PinnedConnectionMixin, HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, HTTPSConnection):
    pass


class _PinnedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class _PinnedHTTPAdapter(HTTPAdapter):
    def __init__(self, *, pinned_addresses=None, pinned_host=None,
                 allow_private=False, **kwargs):
        self.pinned_addresses = tuple(pinned_addresses) if pinned_addresses is not None else None
        self.pinned_host = pinned_host
        self.allow_private = allow_private
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            **self.poolmanager.pool_classes_by_scheme,
            "http": _PinnedHTTPConnectionPool,
            "https": _PinnedHTTPSConnectionPool,
        }

    def _pin_pool(self, pool, url):
        if (self.pinned_addresses is not None
                and httpx.URL(url).host.lower().rstrip('.') == self.pinned_host.lower().rstrip('.')):
            pool.conn_kw.update(
                pinned_addresses=self.pinned_addresses,
                pinned_host=self.pinned_host,
                allow_private=self.allow_private,
            )
        return pool

    def get_connection(self, url, proxies=None):
        return self._pin_pool(super().get_connection(url, proxies), url)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        get_connection = getattr(super(), "get_connection_with_tls_context", None)
        if get_connection is None:
            return self.get_connection(request.url, proxies)
        pool = get_connection(request, verify, proxies, cert)
        return self._pin_pool(pool, request.url)


class _GuardedAsyncBackend(httpcore._backends.auto.AutoBackend):
    async def connect_tcp(
        self,
        host,
        port,
        timeout=None,
        local_address=None,
        socket_options=None,
    ):
        try:
            # Include DNS in the caller's connect budget. Abandoning a slow
            # resolver thread cannot open a socket: only this task connects.
            with anyio.fail_after(timeout):
                addresses = await anyio.to_thread.run_sync(
                    resolve_outbound_addresses, host, port, abandon_on_cancel=True,
                )
                last_error = None
                for address in addresses:
                    try:
                        return await super().connect_tcp(
                            address, port, timeout=timeout,
                            local_address=local_address, socket_options=socket_options,
                        )
                    except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                        last_error = exc
                raise last_error
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout('Outbound DNS/connection timed out') from exc


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, **kwargs):
        proxy = kwargs.pop("proxy", None)
        uds = kwargs.pop("uds", None)
        if proxy is not None or uds is not None:
            raise ValueError("Guarded HTTPX transport does not allow proxies or Unix sockets")
        kwargs["trust_env"] = False
        super().__init__(proxy=None, **kwargs)
        pool = getattr(self, "_pool", None)
        if not isinstance(pool, httpcore.AsyncConnectionPool):
            raise RuntimeError("Unsupported HTTPX transport pool; guarded backend unavailable")
        pool._network_backend = _GuardedAsyncBackend()


def guarded_request(method, url, *, allow_private=False, pinned_addresses=None, **kwargs):
    """Send Requests with connection-time DNS pinning.

    Callers may supply numeric addresses already resolved and validated for this
    URL's hostname. They are rechecked as IP literals, then used without DNS.
    """
    if allow_private and pinned_addresses is None:
        return requests.request(method=method, url=url, **kwargs)
    if kwargs.get("proxies") is not None:
        raise ValueError("Guarded Requests do not allow explicit proxies")

    parsed_host = httpx.URL(url).host
    if pinned_addresses is not None and not pinned_addresses:
        raise ValueError("pinned_addresses must contain at least one address")

    with requests.Session() as session:
        session.trust_env = False
        adapter = _PinnedHTTPAdapter(
            pinned_addresses=pinned_addresses,
            pinned_host=parsed_host if pinned_addresses is not None else None,
            allow_private=allow_private,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session.request(method, url, **kwargs)


def guarded_get(url, *, allow_private=False, pinned_addresses=None, **kwargs):
    """GET with guarded connection pinning unless private routes are enabled."""
    if allow_private and pinned_addresses is None:
        return requests.get(url, **kwargs)
    return guarded_request(
        "GET", url, allow_private=allow_private,
        pinned_addresses=pinned_addresses, **kwargs,
    )


def guarded_post(url, *, allow_private=False, pinned_addresses=None, **kwargs):
    """POST with guarded connection pinning unless private routes are enabled."""
    if allow_private and pinned_addresses is None:
        return requests.post(url, **kwargs)
    return guarded_request(
        "POST", url, allow_private=allow_private,
        pinned_addresses=pinned_addresses, **kwargs,
    )


def outbound_httpx_options(allow_private, verify=True, http2=False, **options):
    """Return AsyncClient options for a DNS-pinned HTTPX transport."""
    if allow_private:
        return {}
    transport = _PinnedAsyncHTTPTransport(
        verify=verify,
        http2=http2,
        **options,
    )
    return {"transport": transport, "trust_env": False}


__all__ = [
    "guarded_get",
    "guarded_post",
    "guarded_request",
    "outbound_httpx_options",
]
