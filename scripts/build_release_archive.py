#!/usr/bin/env python3
"""Package a clean fixed Git tree plus a frontend build bound to that tree.

This creates a local transport artifact only. It never commits, publishes or
uses untracked source/runtime files. Dirty development builds remain usable,
but must be committed and rebuilt before they can become a strict release.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

if __package__:
    from .release_integrity import RELEASE_RE, git_object_id, safe_relative, verified_source_files
    from .validate_release_manifest import validate_manifest
else:
    from release_integrity import RELEASE_RE, git_object_id, safe_relative, verified_source_files
    from validate_release_manifest import validate_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(*args):
    try:
        return subprocess.check_output(['git', *args], cwd=REPO_ROOT, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit('unable to read the selected Git source tree') from exc


def git_commit(ref='HEAD'):
    return _git('rev-parse', '--verify', '--end-of-options', f'{ref}^{{commit}}').decode('ascii').strip()


def git_release():
    return git_commit()[:7]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _require_clean(commit):
    result = subprocess.run(['git', 'diff', '--quiet', commit, '--'], cwd=REPO_ROOT, capture_output=True)
    if result.returncode:
        raise SystemExit('tracked source differs from the selected commit; commit and rebuild before packaging')


def _objects(oids):
    selected = sorted(set(oids))
    if not selected:
        return {}
    try:
        output = subprocess.check_output(
            ['git', 'cat-file', '--batch'], input=('\n'.join(selected) + '\n').encode('ascii'),
            cwd=REPO_ROOT, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit('unable to export fixed Git objects') from exc
    result = {}
    offset = 0
    for requested in selected:
        end = output.find(b'\n', offset)
        try:
            oid, kind, raw_size = output[offset:end].decode('ascii').split()
            size = int(raw_size)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SystemExit('invalid Git object export') from exc
        payload = output[end + 1:end + 1 + size]
        if oid != requested or git_object_id(kind, payload) != oid:
            raise SystemExit('Git object export failed integrity verification')
        result[oid] = (kind, payload)
        offset = end + size + 2
    return result


def _frontend_files():
    root = REPO_ROOT / 'frontend' / 'dist'
    if root.is_symlink() or root.parent.is_symlink():
        raise SystemExit('frontend build contains a symlink directory')
    if not (root / 'index.html').is_file() or not (root / 'build-receipt.json').is_file():
        raise SystemExit('frontend production index and build-receipt.json are required; rebuild before packaging')
    payloads = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in [*dirs, *names]:
            candidate = Path(directory) / name
            if candidate.is_symlink():
                raise SystemExit('frontend build contains a symlink')
        for name in names:
            path = Path(directory) / name
            if name.startswith('.env') or path.suffix.lower() in {'.key', '.pem'}:
                raise SystemExit('frontend build contains a private configuration/key path')
            relative = safe_relative(path.relative_to(REPO_ROOT).as_posix())
            payloads[relative] = path.read_bytes()
    return payloads


def build_archive(output: Path, release: str, source_ref='HEAD'):
    if not RELEASE_RE.fullmatch(release):
        raise SystemExit('release identifier must be one safe path component (letters, digits, dot, underscore or hyphen)')
    commit = git_commit(source_ref)
    _require_clean(commit)
    tree = _git('rev-parse', f'{commit}^{{tree}}').decode('ascii').strip()
    tree_oids = {tree}
    for record in _git('ls-tree', '-rtz', '--full-tree', commit).split(b'\0'):
        if record:
            metadata, _path = record.split(b'\t', 1)
            _mode, kind, oid = metadata.decode('ascii').split()
            if kind == 'tree':
                tree_oids.add(oid)
    proof = _objects([commit, *tree_oids])
    manifest = {
        'schema_version': 2, 'release': release, 'source_commit': commit, 'source_tree': tree,
        'source_commit_object': base64.b64encode(proof[commit][1]).decode('ascii'),
        'source_tree_objects': {oid: base64.b64encode(proof[oid][1]).decode('ascii') for oid in sorted(tree_oids)},
    }
    source_files = verified_source_files(manifest)
    blobs = _objects(entry['git_blob'] for entry in source_files.values())
    payloads = {path: blobs[entry['git_blob']][1] for path, entry in source_files.items()}
    frontend = _frontend_files()
    if set(payloads) & set(frontend):
        raise SystemExit('frontend output overlaps fixed source inventory')
    payloads.update(frontend)
    manifest['source_files'] = source_files
    manifest['files'] = {path: hashlib.sha256(data).hexdigest() for path, data in sorted(payloads.items())}
    manifest['requirements'] = [
        {'path': path, 'sha256': digest}
        for path, digest in manifest['files'].items() if '/' not in path and path.startswith('requirements') and path.endswith('.txt')
    ]
    validate_manifest(manifest, payloads.__getitem__, expected_release=release)
    _require_clean(commit)

    output = output.expanduser().resolve()
    if output == REPO_ROOT or REPO_ROOT in output.parents:
        raise SystemExit('refusing to write a release archive inside the repository')
    if output.exists():
        raise SystemExit('refusing to overwrite an existing release archive')
    output.parent.mkdir(parents=True, exist_ok=True)
    payloads['release-manifest.json'] = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
    archive_root = f'testhub_platform-{release}'
    owned_output = False
    try:
        with output.open('xb') as destination:
            owned_output = True
            with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode='w') as archive:
                    for path, payload in sorted(payloads.items()):
                        info = tarfile.TarInfo(f'{archive_root}/{path}')
                        info.mode = source_files.get(path, {}).get('mode', 0o644)
                        info.size = len(payload)
                        archive.addfile(info, io.BytesIO(payload))
    except Exception:
        if owned_output:
            output.unlink(missing_ok=True)
        raise
    print(f'archive={output}')
    print(f'release={release}')
    print(f'source_commit={commit}')
    print(f'source_tree={tree}')
    print(f'sha256={sha256(output)}')


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--release', default=None)
    parser.add_argument('--source-ref', default='HEAD', help='fixed source commit/ref; working tracked source must match it')
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        build_archive(args.output, args.release if args.release is not None else git_commit(args.source_ref)[:7], args.source_ref)
    except ValueError as exc:
        raise SystemExit(f'release build refused: {exc}') from exc
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
