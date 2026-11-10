"""DNS-pinned loopback proxy for Selenium browser egress."""

from __future__ import annotations

import logging
import select
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit, urlunsplit

import requests

from apps.core.http_transport import guarded_request
from apps.core.network import UnsafeOutboundURL, resolve_outbound_addresses

logger = logging.getLogger(__name__)

_HOP_BY_HOP_HEADERS = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade',
}


def _open_pinned_connection(host, port, *, timeout=15):
    """Connect only to an address returned by the guarded resolver."""
    addresses = resolve_outbound_addresses(host, port)
    last_error = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout=timeout)
        except OSError as exc:
            last_error = exc
    raise OSError(f'could not connect to a validated browser target: {last_error}')


def _parse_authority(authority, *, default_port=None):
    try:
        parsed = urlsplit(f'//{authority}')
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError
        port = parsed.port if parsed.port is not None else default_port
        if port is None or not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise UnsafeOutboundURL('invalid proxy destination') from exc
    return parsed.hostname, port


def _relay(left, right):
    sockets = (left, right)
    while True:
        readable, _, exceptional = select.select(sockets, (), sockets, 120)
        if exceptional or not readable:
            return
        for source in readable:
            try:
                data = source.recv(65536)
            except OSError:
                return
            if not data:
                return
            destination = right if source is left else left
            try:
                destination.sendall(data)
            except OSError:
                return


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'TestHubGuardedProxy/1.0'

    def log_message(self, _format, *_args):
        return

    def handle_expect_100(self):
        self.send_response_only(100)
        self.end_headers()
        return True

    def do_CONNECT(self):
        upstream = None
        try:
            host, port = _parse_authority(self.path)
            upstream = _open_pinned_connection(host, port)
            self.send_response(200, 'Connection Established')
            self.end_headers()
            self.close_connection = True
            _relay(self.connection, upstream)
        except UnsafeOutboundURL:
            self.send_error(403, 'Outbound destination is restricted')
        except (OSError, ValueError):
            self.send_error(502, 'Could not connect to outbound destination')
        finally:
            if upstream is not None:
                upstream.close()

    def _target(self):
        try:
            parsed = urlsplit(self.path)
            scheme = parsed.scheme.lower()
            if scheme not in {'http', 'ws'} or not parsed.hostname:
                raise ValueError
            if parsed.username is not None or parsed.password is not None:
                raise ValueError
            port = parsed.port if parsed.port is not None else 80
            if not 1 <= port <= 65535:
                raise ValueError
            path = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
            target_url = urlunsplit(('http', parsed.netloc, parsed.path or '/', parsed.query, ''))
        except ValueError as exc:
            raise UnsafeOutboundURL('invalid HTTP proxy request target') from exc
        return parsed.hostname, port, path, target_url

    def _forward_upgrade(self, host, port, path):
        upstream = _open_pinned_connection(host, port)
        try:
            upstream.sendall(f'{self.command} {path} {self.request_version}\r\n'.encode('iso-8859-1'))
            for name, value in self.headers.raw_items():
                if name.lower() not in _HOP_BY_HOP_HEADERS - {'upgrade'}:
                    upstream.sendall(f'{name}: {value}\r\n'.encode('iso-8859-1'))
            upstream.sendall(b'Connection: Upgrade\r\n\r\n')
            self.close_connection = True
            _relay(self.connection, upstream)
        finally:
            upstream.close()

    def _read_chunked_body(self):
        body = bytearray()
        while True:
            line = self.rfile.readline(65537)
            if not line.endswith(b'\r\n'):
                raise ValueError('invalid chunked request body')
            try:
                size = int(line.split(b';', 1)[0].strip(), 16)
            except ValueError as exc:
                raise ValueError('invalid chunked request body') from exc
            if size == 0:
                while self.rfile.readline(65537) not in (b'\r\n', b''):
                    pass
                return bytes(body)
            if size < 0 or len(body) + size > 128 * 1024 * 1024:
                raise ValueError('request body is too large')
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.read(2) != b'\r\n':
                raise ValueError('truncated chunked request body')
            body.extend(chunk)

    def _forward_http(self):
        try:
            host, port, path, target_url = self._target()
            if self.headers.get('Upgrade') and 'upgrade' in self.headers.get('Connection', '').lower():
                self._forward_upgrade(host, port, path)
                return
            transfer_encoding = self.headers.get('Transfer-Encoding', '').lower()
            if 'chunked' in transfer_encoding:
                body = self._read_chunked_body()
            else:
                length = int(self.headers.get('Content-Length') or 0)
                if length < 0 or length > 128 * 1024 * 1024:
                    self.send_error(413, 'Request body is too large')
                    return
                body = self.rfile.read(length) if length else None
            headers = {
                name: value for name, value in self.headers.items()
                if name.lower() not in _HOP_BY_HOP_HEADERS
            }
            headers['Connection'] = 'close'
            response = guarded_request(
                self.command,
                target_url,
                allow_private=False,
                headers=headers,
                data=body,
                allow_redirects=False,
                stream=True,
                timeout=(15, 120),
            )
            try:
                self.send_response(response.status_code)
                raw_headers = response.raw.headers
                for name in raw_headers.keys():
                    if name.lower() in _HOP_BY_HOP_HEADERS:
                        continue
                    values = raw_headers.getlist(name) if hasattr(raw_headers, 'getlist') else [response.headers[name]]
                    for value in values:
                        self.send_header(name, value)
                self.send_header('Connection', 'close')
                self.end_headers()
                if self.command != 'HEAD':
                    for chunk in response.raw.stream(65536, decode_content=False):
                        if chunk:
                            self.wfile.write(chunk)
            finally:
                response.close()
        except UnsafeOutboundURL:
            self.send_error(403, 'Outbound destination is restricted')
        except (OSError, requests.RequestException, ValueError):
            self.send_error(502, 'Could not connect to outbound destination')
        finally:
            self.close_connection = True

    do_GET = _forward_http
    do_HEAD = _forward_http
    do_POST = _forward_http
    do_PUT = _forward_http
    do_PATCH = _forward_http
    do_DELETE = _forward_http
    do_OPTIONS = _forward_http
    do_TRACE = _forward_http


class GuardedBrowserProxy:
    """An ephemeral proxy that connects only to validated public addresses."""

    def __init__(self):
        self._server = None
        self._thread = None

    @property
    def url(self):
        if self._server is None:
            raise RuntimeError('guarded browser proxy is not running')
        host, port = self._server.server_address
        return f'http://{host}:{port}'

    def start(self):
        self._server = ThreadingHTTPServer(('127.0.0.1', 0), _ProxyHandler)
        self._server.daemon_threads = True
        self._thread = Thread(target=self._server.serve_forever, name='selenium-egress-proxy', daemon=True)
        self._thread.start()
        return self

    def close(self):
        if self._server is None:
            return
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)


def _configure_browser_proxy(options, browser_type, proxy_url):
    browser_type = str(browser_type or '').lower()
    parsed = urlsplit(proxy_url)
    host, port = parsed.hostname, parsed.port
    if browser_type in {'chrome', 'edge'}:
        options.add_argument(f'--proxy-server={proxy_url}')
        options.add_argument('--proxy-bypass-list=<-loopback>')
        options.add_argument('--disable-quic')
        options.add_argument('--force-webrtc-ip-handling-policy=disable_non_proxied_udp')
        return
    if browser_type == 'firefox':
        options.set_preference('network.proxy.type', 1)
        options.set_preference('network.proxy.http', host)
        options.set_preference('network.proxy.http_port', port)
        options.set_preference('network.proxy.ssl', host)
        options.set_preference('network.proxy.ssl_port', port)
        options.set_preference('network.proxy.share_proxy_settings', True)
        options.set_preference('network.proxy.no_proxies_on', '')
        options.set_preference('network.proxy.allow_hijacking_localhost', True)
        options.set_preference('network.dns.disablePrefetch', True)
        options.set_preference('media.peerconnection.enabled', False)
        return
    raise RuntimeError(f'Selenium {browser_type or "unknown"} cannot enforce guarded browser egress')


def create_guarded_selenium_driver(driver_factory, options=None, *, browser_type, allow_private=False, **kwargs):
    """Create a Selenium driver with all HTTP(S) requests forced through the guarded proxy."""
    proxy = None
    if not allow_private:
        proxy = GuardedBrowserProxy().start()
        try:
            _configure_browser_proxy(options, browser_type, proxy.url)
        except Exception:
            proxy.close()
            raise

    try:
        driver = driver_factory(options=options, **kwargs)
    except Exception:
        if proxy is not None:
            proxy.close()
        raise

    if proxy is not None:
        original_quit = driver.quit

        def quit_with_proxy(*args, **call_kwargs):
            try:
                return original_quit(*args, **call_kwargs)
            finally:
                proxy.close()

        try:
            driver._testhub_guarded_proxy = proxy
            driver.quit = quit_with_proxy
        except Exception as exc:
            proxy.close()
            try:
                original_quit()
            except Exception:
                pass
            raise RuntimeError('Selenium driver cannot retain its guarded egress proxy') from exc
    return driver
