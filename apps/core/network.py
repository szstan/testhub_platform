"""Outbound URL validation for server-side test runners."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeOutboundURL(ValueError):
    pass


def validate_outbound_url(url: str, *, allow_private: bool = False) -> str:
    parsed = urlparse(str(url or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise UnsafeOutboundURL('只允许 http/https URL')
    if parsed.username or parsed.password:
        raise UnsafeOutboundURL('URL 不允许包含认证信息')
    if not allow_private:
        try:
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        except ValueError as exc:
            raise UnsafeOutboundURL('URL 端口无效') from exc
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise UnsafeOutboundURL('目标主机无法解析') from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                raise UnsafeOutboundURL('目标地址属于受限网络')
    return parsed.geturl()
