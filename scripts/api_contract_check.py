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
from urllib.parse import unquote

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


def _operation_parameters(operation: Any, inherited: Any = None) -> dict[tuple[str, str], bool]:
    result: dict[tuple[str, str], bool] = {}
    for parameter in (inherited or []) + ((operation or {}).get("parameters") or []):
        if not isinstance(parameter, dict):
            continue
        key = (str(parameter.get("in", "")), str(parameter.get("name", "")))
        result[key] = bool(parameter.get("required"))
    return result


def _resolve_ref(schema: Any, document: dict[str, Any] | None) -> Any:
    if not isinstance(schema, dict) or not isinstance(document, dict):
        return schema
    current = schema
    seen_refs: set[str] = set()
    while isinstance(current, dict):
        reference = current.get("$ref")
        if not isinstance(reference, str) or not reference.startswith("#/"):
            return current
        if reference in seen_refs:
            return current
        seen_refs.add(reference)
        value: Any = document
        try:
            fragment = unquote(reference[2:])
            for part in fragment.split("/"):
                value = value[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            return current
        if not isinstance(value, dict):
            return current
        current = value
    return current


def _compare_schema(
    old_schema: Any,
    new_schema: Any,
    label: str,
    breaks: list[str],
    seen: set[tuple[int, int]],
    old_document: dict[str, Any] | None = None,
    new_document: dict[str, Any] | None = None,
) -> None:
    old_schema = _resolve_ref(old_schema, old_document)
    new_schema = _resolve_ref(new_schema, new_document)
    if not isinstance(old_schema, dict) or not isinstance(new_schema, dict):
        return
    pair = (id(old_schema), id(new_schema))
    if pair in seen:
        return
    seen.add(pair)
    old_type = _schema_type(old_schema)
    new_type = _schema_type(new_schema)
    if old_type and new_type and old_type != new_type:
        breaks.append(f"schema type changed: {label} {old_type}->{new_type}")
    old_properties = old_schema.get("properties") or {}
    new_properties = new_schema.get("properties") or {}
    old_required = _required_properties(old_schema)
    new_required = _required_properties(new_schema)
    for field in sorted(new_required - old_required):
        new_property = new_properties.get(field)
        if isinstance(new_property, dict) and new_property.get('readOnly') is True:
            continue
        breaks.append(f"required field added: {label}.{field}")
    for field in sorted(set(old_properties) - set(new_properties)):
        breaks.append(f"property removed: {label}.{field}")
    for field in sorted(set(old_properties) & set(new_properties)):
        _compare_schema(old_properties[field], new_properties[field], f"{label}.{field}", breaks, seen, old_document, new_document)
    old_items = old_schema.get("items")
    new_items = new_schema.get("items")
    if old_items is not None and new_items is not None:
        _compare_schema(old_items, new_items, f"{label}[]", breaks, seen, old_document, new_document)
    old_enum = {
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")): value
        for value in (old_schema.get("enum") or [])
    }
    new_enum = {
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")): value
        for value in (new_schema.get("enum") or [])
    }
    for key in sorted(set(old_enum) - set(new_enum)):
        breaks.append(f"enum value removed: {label}={old_enum[key]}")
    if old_schema.get("additionalProperties") is not False and new_schema.get("additionalProperties") is False:
        breaks.append(f"additional properties disallowed: {label}")
    for keyword in ("allOf", "anyOf", "oneOf"):
        old_parts = old_schema.get(keyword) or []
        new_parts = new_schema.get(keyword) or []
        for index, old_part in enumerate(old_parts):
            if index < len(new_parts):
                _compare_schema(old_part, new_parts[index], f"{label}.{keyword}[{index}]", breaks, seen, old_document, new_document)


def _operation_parameter_details(operation: Any, inherited: Any = None, document: dict[str, Any] | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for parameter in (inherited or []) + ((operation or {}).get("parameters") or []):
        parameter = _resolve_ref(parameter, document)
        if not isinstance(parameter, dict):
            continue
        key = (str(parameter.get("in", "")), str(parameter.get("name", "")))
        result[key] = parameter
    return result


def _content_schemas(content: Any) -> dict[str, Any]:
    if not isinstance(content, dict):
        return {}
    return {
        str(media_type): media.get('schema')
        for media_type, media in content.items()
        if isinstance(media, dict) and media.get('schema') is not None
    }


def _compare_content_schemas(
    old_content: Any,
    new_content: Any,
    label: str,
    breaks: list[str],
    old_document: dict[str, Any],
    new_document: dict[str, Any],
) -> None:
    old_schemas = _content_schemas(old_content)
    new_schemas = _content_schemas(new_content)
    for media_type, old_schema in sorted(old_schemas.items()):
        if media_type not in new_schemas:
            breaks.append(f"media type removed: {label} {media_type}")
            continue
        _compare_schema(
            old_schema,
            new_schemas[media_type],
            f"{label} {media_type}",
            breaks,
            set(),
            old_document,
            new_document,
        )


def _parameter_schema(parameter: dict[str, Any]) -> Any:
    if parameter.get('schema') is not None:
        return parameter['schema']
    schemas = _content_schemas(parameter.get('content'))
    return next(iter(schemas.values()), None)


def _component_breaks(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    breaks: list[str] = []
    old_components = (previous.get("components") or {}).get("schemas") or {}
    new_components = (current.get("components") or {}).get("schemas") or {}
    for name, old_schema in old_components.items():
        if name not in new_components:
            breaks.append(f"component schema removed: {name}")
            continue
        _compare_schema(old_schema, new_components[name], f"component {name}", breaks, set(), previous, current)
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
            old_parameters = _operation_parameter_details(old_operation, old_item.get("parameters"), previous)
            new_parameters = _operation_parameter_details(new_operation, new_item.get("parameters"), current)
            old_params = {key: bool(value.get('required')) for key, value in old_parameters.items()}
            new_params = {key: bool(value.get('required')) for key, value in new_parameters.items()}
            for parameter, required in sorted(new_params.items()):
                if required and not old_params.get(parameter, False):
                    breaks.append(f"required parameter added: {label} {parameter[0]}:{parameter[1]}")
            for parameter in sorted(set(old_parameters) & set(new_parameters)):
                old_schema = _parameter_schema(old_parameters[parameter])
                new_schema = _parameter_schema(new_parameters[parameter])
                if old_schema is not None and new_schema is not None:
                    _compare_schema(
                        old_schema,
                        new_schema,
                        f"{label} parameter {parameter[0]}:{parameter[1]}",
                        breaks,
                        set(),
                        previous,
                        current,
                    )
            old_body = _resolve_ref(old_operation.get("requestBody") or {}, previous)
            new_body = _resolve_ref(new_operation.get("requestBody") or {}, current)
            if bool(new_body.get('required')) and not bool(old_body.get('required')):
                breaks.append(f"request body became required: {label}")
            _compare_content_schemas(
                old_body.get('content'),
                new_body.get('content'),
                f"{label} request body",
                breaks,
                previous,
                current,
            )
            old_responses = old_operation.get('responses') or {}
            new_responses = new_operation.get('responses') or {}
            removed_codes = _response_codes(old_operation) - _response_codes(new_operation)
            for code in sorted(removed_codes):
                breaks.append(f"response status removed: {label} {code}")
            for code in sorted(set(old_responses) & set(new_responses)):
                old_response = _resolve_ref(old_responses[code], previous)
                new_response = _resolve_ref(new_responses[code], current)
                _compare_content_schemas(
                    old_response.get('content') if isinstance(old_response, dict) else None,
                    new_response.get('content') if isinstance(new_response, dict) else None,
                    f"{label} response {code}",
                    breaks,
                    previous,
                    current,
                )
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
