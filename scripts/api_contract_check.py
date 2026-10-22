#!/usr/bin/env python3
"""Detect backward-incompatible OpenAPI changes for release gates."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"OpenAPI document must be an object: {path}")
    return value


def _schema_type(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return None
    if "$ref" in schema:
        return schema["$ref"]
    return schema.get("type")


def _required_properties(schema: Any) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    return {str(item) for item in schema.get("required", [])}


def _response_codes(operation: Any) -> set[str]:
    if not isinstance(operation, dict):
        return set()
    responses = operation.get("responses") or {}
    return {str(code) for code in responses if str(code) != "default"}


def _request_body_required(operation: Any) -> bool:
    if not isinstance(operation, dict):
        return False
    body = operation.get("requestBody") or {}
    return bool(body.get("required"))


def _operation_parameters(operation: Any) -> dict[tuple[str, str], bool]:
    if not isinstance(operation, dict):
        return {}
    result: dict[tuple[str, str], bool] = {}
    for parameter in operation.get("parameters") or []:
        if not isinstance(parameter, dict):
            continue
        key = (str(parameter.get("in", "")), str(parameter.get("name", "")))
        result[key] = bool(parameter.get("required"))
    return result


def _component_breaks(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    breaks: list[str] = []
    old_components = (previous.get("components") or {}).get("schemas") or {}
    new_components = (current.get("components") or {}).get("schemas") or {}
    for name, old_schema in old_components.items():
        if name not in new_components:
            breaks.append(f"component schema removed: {name}")
            continue
        new_schema = new_components[name]
        old_type = _schema_type(old_schema)
        new_type = _schema_type(new_schema)
        if old_type and new_type and old_type != new_type:
            breaks.append(f"component schema type changed: {name} {old_type}->{new_type}")
        old_required = _required_properties(old_schema)
        new_required = _required_properties(new_schema)
        for field in sorted(new_required - old_required):
            breaks.append(f"component required field added: {name}.{field}")
        old_properties = (old_schema or {}).get("properties") or {}
        new_properties = (new_schema or {}).get("properties") or {}
        for field in sorted(set(old_properties) - set(new_properties)):
            breaks.append(f"component property removed: {name}.{field}")
        for field in sorted(set(old_properties) & set(new_properties)):
            old_field_type = _schema_type(old_properties[field])
            new_field_type = _schema_type(new_properties[field])
            if old_field_type and new_field_type and old_field_type != new_field_type:
                breaks.append(
                    f"component property type changed: {name}.{field} "
                    f"{old_field_type}->{new_field_type}"
                )
    return breaks


def find_breaking_changes(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Return stable, human-readable breaking-change descriptions."""

    breaks: list[str] = []
    old_paths = previous.get("paths") or {}
    new_paths = current.get("paths") or {}
    for path in sorted(set(old_paths) - set(new_paths)):
        breaks.append(f"path removed: {path}")
    for path in sorted(set(old_paths) & set(new_paths)):
        old_item = old_paths[path] or {}
        new_item = new_paths[path] or {}
        old_methods = {method for method in old_item if method in METHODS}
        new_methods = {method for method in new_item if method in METHODS}
        for method in sorted(old_methods - new_methods):
            breaks.append(f"operation removed: {method.upper()} {path}")
        for method in sorted(old_methods & new_methods):
            old_operation = old_item[method]
            new_operation = new_item[method]
            label = f"{method.upper()} {path}"
            old_params = _operation_parameters(old_operation)
            new_params = _operation_parameters(new_operation)
            for parameter, required in sorted(new_params.items()):
                if required and not old_params.get(parameter, False):
                    breaks.append(f"required parameter added: {label} {parameter[0]}:{parameter[1]}")
            if _request_body_required(new_operation) and not _request_body_required(old_operation):
                breaks.append(f"request body became required: {label}")
            removed_codes = _response_codes(old_operation) - _response_codes(new_operation)
            for code in sorted(removed_codes):
                breaks.append(f"response status removed: {label} {code}")
    breaks.extend(_component_breaks(previous, current))
    return sorted(set(breaks))


def _generate_schema(path: Path) -> int:
    command = [
        sys.executable,
        "manage.py",
        "spectacular",
        "--file",
        str(path),
    ]
    result = subprocess.run(command, cwd=ROOT, text=True)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--current", type=Path)
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="testhub-openapi-contract-") as temp_dir:
        current = args.current
        if current is None:
            current = Path(temp_dir) / "current.yml"
            if _generate_schema(current):
                return 1
        if args.write_baseline:
            args.baseline.parent.mkdir(parents=True, exist_ok=True)
            args.baseline.write_text(current.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"OpenAPI baseline written: {args.baseline}")
            return 0
        try:
            breaks = find_breaking_changes(_load(args.baseline), _load(current))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            print(f"OpenAPI contract check failed: {exc}", file=sys.stderr)
            return 1
        if breaks:
            print("OpenAPI breaking changes detected:")
            for item in breaks:
                print(f"- {item}")
            return 1
        print("OpenAPI contract check passed: no breaking changes detected.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
