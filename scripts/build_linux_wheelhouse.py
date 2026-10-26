#!/usr/bin/env python3
"""Build and validate a Linux/Python 3.12 dependency wheelhouse.

The output is intentionally kept outside the repository. This command is meant
for a native Linux build host; it does not publish artifacts or change pins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = (
    REPO_ROOT / "requirements-prod.txt",
    REPO_ROOT / "requirements-app-optional.txt",
    REPO_ROOT / "requirements-report-optional.txt",
)
DEFAULT_SOURCE_PACKAGES = ("django-simpleui", "airtest", "pywinauto")
DEFAULT_BUILD_REQUIREMENTS = ("setuptools>=40.8.0", "wheel", "setuptools-rust")
ARTIFACT_SUFFIXES = (".whl", ".tar.gz", ".zip")
FORBIDDEN_MARKERS = ("win32", "win_amd64", "win_arm", "macosx", "musllinux")
METADATA_FILES = {"manifest.json", "manifest.sha256", "SHA256SUMS"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Artifact directory outside the repository",
    )
    parser.add_argument(
        "--index-url",
        default="https://pypi.org/simple",
        help="Package index used for building the artifact",
    )
    parser.add_argument(
        "--extra-index-url",
        action="append",
        default=[],
        help="Additional package index; repeat for multiple indexes",
    )
    parser.add_argument(
        "--requirements",
        action="append",
        type=Path,
        dest="requirements",
        help="Requirement input; defaults to the three repository boundaries",
    )
    parser.add_argument(
        "--source-package",
        action="append",
        default=list(DEFAULT_SOURCE_PACKAGES),
        help="Package allowed to resolve as an sdist; repeat to add one",
    )
    parser.add_argument(
        "--build-requirement",
        action="append",
        default=list(DEFAULT_BUILD_REQUIREMENTS),
        help="Build requirement to add to the wheelhouse; repeat to add one",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Skip downloads and manifest an existing artifact directory",
    )
    return parser.parse_args()


def reject_embedded_credentials(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit("index URLs must not contain credentials, query strings, or fragments")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit(f"unsupported index URL: {url!r}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_native_linux() -> None:
    if sys.platform != "linux":
        raise SystemExit("run this builder on a native Linux host; cross-platform pip resolution is not supported")
    if sys.version_info[:2] != (3, 12):
        raise SystemExit(f"Python 3.12 is required, got {platform.python_version()}")
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise SystemExit(f"Linux x86_64 is required, got {platform.machine()}")


def is_artifact_file(path: Path) -> bool:
    return path.is_file() and path.name.endswith(ARTIFACT_SUFFIXES)


def ensure_output_directory(output: Path, *, allow_artifacts: bool = False) -> Path:
    output = output.expanduser().resolve()
    if output == REPO_ROOT or REPO_ROOT in output.parents:
        raise SystemExit(f"refusing to write artifacts inside the repository: {output}")
    output.mkdir(parents=True, exist_ok=True)
    unexpected = [
        path.name
        for path in output.iterdir()
        if path.name not in METADATA_FILES
        and not (allow_artifacts and is_artifact_file(path))
    ]
    if unexpected:
        raise SystemExit(f"output directory is not empty: {', '.join(sorted(unexpected))}")
    return output


def run_pip_download(
    output: Path,
    requirements: tuple[Path, ...],
    index_url: str,
    extra_index_urls: list[str],
    source_packages: list[str],
) -> None:
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--only-binary=:all:",
        f"--no-binary={','.join(source_packages)}",
        "--dest",
        str(output),
        "--index-url",
        index_url,
    ]
    for extra_url in extra_index_urls:
        command.extend(("--extra-index-url", extra_url))
    for requirement in requirements:
        command.extend(("-r", str(requirement)))
    subprocess.run(command, check=True)


def run_build_download(
    output: Path,
    index_url: str,
    extra_index_urls: list[str],
    build_requirements: list[str],
) -> None:
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--only-binary=:all:",
        "--dest",
        str(output),
        "--index-url",
        index_url,
        *build_requirements,
    ]
    for extra_url in extra_index_urls:
        command.extend(("--extra-index-url", extra_url))
    subprocess.run(command, check=True)


def artifact_paths(output: Path) -> list[Path]:
    artifacts = [path for path in output.iterdir() if is_artifact_file(path)]
    if not artifacts:
        raise SystemExit(f"no package artifacts found in {output}")
    forbidden = [
        path.name
        for path in artifacts
        if any(marker in path.name.lower() for marker in FORBIDDEN_MARKERS)
    ]
    if forbidden:
        raise SystemExit(f"forbidden non-Linux artifacts found: {', '.join(sorted(forbidden))}")
    return sorted(artifacts, key=lambda path: path.name.lower())


def write_manifest(
    output: Path,
    requirements: tuple[Path, ...],
    index_url: str,
    source_packages: list[str],
    build_requirements: list[str],
    extra_index_urls: list[str],
) -> None:
    artifacts = artifact_paths(output)
    manifest = {
        "schema_version": 1,
        "target": {
            "python": "3.12",
            "platform": "linux-x86_64",
            "machine": platform.machine(),
            "libc": platform.libc_ver(),
        },
        "sources": {
            "index_url": index_url,
            "extra_index_urls": list(extra_index_urls),
        },
        "inputs": [
            {"name": requirement.name, "sha256": sha256_file(requirement)}
            for requirement in requirements
        ],
        "source_packages": sorted(set(source_packages)),
        "build_requirements": sorted(set(build_requirements)),
        "artifact_count": len(artifacts),
        "artifacts": [
            {
                "filename": artifact.name,
                "size": artifact.stat().st_size,
                "sha256": sha256_file(artifact),
            }
            for artifact in artifacts
        ],
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_sha = sha256_file(manifest_path)
    (output / "manifest.sha256").write_text(
        f"{manifest_sha}  manifest.json\n",
        encoding="ascii",
    )
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{entry['sha256']}  {entry['filename']}\n"
            for entry in manifest["artifacts"]
        ),
        encoding="ascii",
    )
    print(f"wrote {manifest_path}")
    print(f"artifacts={len(artifacts)}")
    print(f"manifest_sha256={manifest_sha}")
    print(f"index_host={urlsplit(index_url).hostname}")


def main() -> int:
    args = parse_args()
    ensure_native_linux()
    reject_embedded_credentials(args.index_url)
    for extra_url in args.extra_index_url:
        reject_embedded_credentials(extra_url)
    requirements = tuple(path.expanduser().resolve() for path in (args.requirements or DEFAULT_REQUIREMENTS))
    missing = [str(path) for path in requirements if not path.is_file()]
    if missing:
        raise SystemExit(f"missing requirement inputs: {', '.join(missing)}")
    output = ensure_output_directory(args.output, allow_artifacts=args.manifest_only)
    if not args.manifest_only:
        run_pip_download(output, requirements, args.index_url, args.extra_index_url, args.source_package)
        run_build_download(output, args.index_url, args.extra_index_url, args.build_requirement)
    write_manifest(
        output,
        requirements,
        args.index_url,
        args.source_package,
        args.build_requirement,
        args.extra_index_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
