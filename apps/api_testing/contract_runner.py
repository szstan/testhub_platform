"""Bounded deterministic OpenAPI contract execution helper."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from itertools import islice
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

from apps.core.http_transport import guarded_request
from apps.core.network import UnsafeOutboundURL, validate_outbound_url
from apps.quality_intelligence.services.redaction import redact_payload

from .contracts import generate_contract_cases, parse_bounded_limit


MAX_CONTRACT_CASES = 10
MAX_CONTRACT_TIMEOUT_SECONDS = 5.0
CONTRACT_RUN_BUDGET_SECONDS = 15.0


def _example(schema: Any) -> Any:
    if not isinstance(schema, Mapping):
        return None
    if 'example' in schema:
        return schema['example']
    if 'default' in schema:
        return schema['default']
    schema_type = schema.get('type')
    if schema_type == 'object' or 'properties' in schema:
        properties = schema.get('properties') or {}
        return {str(key): _example(value) for key, value in list(properties.items())[:50]}
    if schema_type == 'array':
        return [_example(schema.get('items') or {})]
    if schema_type == 'integer' or schema_type == 'number':
        return 1
    if schema_type == 'boolean':
        return True
    return 'test'


def _path_with_examples(path: str, parameters: list[Any]) -> str:
    result = str(path)
    for parameter in parameters or []:
        if not isinstance(parameter, Mapping):
            continue
        if parameter.get('in') == 'path' and parameter.get('name'):
            result = result.replace('{' + str(parameter['name']) + '}', 'test')
    return result


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(str(url))
    host = (parsed.hostname or '').rstrip('.').lower()
    scheme = parsed.scheme.lower()
    try:
        port = parsed.port or (443 if scheme == 'https' else 80)
    except ValueError as exc:
        raise ValueError('OpenAPI case URL has an invalid port') from exc
    return scheme, host, port


def _case_url(
    base_url: str,
    case: Mapping[str, Any],
    *,
    allow_private: bool,
    validate_target: bool = True,
) -> str:
    path = _path_with_examples(case['path'], case.get('parameters', []))
    if (
        not path.startswith('/')
        or path.startswith('//')
        or '\\' in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise ValueError('OpenAPI operation path must be a local absolute path')
    path_parts = urlsplit(path)
    if path_parts.scheme or path_parts.netloc or path_parts.query or path_parts.fragment:
        raise ValueError('OpenAPI operation path cannot override the request authority or query')

    base_parts = urlsplit(base_url)
    if base_parts.query or base_parts.fragment:
        raise ValueError('OpenAPI server URL cannot contain a query or fragment')
    resolved_path = f"{base_parts.path.rstrip('/')}/{path_parts.path.lstrip('/')}"
    resolved = base_parts._replace(path=resolved_path).geturl()
    expected_origin = _origin(base_url)
    if _origin(resolved) != expected_origin:
        raise ValueError('OpenAPI case URL changed the configured server origin')
    if not validate_target:
        return resolved
    try:
        validated = validate_outbound_url(resolved, allow_private=allow_private)
    except UnsafeOutboundURL as exc:
        raise ValueError(str(exc)) from exc
    if _origin(validated) != expected_origin:
        raise ValueError('OpenAPI case URL changed the configured server origin')
    return validated


def _validated_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError('timeout must be a number between 0.1 and 5 seconds')
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('timeout must be a number between 0.1 and 5 seconds') from exc
    if not math.isfinite(parsed) or not 0.1 <= parsed <= MAX_CONTRACT_TIMEOUT_SECONDS:
        raise ValueError('timeout must be a number between 0.1 and 5 seconds')
    return parsed


def _response_is_expected(status_code: int, case: Mapping[str, Any]) -> bool:
    expected = set(str(key) for key in (case.get('responses') or {}).keys())
    if case.get('case_kind') == 'security':
        return status_code in {401, 403}
    if case.get('case_kind') == 'negative':
        return status_code >= 400
    if not expected or 'default' in expected:
        return 200 <= status_code < 400
    return str(status_code) in expected or any(
        key.endswith('XX') and str(status_code).startswith(key[:1]) for key in expected
    )


def _negative_request_body(case: Mapping[str, Any]) -> tuple[bool, Any]:
    if case.get('method') in {'GET', 'HEAD', 'OPTIONS'}:
        return False, None
    request_body = case.get('request_body') or {}
    if not isinstance(request_body, Mapping):
        return False, None
    if request_body.get('required') is True:
        return True, None
    schema = (
        request_body.get('content', {})
        .get('application/json', {})
        .get('schema', {})
    )
    if not isinstance(schema, Mapping):
        return False, None
    required_fields = schema.get('required')
    if not isinstance(required_fields, list) or not required_fields:
        return False, None
    body = _example(schema)
    if not isinstance(body, dict):
        return False, None
    for field in required_fields:
        if field in body:
            body.pop(field)
            break
    return True, body


@dataclass(frozen=True)
class PreparedContractRun:
    cases: list[dict[str, Any]]
    targets: list[tuple[dict[str, Any], str]]
    headers: dict[str, Any]
    timeout: float
    allow_private: bool


def prepare_contract_run(
    spec,
    *,
    base_url: str = '',
    headers: Mapping[str, Any] | None = None,
    kinds: list[str] | None = None,
    limit: int = MAX_CONTRACT_CASES,
    timeout: float = MAX_CONTRACT_TIMEOUT_SECONDS,
    case_definitions: list[dict[str, Any]] | None = None,
) -> PreparedContractRun:
    """Validate a bounded contract run without making outbound requests."""

    document = spec.document if isinstance(spec.document, Mapping) else {}
    if not base_url:
        servers = document.get('servers') or []
        if servers and isinstance(servers[0], Mapping):
            base_url = str(servers[0].get('url') or '')
    if not base_url:
        raise ValueError('OpenAPI contract execution requires base_url or document.servers[0].url')
    case_limit = parse_bounded_limit(limit, default=MAX_CONTRACT_CASES, maximum=MAX_CONTRACT_CASES)
    request_timeout = _validated_timeout(timeout)
    allow_private = bool(getattr(settings, 'PERF_ALLOW_PRIVATE_TARGETS', False))
    try:
        base_url = validate_outbound_url(base_url, allow_private=allow_private)
    except UnsafeOutboundURL as exc:
        raise ValueError(str(exc)) from exc
    request_headers = dict(headers or {})
    selected = set(kinds or ('positive', 'negative', 'security'))
    definitions = generate_contract_cases(spec) if case_definitions is None else case_definitions
    cases = list(islice(
        (case for case in definitions if case['case_kind'] in selected),
        case_limit,
    ))
    targets = [
        (case, _case_url(base_url, case, allow_private=allow_private, validate_target=False))
        for case in cases
    ]
    return PreparedContractRun(cases, targets, request_headers, request_timeout, allow_private)


def execute_prepared_contract_run(prepared: PreparedContractRun) -> list[dict[str, Any]]:
    """Execute a prevalidated plan within the shared wall-clock budget."""

    deadline = time.monotonic() + CONTRACT_RUN_BUDGET_SECONDS
    results = []
    for index, (case, url) in enumerate(prepared.targets):
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            results.extend({
                'contract_key': pending['contract_key'],
                'operation_key': pending['operation_key'],
                'case_kind': pending['case_kind'],
                'status': 'blocked',
                'duration_ms': 0,
                'result': redact_payload({'method': pending['method'], 'path': pending['path']}),
                'error_message': 'contract execution time budget exhausted before request',
            } for pending, _pending_url in prepared.targets[index:])
            break
        method = case['method']
        if case['case_kind'] == 'negative':
            can_generate_invalid_request, body = _negative_request_body(case)
            if not can_generate_invalid_request:
                results.append({
                    'contract_key': case['contract_key'],
                    'operation_key': case['operation_key'],
                    'case_kind': case['case_kind'],
                    'status': 'skipped',
                    'duration_ms': 0,
                    'result': redact_payload({'method': method, 'path': case['path']}),
                    'error_message': 'could not generate a contract-invalid negative request',
                })
                continue
        else:
            body = _example((case.get('request_body') or {}).get('content', {}).get('application/json', {}).get('schema', {}))

        started = time.monotonic()
        case_timeout = min(prepared.timeout, remaining / 2)
        request_headers = dict(prepared.headers)
        if case['case_kind'] == 'security':
            request_headers = {
                key: value for key, value in request_headers.items()
                if str(key).lower() not in {'authorization', 'cookie', 'x-api-key'}
            }
        try:
            response = guarded_request(
                method=method,
                url=url,
                allow_private=prepared.allow_private,
                headers=request_headers,
                json=body if method not in {'GET', 'HEAD', 'OPTIONS'} else None,
                timeout=case_timeout,
                allow_redirects=False,
            )
            passed = _response_is_expected(response.status_code, case)
            results.append({
                'contract_key': case['contract_key'],
                'operation_key': case['operation_key'],
                'case_kind': case['case_kind'],
                'status': 'passed' if passed else 'failed',
                'duration_ms': int((time.monotonic() - started) * 1000),
                'result': redact_payload({
                    'status_code': response.status_code,
                    'method': method,
                    'path': case['path'],
                    'expected_responses': list((case.get('responses') or {}).keys()),
                }),
                'error_message': '' if passed else f'unexpected response status: {response.status_code}',
            })
        except Exception as exc:  # noqa: BLE001 - one case failure must remain a result
            results.append({
                'contract_key': case['contract_key'],
                'operation_key': case['operation_key'],
                'case_kind': case['case_kind'],
                'status': 'failed',
                'duration_ms': int((time.monotonic() - started) * 1000),
                'result': redact_payload({'method': method, 'path': case['path']}),
                'error_message': redact_payload(str(exc)[:4000]),
            })
    return results


def execute_contract_cases(
    spec,
    *,
    base_url: str = '',
    headers: Mapping[str, Any] | None = None,
    kinds: list[str] | None = None,
    limit: int = MAX_CONTRACT_CASES,
    timeout: float = MAX_CONTRACT_TIMEOUT_SECONDS,
    case_definitions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Validate and execute bounded cases without persisting raw secrets."""

    prepared = prepare_contract_run(
        spec,
        base_url=base_url,
        headers=headers,
        kinds=kinds,
        limit=limit,
        timeout=timeout,
        case_definitions=case_definitions,
    )
    return execute_prepared_contract_run(prepared)


__all__ = [
    'execute_contract_cases',
    'execute_prepared_contract_run',
    'prepare_contract_run',
]
