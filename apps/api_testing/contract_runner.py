"""Bounded deterministic OpenAPI contract execution helper."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin

import requests
from django.conf import settings

from apps.core.network import UnsafeOutboundURL, validate_outbound_url
from apps.quality_intelligence.services.redaction import redact_payload

from .contracts import generate_contract_cases


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


def _response_is_expected(status_code: int, case: Mapping[str, Any]) -> bool:
    expected = set(str(key) for key in (case.get('responses') or {}).keys())
    if case.get('case_kind') == 'security':
        return status_code in {401, 403}
    if case.get('case_kind') == 'negative':
        return status_code >= 400 or str(status_code) in expected
    if not expected or 'default' in expected:
        return 200 <= status_code < 400
    return str(status_code) in expected or any(
        key.endswith('XX') and str(status_code).startswith(key[:1]) for key in expected
    )


def execute_contract_cases(
    spec,
    *,
    base_url: str = '',
    headers: Mapping[str, Any] | None = None,
    kinds: list[str] | None = None,
    limit: int = 100,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Execute bounded generated cases; never persists raw request/response secrets."""

    document = spec.document if isinstance(spec.document, Mapping) else {}
    if not base_url:
        servers = document.get('servers') or []
        if servers and isinstance(servers[0], Mapping):
            base_url = str(servers[0].get('url') or '')
    if not base_url:
        raise ValueError('OpenAPI contract execution requires base_url or document.servers[0].url')
    try:
        base_url = validate_outbound_url(
            base_url,
            allow_private=bool(getattr(settings, 'PERF_ALLOW_PRIVATE_TARGETS', False)),
        )
    except UnsafeOutboundURL as exc:
        raise ValueError(str(exc)) from exc
    headers = dict(headers or {})
    selected = set(kinds or ('positive', 'negative', 'security'))
    cases = [case for case in generate_contract_cases(spec) if case['case_kind'] in selected][:max(1, min(int(limit), 100))]
    results = []
    for case in cases:
        started = time.monotonic()
        method = case['method']
        url = urljoin(base_url.rstrip('/') + '/', _path_with_examples(case['path'], case.get('parameters', []))).replace('://', '§§').replace('//', '/').replace('§§', '://')
        request_headers = dict(headers)
        if case['case_kind'] == 'security':
            request_headers = {
                key: value for key, value in request_headers.items()
                if str(key).lower() not in {'authorization', 'cookie', 'x-api-key'}
            }
        body = _example((case.get('request_body') or {}).get('content', {}).get('application/json', {}).get('schema', {}))
        if case['case_kind'] == 'negative' and isinstance(body, dict):
            required = ((case.get('request_body') or {}).get('required') is True)
            if required and body:
                body.pop(next(iter(body)))
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=request_headers,
                json=body if method not in {'GET', 'HEAD', 'OPTIONS'} else None,
                timeout=max(0.5, min(float(timeout), 60.0)),
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
                'error_message': str(exc)[:4000],
            })
    return results


__all__ = ['execute_contract_cases']
