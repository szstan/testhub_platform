#!/usr/bin/env python3
"""Validate complete release bytes and fixed Git source provenance.

Legacy v1 manifests require --allow-legacy and provide partial integrity only;
they never prove source/commit or complete frontend binding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tarfile
from pathlib import Path, PurePosixPath
from typing import Callable

if __package__:
    from .release_integrity import (
        GIT_OID_RE, RELEASE_RE, SHA256_RE, checkout_payload_path,
        git_object_id, safe_relative, verified_source_files,
    )
else:
    from release_integrity import (
        GIT_OID_RE, RELEASE_RE, SHA256_RE, checkout_payload_path,
        git_object_id, safe_relative, verified_source_files,
    )


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _document(payload):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key in release JSON document')
            result[key] = value
        return result
    document = json.loads(payload, object_pairs_hook=unique_object)
    if not isinstance(document, dict):
        raise ValueError('invalid release JSON document')
    return document


def _requirements(manifest, files, read_file):
    requirements = manifest.get('requirements')
    if not isinstance(requirements, list) or not requirements:
        raise ValueError('release manifest has no requirements entries')
    seen = set()
    for entry in requirements:
        if not isinstance(entry, dict):
            raise ValueError('invalid requirements manifest entry')
        path = safe_relative(entry.get('path', ''))
        if '/' in path or not path.startswith('requirements') or not path.endswith('.txt') or path in seen:
            raise ValueError('invalid or duplicate requirements manifest path')
        seen.add(path)
        expected = entry.get('sha256', '')
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected) or digest_bytes(read_file(path)) != expected:
            raise ValueError(f'requirements hash mismatch: {path}')
        if path in files and files[path] != expected:
            raise ValueError('requirements entry disagrees with file inventory')
    return len(seen)


def validate_manifest(manifest, read_file: Callable[[str], bytes], *, expected_release=None, allow_legacy=False):
    if not isinstance(manifest, dict):
        raise ValueError('invalid release manifest document')
    schema = manifest.get('schema_version')
    if schema not in {1, 2} or (schema == 1 and not allow_legacy):
        raise ValueError('strict schema v2 required; --allow-legacy only permits partial v1 integrity')
    release = manifest.get('release', '')
    if not isinstance(release, str) or not RELEASE_RE.fullmatch(release):
        raise ValueError('release identifier is not one safe path component')
    if expected_release is not None and release != expected_release:
        raise ValueError('release identifier mismatch')
    if not GIT_OID_RE.fullmatch(str(manifest.get('source_commit') or '')):
        raise ValueError('release manifest source_commit is not a full Git SHA')
    files = manifest.get('files')
    if not isinstance(files, dict) or 'frontend/dist/index.html' not in files:
        raise ValueError('release manifest has no frontend index hash')
    for path, expected in files.items():
        safe_relative(path)
        if not checkout_payload_path(path):
            raise ValueError(f'forbidden payload path in manifest: {path}')
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            raise ValueError(f'invalid file hash: {path}')
        if digest_bytes(read_file(path)) != expected:
            raise ValueError(f'release file hash mismatch: {path}')
    count = _requirements(manifest, files, read_file)
    if schema == 1:
        return release, count

    source_files = verified_source_files(manifest)
    if source_files != manifest.get('source_files'):
        raise ValueError('source inventory does not match the declared commit/tree')
    for path, entry in source_files.items():
        if path not in files or git_object_id('blob', read_file(path)) != entry['git_blob']:
            raise ValueError(f'source bytes do not match the declared Git blob: {path}')
    for path in set(files) - set(source_files):
        if not path.startswith('frontend/dist/') or not checkout_payload_path(path):
            raise ValueError(f'unbound file in release inventory: {path}')
    if any(path not in files for path in source_files):
        raise ValueError('release inventory omits source files')
    requirement_paths = {entry['path'] for entry in manifest['requirements']}
    if requirement_paths != {path for path in files if '/' not in path and path.startswith('requirements') and path.endswith('.txt')}:
        raise ValueError('requirements inventory is incomplete')
    receipt_path = 'frontend/dist/build-receipt.json'
    if receipt_path not in files:
        raise ValueError('release has no frontend build receipt')
    receipt = _document(read_file(receipt_path).decode('utf-8'))
    if not isinstance(receipt, dict) or receipt.get('schema_version') != 1:
        raise ValueError('invalid frontend build receipt')
    if (receipt.get('source_commit') != manifest['source_commit']
            or receipt.get('source_tree') != manifest['source_tree'] or receipt.get('source_dirty') is not False):
        raise ValueError('frontend build is dirty, unbound or from a different source commit/tree')
    frontend_sources = {path: entry['git_blob'] for path, entry in source_files.items() if path.startswith('frontend/')}
    if receipt.get('source_files') != frontend_sources:
        raise ValueError('frontend build source inputs differ from the declared Git tree')
    receipt_files = receipt.get('files')
    if not isinstance(receipt_files, dict):
        raise ValueError('invalid frontend output inventory')
    expected_outputs = {}
    for relative, digest in receipt_files.items():
        safe_relative(relative)
        if relative == 'build-receipt.json':
            raise ValueError('frontend receipt cannot hash itself')
        expected_outputs['frontend/dist/' + relative] = digest
    actual_outputs = {path: digest for path, digest in files.items() if path.startswith('frontend/dist/') and path != receipt_path}
    if expected_outputs != actual_outputs:
        raise ValueError('frontend output bytes differ from their build receipt')
    return release, count


def _checkout_inventory(root):
    files = set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if not checkout_payload_path(relative) and relative != 'frontend/dist':
                dirs.remove(name)
            elif path.is_symlink():
                raise ValueError(f'symlink directory in release: {relative}')
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == 'release-manifest.json':
                continue
            if checkout_payload_path(relative):
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f'nonregular release file: {relative}')
                files.add(relative)
    return files


def validate_checkout(root: Path, expected_release=None, *, allow_legacy=False):
    root = root.resolve()
    manifest_path = root / 'release-manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError('missing regular release manifest')
    manifest = _document(manifest_path.read_text(encoding='utf-8'))

    def read_file(relative):
        path = root.joinpath(*PurePosixPath(safe_relative(relative)).parts)
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError(f'symlink directory in manifest path: {relative}')
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError(f'missing or unsafe manifest file: {relative}')
        return path.read_bytes()

    result = validate_manifest(manifest, read_file, expected_release=expected_release, allow_legacy=allow_legacy)
    if manifest.get('schema_version') == 2 and _checkout_inventory(root) != set(manifest['files']):
        raise ValueError('checkout contains missing or unlisted release payload files')
    return result


def validate_archive(archive_path: Path, expected_release=None, *, allow_legacy=False):
    with tarfile.open(archive_path, 'r:gz') as archive:
        file_members = {}
        directories = set()
        for member in archive.getmembers():
            name = member.name[:-1] if member.isdir() and member.name.endswith('/') else member.name
            safe_relative(name)
            if name in file_members or name in directories:
                raise ValueError('duplicate archive member')
            if member.isdir():
                if member.mode != 0o755:
                    raise ValueError('archive contains an unsafe directory mode')
                directories.add(name)
            elif member.isreg() and member.mode in {0o644, 0o755}:
                file_members[name] = member
            else:
                raise ValueError('archive contains a link or nonregular/unsafe-mode member')
        manifests = [name for name in file_members if len(PurePosixPath(name).parts) == 2 and name.endswith('/release-manifest.json')]
        if len(manifests) != 1:
            raise ValueError('archive must contain exactly one root release manifest')
        manifest_name = manifests[0]
        prefix = manifest_name.partition('/')[0]
        manifest = _document(archive.extractfile(file_members[manifest_name]).read().decode('utf-8'))
        if prefix != f"testhub_platform-{manifest.get('release', '')}":
            raise ValueError('archive root does not match the release identifier')

        def read_file(relative):
            member = file_members.get(f'{prefix}/{safe_relative(relative)}')
            if member is None:
                raise ValueError(f'archive is missing a manifest file: {relative}')
            return archive.extractfile(member).read()

        result = validate_manifest(manifest, read_file, expected_release=expected_release, allow_legacy=allow_legacy)
        if manifest.get('schema_version') == 2:
            allowed_files = {f'{prefix}/{path}' for path in manifest['files']} | {manifest_name}
            allowed_dirs = set()
            for path in allowed_files:
                allowed_dirs.update(str(parent) for parent in PurePosixPath(path).parents if str(parent) != '.')
            if set(file_members) != allowed_files or not directories.issubset(allowed_dirs):
                raise ValueError('archive contains missing or unlisted payload members')
            for name, member in file_members.items():
                relative = name[len(prefix) + 1:]
                expected_mode = manifest.get('source_files', {}).get(relative, {}).get('mode', 0o644)
                if member.mode != expected_mode:
                    raise ValueError('archive mode differs from fixed source mode')
        return result


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--repo-root', type=Path)
    group.add_argument('--archive', type=Path)
    parser.add_argument('--release', default=None)
    parser.add_argument('--allow-legacy', action='store_true', help='allow v1 partial integrity only, without complete source binding')
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        if args.repo_root is not None:
            result = validate_checkout(args.repo_root.expanduser().resolve(), args.release, allow_legacy=args.allow_legacy)
        else:
            result = validate_archive(args.archive.expanduser().resolve(), args.release, allow_legacy=args.allow_legacy)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        raise SystemExit(f'release manifest validation failed: {exc}') from exc
    mode = 'legacy partial integrity explicitly enabled' if args.allow_legacy else 'strict source/tree and complete byte inventory'
    print(f'release manifest valid ({mode}): release={result[0]} requirements={result[1]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
