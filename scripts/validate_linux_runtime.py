#!/usr/bin/env python3
"""Validate an installed Linux runtime without network access or mutation."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "web": {
        "imports": ("django", "daphne", "celery", "channels", "channels_redis", "rest_framework"),
        "executables": (),
    },
    "automation": {
        "imports": (
            "django",
            "daphne",
            "celery",
            "channels",
            "channels_redis",
            "rest_framework",
            "airtest.core.api",
            "allure",
            "allure_pytest",
            "cv2",
            "easyocr",
            "numpy",
            "torch",
            "torchvision",
        ),
        "executables": ("adb", "allure"),
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        required=True,
        help="Runtime boundary to validate",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root containing the Allure CLI",
    )
    parser.add_argument(
        "--mirror",
        type=Path,
        help="Optional immutable mirror; only its manifest hash is checked",
    )
    parser.add_argument(
        "--adb",
        type=Path,
        help="ADB executable; defaults to APP_ADB_PATH or PATH lookup",
    )
    parser.add_argument(
        "--allure",
        type=Path,
        help="Allure executable; defaults to <repo-root>/allure/bin/allure",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable result",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_result(command: list[str], *, timeout: int = 60) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={
                **os.environ,
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INDEX": "1",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "command": command,
            "error": f"{type(exc).__name__}: {exc}",
        }
    output = "\n".join(
        line.strip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    )
    return {
        "ok": completed.returncode == 0,
        "command": command,
        "returncode": completed.returncode,
        "output": output[:1000],
    }


def validate_platform() -> dict[str, Any]:
    version = platform.python_version()
    machine = platform.machine()
    checks = {
        "system": platform.system(),
        "python": version,
        "machine": machine,
        "ok": platform.system() == "Linux"
        and sys.version_info[:2] == (3, 12)
        and machine.lower() in {"x86_64", "amd64"},
    }
    if not checks["ok"]:
        checks["error"] = "Linux x86_64 with Python 3.12 is required"
    return checks


def validate_mirror(mirror: Path) -> dict[str, Any]:
    mirror = mirror.expanduser().resolve()
    manifest = mirror / "manifest.json"
    manifest_sha = mirror / "manifest.sha256"
    sums = mirror / "SHA256SUMS"
    result: dict[str, Any] = {"path": str(mirror), "ok": False}
    if not mirror.is_dir():
        result["error"] = "mirror directory does not exist"
        return result
    if not manifest.is_file() or not manifest_sha.is_file() or not sums.is_file():
        result["error"] = "manifest.json, manifest.sha256, and SHA256SUMS are required"
        return result
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("manifest.json must contain an object")
        expected = manifest_sha.read_text(encoding="ascii").split()[0]
        actual = sha256_file(manifest)
        sum_entries = {}
        for line in sums.read_text(encoding='ascii').splitlines():
            if not line.strip():
                continue
            digest, filename = line.split(None, 1)
            sum_entries[filename.strip().lstrip('*')] = digest
        artifacts = metadata.get('artifacts')
        if not isinstance(artifacts, list):
            raise ValueError('manifest.json artifacts must be a list')
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ValueError('manifest.json artifacts must contain objects')
            filename = str(artifact.get('filename') or '')
            path = (mirror / filename).resolve()
            if not filename or mirror not in path.parents or not path.is_file():
                raise ValueError(f'missing or unsafe artifact: {filename}')
            expected_hash = str(artifact.get('sha256') or '')
            expected_size = int(artifact.get('size') or -1)
            if path.stat().st_size != expected_size or sha256_file(path) != expected_hash:
                raise ValueError(f'artifact hash/size mismatch: {filename}')
            if sum_entries.get(filename) != expected_hash:
                raise ValueError(f'SHA256SUMS mismatch: {filename}')
    except (OSError, ValueError, IndexError, TypeError) as exc:
        result["error"] = f"invalid mirror metadata: {exc}"
        return result
    result.update(
        {
            "manifest_sha256": actual,
            "expected_manifest_sha256": expected,
            "artifact_count": metadata.get("artifact_count"),
            "ok": expected == actual and isinstance(metadata.get("artifacts"), list),
        }
    )
    if not result["ok"]:
        result["error"] = "manifest.sha256 does not match manifest.json"
    return result


def validate_imports(module_names: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "modules": {}}
    for name in module_names:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # Import failures are reported without a traceback.
            result["ok"] = False
            result["modules"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            result["modules"][name] = {
                "ok": True,
                "version": getattr(module, "__version__", "unknown"),
            }
    return result


def validate_executable(name: str, path: Path | None, default: Path | None = None) -> dict[str, Any]:
    if path is None:
        path_value = os.environ.get("APP_ADB_PATH") if name == "adb" else None
        path = Path(path_value) if path_value else default
    if path is None:
        resolved = shutil.which(name)
        path = Path(resolved) if resolved else None
    result: dict[str, Any] = {"name": name, "ok": False}
    if path is None:
        result["error"] = "executable was not found"
        return result
    path = path.expanduser().resolve()
    result["path"] = str(path)
    if not path.is_file() or not os.access(path, os.X_OK):
        result["error"] = "executable is missing or not executable"
        return result
    command = command_result([str(path), "--version"] if name == "allure" else [str(path), "version"])
    result.update(command)
    result["ok"] = command["ok"]
    return result


def main() -> int:
    args = parse_args()
    profile = PROFILES[args.profile]
    result: dict[str, Any] = {
        "schema_version": 1,
        "profile": args.profile,
        "python_executable": sys.executable,
        "platform": validate_platform(),
        "pip_check": command_result([sys.executable, "-m", "pip", "check"]),
        "imports": validate_imports(profile["imports"]),
    }
    if args.mirror:
        result["mirror"] = validate_mirror(args.mirror)
    if args.profile == "automation":
        result["executables"] = {
            "adb": validate_executable("adb", args.adb),
            "allure": validate_executable(
                "allure",
                args.allure,
                args.repo_root.expanduser().resolve() / "allure" / "bin" / "allure",
            ),
        }
    checks = [result["platform"], result["pip_check"], result["imports"]]
    if args.mirror:
        checks.append(result["mirror"])
    if args.profile == "automation":
        checks.extend(result["executables"].values())
    result["ok"] = all(check.get("ok", False) for check in checks)
    if args.json:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    else:
        print(f"profile={args.profile}")
        print(f"python={platform.python_version()}")
        print(f"platform={platform.system()} {platform.machine()}")
        print(f"pip_check={'passed' if result['pip_check']['ok'] else 'failed'}")
        print(f"imports={'passed' if result['imports']['ok'] else 'failed'}")
        if args.mirror:
            print(f"mirror={'passed' if result['mirror']['ok'] else 'failed'}")
        if args.profile == "automation":
            for name, check in result["executables"].items():
                print(f"{name}={'passed' if check['ok'] else 'failed'}")
        print(f"runtime_validation={'passed' if result['ok'] else 'failed'}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
