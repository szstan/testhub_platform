#!/usr/bin/env python3
"""Validate the reproducibility manifest embedded in a release or checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Callable


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_relative(value: str) -> str:
    path = PurePosixPath(str(value))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe manifest path: {value!r}")
    return path.as_posix()


def validate_manifest(
    manifest: dict,
    read_file: Callable[[str], bytes],
    *,
    expected_release: str | None = None,
) -> tuple[str, int]:
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported release manifest schema")
    release = str(manifest.get("release") or "").strip()
    if not release:
        raise ValueError("release manifest has no release identifier")
    if expected_release is not None and release != expected_release:
        raise ValueError(f"release mismatch: expected {expected_release!r}, got {release!r}")

    source_commit = str(manifest.get("source_commit") or "").strip().lower()
    if not _COMMIT_RE.fullmatch(source_commit):
        raise ValueError("release manifest source_commit is not a full git SHA")

    requirements = manifest.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("release manifest has no requirements entries")
    seen = set()
    for entry in requirements:
        if not isinstance(entry, dict):
            raise ValueError("invalid requirements manifest entry")
        path = safe_relative(entry.get("path", ""))
        if not path.startswith("requirements") or not path.endswith(".txt"):
            raise ValueError(f"invalid requirements manifest path: {path}")
        if path in seen:
            raise ValueError(f"duplicate requirements manifest path: {path}")
        seen.add(path)
        expected_hash = str(entry.get("sha256") or "").lower()
        if not _SHA256_RE.fullmatch(expected_hash):
            raise ValueError(f"invalid requirements hash for {path}")
        actual_hash = digest_bytes(read_file(path))
        if actual_hash != expected_hash:
            raise ValueError(f"requirements hash mismatch: {path}")

    files = manifest.get("files")
    if not isinstance(files, dict) or files.get("frontend/dist/index.html") is None:
        raise ValueError("release manifest has no frontend index hash")
    for raw_path, raw_hash in files.items():
        path = safe_relative(raw_path)
        expected_hash = str(raw_hash or "").lower()
        if not _SHA256_RE.fullmatch(expected_hash):
            raise ValueError(f"invalid file hash for {path}")
        actual_hash = digest_bytes(read_file(path))
        if actual_hash != expected_hash:
            raise ValueError(f"release file hash mismatch: {path}")

    return release, len(requirements)


def validate_checkout(root: Path, expected_release: str | None) -> tuple[str, int]:
    manifest_path = root / "release-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"missing release manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    def read_file(relative: str) -> bytes:
        candidate = (root / Path(*PurePosixPath(relative).parts)).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError(f"manifest path escapes checkout: {relative}") from exc
        if not candidate.is_file():
            raise ValueError(f"missing manifest file: {relative}")
        return candidate.read_bytes()

    return validate_manifest(manifest, read_file, expected_release=expected_release)


def validate_archive(archive_path: Path, expected_release: str | None) -> tuple[str, int]:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        manifest_members = [member for member in members if member.name.endswith("/release-manifest.json")]
        if len(manifest_members) != 1:
            raise ValueError("archive must contain exactly one release manifest")
        manifest_member = manifest_members[0]
        prefix = manifest_member.name[: -len("/release-manifest.json")]
        manifest = json.loads(archive.extractfile(manifest_member).read().decode("utf-8"))
        member_map = {member.name: member for member in members}

        def read_file(relative: str) -> bytes:
            member = member_map.get(f"{prefix}/{relative}")
            if member is None:
                raise ValueError(f"archive is missing manifest file: {relative}")
            return archive.extractfile(member).read()

        return validate_manifest(manifest, read_file, expected_release=expected_release)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--repo-root", type=Path)
    group.add_argument("--archive", type=Path)
    parser.add_argument("--release", default=None, help="expected release identifier")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.repo_root is not None:
            release, count = validate_checkout(args.repo_root.expanduser().resolve(), args.release)
        else:
            archive = args.archive.expanduser().resolve()
            if not archive.is_file():
                raise ValueError(f"missing archive: {archive}")
            release, count = validate_archive(archive, args.release)
    except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
        raise SystemExit(f"release manifest validation failed: {exc}") from exc
    print(f"release manifest valid: release={release} requirements={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
