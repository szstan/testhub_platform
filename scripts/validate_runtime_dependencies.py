#!/usr/bin/env python3
"""Verify that an interpreter matches the pinned project requirements."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def requirements(paths: list[Path]) -> list[Requirement]:
    parsed: list[Requirement] = []
    for path in paths:
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw_line.split(" #", 1)[0].strip()
            if not line or line.startswith("#") or line.startswith(("-r ", "--")):
                continue
            try:
                requirement = Requirement(line)
            except Exception as exc:  # noqa: BLE001 - identify the exact lock line
                raise ValueError(f"invalid requirement {path}:{line_number}: {raw_line!r}") from exc
            if requirement.marker is None or requirement.marker.evaluate():
                parsed.append(requirement)
    return parsed


def validate(paths: list[Path]) -> int:
    seen: dict[str, str] = {}
    failures: list[str] = []
    checked = 0
    for requirement in requirements(paths):
        name = canonicalize_name(requirement.name)
        try:
            installed = version(requirement.name)
        except PackageNotFoundError:
            failures.append(f"{requirement.name}: missing (required {requirement.specifier or 'any version'})")
            continue
        previous = seen.get(name)
        if previous is not None and previous != installed:
            failures.append(f"{requirement.name}: conflicting installed versions {previous} and {installed}")
            continue
        seen[name] = installed
        checked += 1
        if requirement.specifier and installed not in requirement.specifier:
            failures.append(f"{requirement.name}: installed {installed}, required {requirement.specifier}")
    if failures:
        raise SystemExit("runtime dependency validation failed:\n" + "\n".join(f"- {item}" for item in failures))
    return checked


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", nargs="+", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    missing_files = [str(path) for path in args.requirements if not path.is_file()]
    if missing_files:
        raise SystemExit("missing requirements file(s): " + ", ".join(missing_files))
    checked = validate(args.requirements)
    print(f"runtime dependencies valid: {checked} requirements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
