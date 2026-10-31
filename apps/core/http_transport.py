"""Connection-time-pinned HTTP transports for guarded outbound requests."""

from __future__ import annotations

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
    def _new_conn(self):
        try:
            addresses = resolve_outbound_addresses(self._dns_host, self.port)
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
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            **self.poolmanager.pool_classes_by_scheme,
            "http": _PinnedHTTPConnectionPool,
            "https": _PinnedHTTPSConnectionPool,
        }


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


def guarded_request(method, url, *, allow_private=False, **kwargs):
    """Send a Requests request with validated, connection-time DNS pinning."""
    if allow_private:
        return requests.request(method=method, url=url, **kwargs)
    if kwargs.get("proxies") is not None:
        raise ValueError("Guarded Requests do not allow explicit proxies")

    with requests.Session() as session:
        session.trust_env = False
        adapter = _PinnedHTTPAdapter()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session.request(method, url, **kwargs)


def guarded_get(url, *, allow_private=False, **kwargs):
    """GET with guarded connection pinning unless private routes are enabled."""
    if allow_private:
        return requests.get(url, **kwargs)
    return guarded_request("GET", url, allow_private=False, **kwargs)


def guarded_post(url, *, allow_private=False, **kwargs):
    """POST with guarded connection pinning unless private routes are enabled."""
    if allow_private:
        return requests.post(url, **kwargs)
    return guarded_request("POST", url, allow_private=False, **kwargs)


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
