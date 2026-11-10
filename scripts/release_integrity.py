"""Shared release path policy and Git object verification (no credential reads)."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import PurePosixPath

EXCLUDED_PARTS = {
    '.git', '.workflow', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
    'node_modules', 'venv', '.venv', 'venv-prod', 'venv-automation', '.playwright-mcp',
}
EXCLUDED_ROOTS = {
    'backups', 'logs', 'media', 'static_files', 'wheels', 'staging-data',
    '.coverage', '.playwright-mcp', 'testhub-login-192-168-10-111.png',
}
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
GIT_OID_RE = re.compile(r'^[0-9a-f]{40}$')
RELEASE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f'unsafe release path: {value!r}')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/')):
        raise ValueError(f'unsafe release path: {value!r}')
    if path.as_posix() != value:
        raise ValueError(f'noncanonical release path: {value!r}')
    return value


def included_path(value):
    parts = PurePosixPath(safe_relative(value)).parts
    if parts[0] in EXCLUDED_ROOTS or any(part in EXCLUDED_PARTS for part in parts):
        return False
    if parts[0] == 'release-manifest.json' or parts[:2] == ('frontend', 'dist'):
        return False
    if any(part.startswith(('dist.previous.', '.dist-staging.', '.dist.staging.', '.dist.previous.')) for part in parts):
        return False
    name = parts[-1]
    return not (name.startswith('.env') or PurePosixPath(name).suffix.lower() in {'.pem', '.key'})


def checkout_payload_path(value):
    value = safe_relative(value)
    name = PurePosixPath(value).name
    if name.startswith('.env') or PurePosixPath(name).suffix.lower() in {'.pem', '.key'}:
        return False
    return included_path(value) or value.startswith('frontend/dist/')


def git_object_id(kind, payload):
    return hashlib.sha1(f'{kind} {len(payload)}\0'.encode('ascii') + payload).hexdigest()


def decode_object(encoded):
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('invalid encoded Git object') from exc


def verified_source_files(manifest):
    """Verify the commit and tree Merkle proof, returning all regular source files."""
    commit = str(manifest.get('source_commit') or '')
    root_tree = str(manifest.get('source_tree') or '')
    if not GIT_OID_RE.fullmatch(commit) or not GIT_OID_RE.fullmatch(root_tree):
        raise ValueError('release source commit/tree must be full Git SHA-1 identifiers')
    commit_data = decode_object(manifest.get('source_commit_object'))
    if git_object_id('commit', commit_data) != commit or commit_data.split(b'\n', 1)[0] != f'tree {root_tree}'.encode('ascii'):
        raise ValueError('source commit does not bind the declared tree')
    trees = manifest.get('source_tree_objects')
    if not isinstance(trees, dict):
        raise ValueError('missing source tree proof')
    verified = {}
    visiting = set()
    used = set()

    def walk(oid, prefix=''):
        if oid in visiting:
            raise ValueError('cyclic source tree proof')
        visiting.add(oid)
        used.add(oid)
        payload = decode_object(trees.get(oid))
        if git_object_id('tree', payload) != oid:
            raise ValueError('source tree object hash mismatch')
        offset = 0
        names = set()
        while offset < len(payload):
            end = payload.find(b'\0', offset)
            if end < 0 or end + 21 > len(payload):
                raise ValueError('invalid source tree entry')
            try:
                mode, name = payload[offset:end].decode('utf-8').split(' ', 1)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError('invalid source tree path encoding') from exc
            if '/' in name or name in names:
                raise ValueError('invalid or duplicate source tree name')
            names.add(name)
            relative = safe_relative(prefix + name)
            child = payload[end + 1:end + 21].hex()
            offset = end + 21
            if mode == '40000':
                walk(child, relative + '/')
            elif included_path(relative):
                if mode not in {'100644', '100755'}:
                    raise ValueError(f'nonregular source entry: {relative}')
                verified[relative] = {'git_blob': child, 'mode': int(mode[-3:], 8)}
        visiting.remove(oid)

    walk(root_tree)
    if used != set(trees):
        raise ValueError('unreferenced source tree proof objects')
    return verified
