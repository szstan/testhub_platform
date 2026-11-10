#!/usr/bin/env python3
"""Bind staging probes to the service endpoints described by candidate Nginx."""
from __future__ import annotations

import argparse
import ipaddress
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_TOKEN = re.compile(r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|#[^\r\n]*|[{};]|[^\s{};#]+)''')


@dataclass(frozen=True)
class _Node:
    name: str
    args: tuple[str, ...]
    children: tuple['_Node', ...] = ()


def _tokens(text: str) -> list[str]:
    result = []
    for match in _TOKEN.finditer(text):
        value = match.group(0)
        if value.startswith('#'):
            continue
        if value.startswith(('"', "'")):
            parsed = shlex.split(value)
            if len(parsed) != 1:
                raise ValueError('invalid quoted Nginx token')
            value = parsed[0]
        result.append(value)
    return result


def _parse_nodes(tokens: list[str], start: int = 0, *, nested: bool = False):
    nodes = []
    statement = []
    index = start
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == '{':
            if not statement:
                raise ValueError('invalid Nginx block header')
            children, index = _parse_nodes(tokens, index, nested=True)
            nodes.append(_Node(statement[0], tuple(statement[1:]), tuple(children)))
            statement = []
        elif token == ';':
            if statement:
                nodes.append(_Node(statement[0], tuple(statement[1:])))
                statement = []
        elif token == '}':
            if not nested or statement:
                raise ValueError('unbalanced Nginx configuration block')
            return nodes, index
        else:
            statement.append(token)
    if nested or statement:
        raise ValueError('unterminated Nginx configuration block or directive')
    return nodes, index


def _descendants(nodes, name):
    for node in nodes:
        if node.name == name:
            yield node
        yield from _descendants(node.children, name)


def _directives(node: _Node, name: str):
    return [child for child in node.children if child.name == name and not child.children]


def _parse_origin(value: str, label: str):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
            raise ValueError
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == 'https' else 80
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f'{label} must be an HTTP(S) origin without credentials or a path') from exc
    return parsed.scheme, parsed.hostname.lower().rstrip('.'), port


def _listen_origin(listen: _Node):
    if not listen.args:
        raise ValueError('candidate Nginx server has no usable listen directive')
    address = listen.args[0]
    tls = 'ssl' in listen.args[1:]
    scheme = 'https' if tls else 'http'
    if address.startswith('unix:'):
        raise ValueError('Unix-socket Nginx listeners cannot be validated by HTTP probes')
    if address.isdigit():
        host, port = '*', int(address)
    elif address.startswith('['):
        parsed = urlsplit(f'//{address}')
        host, port = parsed.hostname, parsed.port or (443 if tls else 80)
    elif address.count(':') == 1:
        host, raw_port = address.rsplit(':', 1)
        if not raw_port.isdigit():
            raise ValueError('candidate Nginx listen port is not numeric')
        port = int(raw_port)
    else:
        try:
            host = str(ipaddress.ip_address(address))
        except ValueError:
            host = address
        port = 443 if tls else 80
    if not host or not 1 <= port <= 65535:
        raise ValueError('candidate Nginx listen endpoint is invalid')
    return scheme, host.lower().rstrip('.'), port


def _frontend_matches(candidate, configured):
    scheme, host, port = candidate
    actual_scheme, actual_host, actual_port = configured
    if scheme != actual_scheme or port != actual_port:
        return False
    if host in {'*', '0.0.0.0'}:
        return actual_host in {'127.0.0.1', 'localhost'}
    if host in {'::', '[::]'}:
        return actual_host in {'::1', 'localhost'}
    return host == actual_host


def _location_path(location):
    if not location.args:
        return '', None
    if location.args[0] in {'=', '^~', '~', '~*'}:
        return (location.args[1], location.args[0]) if len(location.args) > 1 else ('', location.args[0])
    return location.args[0], None


def _validate_candidate_frontend_routes(server, repo_root):
    locations = list(_descendants(server.children, 'location'))
    default_locations = [
        location for location in locations
        if _location_path(location) == ('/', None)
    ]
    api_locations = [
        location for location in locations
        if _location_path(location) == ('/api/', None)
    ]
    if len(default_locations) != 1:
        raise ValueError('candidate Nginx server must have exactly one default frontend location')
    if len(api_locations) != 1:
        raise ValueError('candidate Nginx server must have exactly one /api/ location')

    indexes = _directives(server, 'index')
    if len(indexes) != 1 or indexes[0].args != ('index.html',):
        raise ValueError('candidate Nginx server must serve the candidate index.html')
    try_files = _directives(default_locations[0], 'try_files')
    if len(try_files) != 1 or '/index.html' not in try_files[0].args:
        raise ValueError('candidate default frontend location must fall back to its index.html')

    api_proxy = _directives(api_locations[0], 'proxy_pass')
    if len(api_proxy) != 1 or len(api_proxy[0].args) != 1:
        raise ValueError('candidate /api/ location must have exactly one direct proxy_pass target')

    proxy_directives = {'proxy_pass', 'fastcgi_pass', 'uwsgi_pass', 'scgi_pass', 'grpc_pass'}
    for location in locations:
        path, _modifier = _location_path(location)
        directives = {item.name for item in location.children if not item.children}
        if 'root' in directives:
            raise ValueError('candidate Nginx locations must not override the candidate frontend root')
        if 'alias' in directives:
            aliases = _directives(location, 'alias')
            expected_static = (repo_root / 'static_files').resolve()
            if (path != '/static/' or len(aliases) != 1 or len(aliases[0].args) != 1
                    or Path(aliases[0].args[0]).resolve() != expected_static):
                raise ValueError('candidate Nginx location aliases must not override frontend assets')
        if directives & proxy_directives:
            if path not in {'/api/', '/ws/'}:
                raise ValueError('candidate Nginx contains competing frontend proxy routing')
            if path == '/ws/' and len(_directives(location, 'proxy_pass')) != 1:
                raise ValueError('candidate /ws/ location must have one direct proxy_pass')
        if directives & {'rewrite', 'return'} and path not in {'/health', '/media/'}:
            raise ValueError('candidate Nginx contains competing frontend rewrite or return routing')

    return api_locations[0]


def validate_probe_targets(repo_root, nginx_config, frontend_url, backend_url):
    repo_root = Path(repo_root).resolve()
    config_path = Path(nginx_config).resolve()
    expected_root = (repo_root / 'frontend' / 'dist').resolve()
    try:
        nodes, _ = _parse_nodes(_tokens(config_path.read_text(encoding='utf-8')))
    except OSError as exc:
        raise ValueError(f'cannot read candidate Nginx config: {config_path}') from exc

    candidates = []
    for server in _descendants(nodes, 'server'):
        roots = _directives(server, 'root')
        if len(roots) != 1 or len(roots[0].args) != 1:
            continue
        configured_root = Path(roots[0].args[0])
        if not configured_root.is_absolute() or configured_root.resolve() != expected_root:
            continue
        candidates.append(server)
    if len(candidates) != 1:
        raise ValueError('candidate Nginx config must contain exactly one server rooted at this candidate frontend/dist')

    server = candidates[0]
    listeners = _directives(server, 'listen')
    if len(listeners) != 1:
        raise ValueError('candidate Nginx server must have exactly one HTTP-probe listener')
    api_location = _validate_candidate_frontend_routes(server, repo_root)

    listener_origin = _listen_origin(listeners[0])
    frontend_origin = _parse_origin(frontend_url, 'STAGING_FRONTEND_URL')
    if not _frontend_matches(listener_origin, frontend_origin):
        raise ValueError(
            f'STAGING_FRONTEND_URL does not match candidate Nginx listener {listener_origin[0]}://{listener_origin[1]}:{listener_origin[2]}'
        )

    proxy_target = _directives(api_location, 'proxy_pass')[0].args[0]
    if '$' in proxy_target:
        raise ValueError('variable-based candidate proxy_pass targets are not supported for staging probes')
    proxy_origin = _parse_origin(proxy_target, 'candidate proxy_pass')
    upstream_names = {
        upstream.args[0].lower() for upstream in _descendants(nodes, 'upstream') if upstream.args
    }
    if proxy_origin[1] in upstream_names:
        raise ValueError('named Nginx upstream targets are not supported for staging probes')
    backend_origin = _parse_origin(backend_url, 'STAGING_BACKEND_URL')
    if proxy_origin != backend_origin:
        raise ValueError(
            f'STAGING_BACKEND_URL does not match candidate /api/ proxy_pass {proxy_origin[0]}://{proxy_origin[1]}:{proxy_origin[2]}'
        )
    return listener_origin, proxy_origin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', required=True, type=Path)
    parser.add_argument('--nginx-config', required=True, type=Path)
    parser.add_argument('--frontend-url', required=True)
    parser.add_argument('--backend-url', required=True)
    args = parser.parse_args()
    try:
        frontend, backend = validate_probe_targets(
            args.repo_root, args.nginx_config, args.frontend_url, args.backend_url,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f'staging candidate target validation failed: {exc}') from exc
    print(
        'staging probe targets match candidate Nginx: '
        f'frontend={frontend[0]}://{frontend[1]}:{frontend[2]} '
        f'backend={backend[0]}://{backend[1]}:{backend[2]}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
