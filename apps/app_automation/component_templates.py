"""Safe validation and expansion for parameterized APP component templates.

The caller owns authorization and data access. ``resolve_template`` is called
only with the requested project, type, and version and must return a definition
scoped to that project. Definitions are JSON data; step contents are never
interpreted as Python or otherwise executed here.

A resolver-backed definition has this shape::

    {
        "project_id": "project-1", "type": "login", "version": "2",
        "parameters": {
            "username": {"type": "string", "required": True},
            "wait": {"type": "number", "default": 0}
        },
        "steps": [{"type": "input", "config": {"text": "{{params.username}}"}}]
    }

``schema.properties``/``schema.required`` and ``default_config`` are also
accepted for compatibility with existing AppCustomComponent definitions.
Custom references use ``kind``, ``type``, ``version``, and ``params`` (``config``
is accepted only when ``params`` is absent). A custom step with an inline
``steps`` list is expanded without calling the resolver.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from typing import Any


DEFAULT_MAX_DEPTH = 16
DEFAULT_MAX_STEPS = 1000
DEFAULT_MAX_PAYLOAD_BYTES = 1_048_576

_STEP_LIST_FIELDS = (
    "steps",
    "then_steps",
    "else_steps",
    "try_steps",
    "catch_steps",
    "finally_steps",
)
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOKEN_RE = re.compile(r"\{\{([^{}]*)\}\}")
_PARAM_RE = re.compile(r"^params\.([A-Za-z_][A-Za-z0-9_]*)$")
_EXACT_PARAM_RE = re.compile(r"^\{\{\s*params\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")
_EXACT_SCENE_VARIABLE_RE = re.compile(
    r"(?:\{\{\s*(?:local|global)\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*\s*\}\}"
    r"|\$\{\s*(?:local|global)\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*\s*\})\Z"
)
_DOLLAR_TOKEN_RE = re.compile(r"\$\{([^{}]*)\}")


class ComponentTemplateError(ValueError):
    """Base error for invalid component data or expansion failures."""


class StepValidationError(ComponentTemplateError):
    """Raised when a step tree is not valid JSON step data."""


class MissingParameterError(ComponentTemplateError):
    """Raised when a required or referenced template parameter is absent."""


class TemplateResolutionError(ComponentTemplateError):
    """Raised when a template cannot be resolved within the requested project."""


class ExpansionLimitError(ComponentTemplateError):
    """Raised when expansion exceeds a configured recursion, step, or size limit."""


TemplateResolver = Callable[[str | int, str, str], Mapping[str, Any] | None]


def _json_value(value: Any, path: str, active: set[int] | None = None) -> None:
    """Accept only finite, acyclic JSON values, without invoking custom encoders."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise StepValidationError(f"{path} contains a non-finite number")
        return
    if type(value) not in (dict, list):
        raise StepValidationError(f"{path} contains a non-JSON value")

    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        raise StepValidationError(f"{path} contains a cyclic value")
    active.add(identity)
    try:
        if isinstance(value, dict):
            for key, item in value.items():
                if type(key) is not str:
                    raise StepValidationError(f"{path} contains a non-string object key")
                _json_value(item, f"{path}.{key}", active)
        else:
            for index, item in enumerate(value):
                _json_value(item, f"{path}[{index}]", active)
    finally:
        active.remove(identity)


def _encoded_size(value: Any, path: str) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise StepValidationError(f"{path} cannot be encoded as JSON") from None


def _check_payload(value: Any, path: str, limit: int) -> int:
    try:
        _json_value(value, path)
    except RecursionError:
        raise StepValidationError(f"{path} is nested too deeply") from None
    size = _encoded_size(value, path)
    if size > limit:
        raise ExpansionLimitError(f"{path} exceeds the {limit}-byte payload limit")
    return size


def validate_steps(steps: Any, *, path: str = "steps") -> None:
    """Validate an existing list of step dictionaries without changing it.

    Step types are intentionally not allowlisted here: the runner owns the
    action catalog. Known nested step-list fields on a step or its config are
    validated recursively.
    """
    try:
        _json_value(steps, path)
    except RecursionError:
        raise StepValidationError(f"{path} is nested too deeply") from None
    if type(steps) is not list:
        raise StepValidationError(f"{path} must be a list")

    def visit(step_list: list[Any], list_path: str) -> None:
        for index, step in enumerate(step_list):
            step_path = f"{list_path}[{index}]"
            if type(step) is not dict:
                raise StepValidationError(f"{step_path} must be an object")
            step_type = step.get("type")
            if not isinstance(step_type, str) or not step_type.strip():
                raise StepValidationError(f"{step_path}.type must be a non-empty string")

            is_custom = step.get("kind") == "custom"
            inline_steps = step.get("steps")
            if is_custom:
                for field in ("params", "config"):
                    if field in step and not isinstance(step[field], dict):
                        raise StepValidationError(f"{step_path}.{field} must be an object")
            if is_custom and inline_steps is not None and not isinstance(inline_steps, list):
                raise StepValidationError(f"{step_path}.steps must be a list when provided")
            if is_custom and inline_steps is None:
                version = step.get("version")
                if not isinstance(version, str) or not version.strip():
                    raise StepValidationError(f"{step_path}.version is required for a template reference")

            containers = [(step, step_path)]
            config = step.get("config")
            if not is_custom and isinstance(config, dict):
                containers.append((config, f"{step_path}.config"))
            for container, container_path in containers:
                for field in _STEP_LIST_FIELDS:
                    if field not in container or container[field] is None:
                        continue
                    nested = container[field]
                    if not isinstance(nested, list):
                        raise StepValidationError(f"{container_path}.{field} must be a list")
                    visit(nested, f"{container_path}.{field}")

    try:
        visit(steps, path)
    except RecursionError:
        raise StepValidationError(f"{path} is nested too deeply") from None


def _copy_json(value: Any) -> Any:
    """Copy already-validated JSON data using only built-in containers."""
    if type(value) is dict:
        return {key: _copy_json(item) for key, item in value.items()}
    if type(value) is list:
        return [_copy_json(item) for item in value]
    return value


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return type(value) is str
    if expected == "boolean":
        return type(value) is bool
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) in (int, float)
    if expected == "object":
        return type(value) is dict
    if expected == "array":
        return type(value) is list
    if expected == "null":
        return value is None
    raise TemplateResolutionError("template declares an unsupported parameter type")


def _required_names(values: Any, label: str) -> set[str]:
    if type(values) is not list or any(type(name) is not str for name in values):
        raise TemplateResolutionError(f"{label} must be a list of parameter names")
    return set(values)


def _parameter_contract(
    definition: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, Any], bool]:
    raw_parameters = definition.get("parameters")
    has_parameters = "parameters" in definition
    schema = definition.get("schema", {})
    if not isinstance(schema, dict):
        raise TemplateResolutionError("template schema must be an object")

    strict = has_parameters
    required: set[str] = set()
    if has_parameters:
        if not isinstance(raw_parameters, dict):
            raise TemplateResolutionError("template parameters must be an object")
        if "properties" in raw_parameters:
            properties = raw_parameters.get("properties")
            if not isinstance(properties, dict):
                raise TemplateResolutionError("template parameter properties must be an object")
            specs = properties
            required.update(_required_names(raw_parameters.get("required", []), "template required"))
        else:
            specs = raw_parameters
    else:
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise TemplateResolutionError("template schema properties must be an object")
        specs = properties
        strict = "properties" in schema
        required.update(_required_names(schema.get("required", []), "template schema required"))

    normalized: dict[str, dict[str, Any]] = {}
    defaults: dict[str, Any] = {}
    for name, raw_spec in specs.items():
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise TemplateResolutionError("template parameter names must be simple identifiers")
        if isinstance(raw_spec, str):
            spec = {"type": raw_spec}
        elif isinstance(raw_spec, dict):
            spec = raw_spec
        else:
            raise TemplateResolutionError(f"parameter '{name}' declaration must be an object")
        normalized[name] = spec
        if spec.get("required") is True:
            required.add(name)
        if "default" in spec:
            defaults[name] = _copy_json(spec["default"])

    if any(not isinstance(name, str) or name not in normalized for name in required):
        raise TemplateResolutionError("template required parameters must be declared")

    configured_defaults = definition.get("default_config", {})
    if not isinstance(configured_defaults, dict):
        raise TemplateResolutionError("template default_config must be an object")
    if strict and any(name not in normalized for name in configured_defaults):
        raise TemplateResolutionError("template default_config contains an undeclared parameter")
    defaults.update(_copy_json(configured_defaults))

    for name, value in defaults.items():
        _validate_parameter(name, value, normalized.get(name, {}))
    return normalized, required, defaults, strict


def _validate_parameter(name: str, value: Any, spec: dict[str, Any]) -> None:
    if isinstance(value, str):
        tokens = [match.group(1).strip() for match in _TOKEN_RE.finditer(value)]
        tokens.extend(match.group(1).strip() for match in _DOLLAR_TOKEN_RE.finditer(value))
        if any(token == "params" or token.startswith("params.") for token in tokens):
            raise ComponentTemplateError(f"parameter '{name}' contains an unresolved template parameter reference")

    expected = spec.get("type")
    is_scene_variable_reference = (
        isinstance(value, str) and _EXACT_SCENE_VARIABLE_RE.fullmatch(value) is not None
    )
    if expected is not None:
        expected_types = expected if isinstance(expected, list) else [expected]
        if not all(isinstance(item, str) for item in expected_types):
            raise TemplateResolutionError(f"parameter '{name}' has an invalid type declaration")
        if not is_scene_variable_reference and not any(_type_matches(value, item) for item in expected_types):
            raise ComponentTemplateError(f"parameter '{name}' must match its declared type")
    if "enum" in spec:
        choices = spec["enum"]
        if not isinstance(choices, list):
            raise TemplateResolutionError(f"parameter '{name}' enum must be a list")
        if not is_scene_variable_reference and value not in choices:
            raise ComponentTemplateError(f"parameter '{name}' must match a declared choice")


def _string_value(value: Any, name: str) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    raise ComponentTemplateError(f"parameter '{name}' must be scalar for string interpolation")


def _substitute(value: Any, params: dict[str, Any], path: str) -> Any:
    if isinstance(value, str):
        exact = _EXACT_PARAM_RE.fullmatch(value)
        if exact:
            name = exact.group(1)
            if name not in params:
                raise MissingParameterError(f"missing parameter '{name}' at {path}")
            return _copy_json(params[name])

        def replace(match: re.Match[str]) -> str:
            expression = match.group(1).strip()
            if not expression.startswith("params"):
                return match.group(0)
            parsed = _PARAM_RE.fullmatch(expression)
            if not parsed:
                raise ComponentTemplateError(f"invalid parameter placeholder at {path}")
            name = parsed.group(1)
            if name not in params:
                raise MissingParameterError(f"missing parameter '{name}' at {path}")
            return _string_value(params[name], name)

        return _TOKEN_RE.sub(replace, value)
    if isinstance(value, dict):
        return {key: _substitute(item, params, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, params, f"{path}[{index}]") for index, item in enumerate(value)]
    return value


def expand_steps(
    steps: Any,
    *,
    project_id: str | int,
    resolve_template: TemplateResolver,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> list[dict[str, Any]]:
    """Validate and expand steps into fresh runner-ready dictionaries.

    ``resolve_template(project_id, type, version)`` must perform an authorized,
    project-scoped lookup and return a JSON definition containing matching
    ``project_id``, ``type``, ``version``, and a ``steps`` list. It may return
    ``None`` when no authorized definition exists. The expander independently
    rejects returned definitions whose identity does not match the request.
    Custom wrappers are spliced out; nested control-flow step lists stay nested.

    Exact ``{{params.name}}`` values retain their JSON type. The same syntax
    embedded in a string interpolates scalar values as text. Other runner
    placeholders, such as ``{{local.value}}``, remain untouched.

    ``max_payload_bytes`` bounds the cumulative JSON source data (root steps and
    every resolved definition) plus the emitted expansion.
    """
    if type(project_id) not in (str, int) or project_id == "":
        raise ValueError("project_id must be a non-empty string or integer")
    if not callable(resolve_template):
        raise TypeError("resolve_template must be callable")
    for label, value, allow_zero in (
        ("max_depth", max_depth, True),
        ("max_steps", max_steps, False),
        ("max_payload_bytes", max_payload_bytes, False),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
            raise ValueError(f"{label} must be a {'non-negative' if allow_zero else 'positive'} integer")

    validate_steps(steps)
    source_size = _check_payload(steps, "steps", max_payload_bytes)
    budget = {"steps": 0, "source_bytes": source_size}

    def append_checked(target: list[dict[str, Any]], step: dict[str, Any], path: str,
                       current_size: int) -> int:
        # Count each appended value once; re-encoding every growing prefix is
        # quadratic for an otherwise bounded, valid component.
        next_size = current_size + _encoded_size(step, path) + (1 if target else 0)
        if next_size > max_payload_bytes:
            raise ExpansionLimitError(f"expanded payload exceeds the {max_payload_bytes}-byte limit")
        target.append(step)
        return next_size

    def expand_sequence(
        source: list[dict[str, Any]],
        params: dict[str, Any],
        active: tuple[tuple[str, str], ...],
        depth: int,
        path: str,
    ) -> list[dict[str, Any]]:
        if depth > max_depth:
            raise ExpansionLimitError(f"component expansion exceeds the depth limit of {max_depth}")
        expanded: list[dict[str, Any]] = []
        expanded_size = 2  # Opening and closing list brackets.
        for index, original in enumerate(source):
            step_path = f"{path}[{index}]"
            if original.get("kind") == "custom":
                inline_steps = original.get("steps")
                if isinstance(inline_steps, list):
                    container = {}
                    for key, value in original.items():
                        if key == "steps":
                            container[key] = _copy_json(value)
                        else:
                            container[key] = _substitute(value, params, f"{step_path}.{key}")
                    raw_params = container.get("params", container.get("config", {}))
                    if raw_params is None:
                        raw_params = {}
                    if not isinstance(raw_params, dict):
                        raise StepValidationError(f"{step_path}.params must be an object")
                    inline_params = dict(params)
                    inline_params.update(raw_params)
                    children = expand_sequence(
                        inline_steps, inline_params, active, depth + 1, f"{step_path}.steps"
                    )
                    for child in children:
                        expanded_size = append_checked(expanded, child, path, expanded_size)
                    continue

                reference = {
                    key: _substitute(value, params, f"{step_path}.{key}")
                    for key, value in original.items()
                    if key != "steps"
                }
                component_type = reference.get("type")
                version = reference.get("version")
                if not isinstance(component_type, str) or not component_type:
                    raise StepValidationError(f"{step_path}.type must be a non-empty string")
                if not isinstance(version, str) or not version:
                    raise StepValidationError(f"{step_path}.version is required for a template reference")
                identity = (component_type, version)
                if identity in active:
                    chain = active + (identity,)
                    display = " -> ".join(f"{kind}@{revision}" for kind, revision in chain)
                    raise TemplateResolutionError(f"component template cycle detected: {display}")
                try:
                    resolved = resolve_template(project_id, component_type, version)
                except Exception:
                    raise TemplateResolutionError("template resolver failed") from None
                if resolved is None:
                    raise TemplateResolutionError("template was not found in the authorized project scope")
                if not isinstance(resolved, Mapping):
                    raise TemplateResolutionError("template resolver must return a definition object")
                definition = dict(resolved)
                definition_size = _check_payload(definition, "template definition", max_payload_bytes)
                budget["source_bytes"] += definition_size
                if budget["source_bytes"] > max_payload_bytes:
                    raise ExpansionLimitError(
                        f"cumulative template payload exceeds the {max_payload_bytes}-byte limit"
                    )
                if (
                    type(definition.get("project_id")) is not type(project_id)
                    or definition.get("project_id") != project_id
                    or definition.get("type") != component_type
                    or definition.get("version") != version
                ):
                    raise TemplateResolutionError(
                        "resolved template identity does not match the authorized project scope"
                    )
                template_steps = definition.get("steps")
                validate_steps(template_steps, path=f"template {component_type}@{version}.steps")

                specs, required, defaults, strict = _parameter_contract(definition)
                supplied = reference.get("params", reference.get("config", {}))
                if supplied is None:
                    supplied = {}
                if not isinstance(supplied, dict):
                    raise StepValidationError(f"{step_path}.params must be an object")
                if strict:
                    extra = set(supplied) - set(specs)
                    if extra:
                        name = sorted(extra)[0]
                        raise ComponentTemplateError(f"parameter '{name}' is not declared by the template")
                resolved_params = _copy_json(defaults)
                resolved_params.update(_copy_json(supplied))
                for name in required:
                    if name not in resolved_params:
                        raise MissingParameterError(
                            f"required parameter '{name}' is missing for {component_type}@{version}"
                        )
                for name, value in resolved_params.items():
                    _validate_parameter(name, value, specs.get(name, {}))

                children = expand_sequence(
                    template_steps,
                    resolved_params,
                    active + (identity,),
                    depth + 1,
                    f"template {component_type}@{version}.steps",
                )
                for child in children:
                    expanded_size = append_checked(expanded, child, path, expanded_size)
                continue

            concrete = {}
            for key, value in original.items():
                if key == "config" and isinstance(value, dict):
                    config = {}
                    for config_key, config_value in value.items():
                        if config_key in _STEP_LIST_FIELDS and isinstance(config_value, list):
                            config[config_key] = expand_sequence(
                                config_value,
                                params,
                                active,
                                depth + 1,
                                f"{step_path}.config.{config_key}",
                            )
                        else:
                            config[config_key] = _substitute(
                                config_value, params, f"{step_path}.config.{config_key}"
                            )
                    concrete[key] = config
                elif key in _STEP_LIST_FIELDS and isinstance(value, list):
                    concrete[key] = expand_sequence(
                        value, params, active, depth + 1, f"{step_path}.{key}"
                    )
                else:
                    concrete[key] = _substitute(value, params, f"{step_path}.{key}")
            budget["steps"] += 1
            if budget["steps"] > max_steps:
                raise ExpansionLimitError(f"expanded step count exceeds the limit of {max_steps}")
            expanded_size = append_checked(expanded, concrete, path, expanded_size)
        return expanded

    try:
        result = expand_sequence(steps, {}, (), 0, "steps")
        result_size = _check_payload(result, "expanded steps", max_payload_bytes)
        if budget["source_bytes"] + result_size > max_payload_bytes:
            raise ExpansionLimitError(
                f"cumulative template payload exceeds the {max_payload_bytes}-byte limit"
            )
    except RecursionError:
        raise ExpansionLimitError("component expansion exceeds the safe recursion limit") from None
    return result


__all__ = [
    "ComponentTemplateError",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_MAX_STEPS",
    "ExpansionLimitError",
    "MissingParameterError",
    "StepValidationError",
    "TemplateResolutionError",
    "TemplateResolver",
    "expand_steps",
    "validate_steps",
]
