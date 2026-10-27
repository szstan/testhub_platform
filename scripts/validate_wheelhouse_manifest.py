#!/usr/bin/env python3
"""Validate an existing Linux wheelhouse manifest and artifact hashes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from build_linux_wheelhouse import ARTIFACT_SUFFIXES, FORBIDDEN_MARKERS, METADATA_FILES, sha256_file


def artifact_file(path: Path) -> bool:
    return path.is_file() and path.name.endswith(ARTIFACT_SUFFIXES)


def validate(directory: Path, requirement_paths: list[Path]) -> tuple[int, str]:
    directory = directory.expanduser().resolve()
    manifest_path = directory / "manifest.json"
    manifest_sha_path = directory / "manifest.sha256"
    sums_path = directory / "SHA256SUMS"
    for path in (manifest_path, manifest_sha_path, sums_path):
        if not path.is_file():
            raise ValueError(f"missing wheelhouse metadata: {path.name}")

    manifest_hash = sha256_file(manifest_path)
    manifest_sha_text = manifest_sha_path.read_text(encoding="ascii").strip().split()
    if not manifest_sha_text:
        raise ValueError("manifest.sha256 is empty")
    expected_manifest_hash = manifest_sha_text[0]
    if manifest_hash != expected_manifest_hash:
        raise ValueError("manifest.sha256 does not match manifest.json")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("wheelhouse manifest must be a JSON object")
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported wheelhouse manifest schema")
    target = manifest.get("target") or {}
    if target.get("python") != "3.12" or target.get("platform") != "linux-x86_64":
        raise ValueError(f"unsupported wheelhouse target: {target}")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("wheelhouse manifest has no artifacts")
    if manifest.get("artifact_count") != len(artifacts):
        raise ValueError("wheelhouse artifact_count does not match artifacts")

    manifest_by_name = {}
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise ValueError("invalid artifact entry")
        name = str(entry.get("filename") or "")
        if not name or Path(name).name != name or name in manifest_by_name:
            raise ValueError(f"invalid or duplicate artifact filename: {name!r}")
        if any(marker in name.lower() for marker in FORBIDDEN_MARKERS):
            raise ValueError(f"forbidden non-Linux artifact: {name}")
        path = directory / name
        if not artifact_file(path):
            raise ValueError(f"missing or invalid artifact: {name}")
        if path.stat().st_size != entry.get("size"):
            raise ValueError(f"artifact size mismatch: {name}")
        if sha256_file(path) != entry.get("sha256"):
            raise ValueError(f"artifact hash mismatch: {name}")
        manifest_by_name[name] = entry["sha256"]

    sums = {}
    for line in sums_path.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        sums[name.strip()] = digest
    if sums != manifest_by_name:
        raise ValueError("SHA256SUMS does not match manifest artifacts")

    allowed = set(METADATA_FILES) | set(manifest_by_name)
    unexpected = sorted(path.name for path in directory.iterdir() if path.name not in allowed)
    if unexpected:
        raise ValueError(f"unexpected wheelhouse files: {', '.join(unexpected)}")

    inputs = manifest.get("inputs") or []
    input_hashes = {str(item.get("name")): str(item.get("sha256")) for item in inputs if isinstance(item, dict)}
    for requirement in requirement_paths:
        if not requirement.is_file():
            raise ValueError(f"missing requirements input: {requirement}")
        expected = input_hashes.get(requirement.name)
        actual = sha256_file(requirement)
        if expected != actual:
            raise ValueError(f"requirements hash mismatch: {requirement.name}")

    sources = manifest.get("sources") or {}
    if not isinstance(sources, dict):
        raise ValueError("wheelhouse manifest sources must be an object")
    index_url = str(sources.get("index_url") or "")
    extra_index_urls = sources.get("extra_index_urls") or []
    if not isinstance(extra_index_urls, list):
        raise ValueError("wheelhouse manifest extra_index_urls must be a list")
    for raw_url in [index_url, *extra_index_urls]:
        parsed = urlsplit(str(raw_url))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("wheelhouse manifest has an invalid index URL provenance")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("wheelhouse manifest contains unsafe index URL provenance")


    return len(artifacts), manifest_hash


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--requirements", action="append", type=Path, default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        count, manifest_hash = validate(args.directory, [path.expanduser().resolve() for path in args.requirements])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"wheelhouse validation failed: {exc}") from exc
    print(f"wheelhouse valid: artifacts={count} manifest_sha256={manifest_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
