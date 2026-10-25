#!/usr/bin/env python3
"""Build a source release archive with the production frontend artifact included.

The archive is a transport artifact only. It never publishes or deploys a
release, and it intentionally excludes local credentials, virtualenvs,
node_modules, caches, and repository workflow scratch data.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {
    ".git",
    ".workflow",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    "venv-prod",
    "venv-automation",
}
EXCLUDED_ROOTS = {"backups", "logs", "media", "static_files", "wheels", "testhub-login-192-168-10-111.png", ".coverage", ".playwright-mcp"}


def git_release() -> str:
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            encoding='utf-8',
            errors='replace',
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"unable to determine git release: {exc}") from exc
    if not value:
        raise SystemExit("git returned an empty release identifier")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="output .tar.gz path")
    parser.add_argument("--release", default=None, help="release identifier; defaults to git HEAD")
    return parser.parse_args()


def should_include(path: Path) -> bool:
    relative = path.relative_to(REPO_ROOT)
    if not relative.parts:
        return False
    if relative.parts[0] in EXCLUDED_ROOTS:
        return False
    if path.name.startswith('.env') or path.suffix.lower() in {'.pem', '.key'}:
        return False
    if any(part.startswith('dist.previous.') or part.startswith('.dist-staging.') for part in relative.parts):
        return False
    return not any(part in EXCLUDED_PARTS for part in relative.parts)


def tracked_executable_paths() -> set[str]:
    try:
        output = subprocess.check_output(
            ['git', 'ls-files', '-s'],
            cwd=REPO_ROOT,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
    except (OSError, subprocess.CalledProcessError):
        return set()
    executable_paths = set()
    for line in output.splitlines():
        fields = line.split(maxsplit=3)
        if len(fields) == 4 and fields[0] == '100755':
            executable_paths.add(fields[3])
    return executable_paths


def archive_mode(relative: Path, source: Path, executable_paths: set[str]) -> int:
    if relative.as_posix() in executable_paths or source.stat().st_mode & 0o111:
        return 0o755
    return 0o644


def build_archive(output: Path, release: str) -> None:
    frontend_index = REPO_ROOT / "frontend" / "dist" / "index.html"
    if not frontend_index.is_file():
        raise SystemExit(
            "frontend/dist/index.html is required; run the frontend production build before packaging"
        )

    output = output.expanduser().resolve()
    if output == REPO_ROOT or REPO_ROOT in output.parents:
        raise SystemExit(f"refusing to write release archive inside repository: {output}")
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing archive: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    archive_root = f"testhub_platform-{release}"
    executable_paths = tracked_executable_paths()
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(REPO_ROOT.rglob("*")):
            if not path.is_file() or not should_include(path):
                continue
            relative = path.relative_to(REPO_ROOT)
            info = archive.gettarinfo(str(path), arcname=Path(archive_root) / relative)
            info.mode = archive_mode(relative, path, executable_paths)
            with path.open("rb") as handle:
                archive.addfile(info, handle)

    print(f"archive={output}")
    print(f"release={release}")
    print(f"sha256={sha256(output)}")


def main() -> int:
    args = parse_args()
    build_archive(args.output, args.release or git_release())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
