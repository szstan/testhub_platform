"""OpenAPI contract versioning, diff, case generation and coverage."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.db.models import Q

from apps.executions.models import ExecutionStepResult

from .models import OpenApiContractSpec


HTTP_METHODS = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace'}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def contract_operations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    paths = document.get('paths') if isinstance(document, dict) else {}
    if not isinstance(paths, dict):
        raise ValueError('OpenAPI document.paths 必须是对象')
    operations = {}
    for path, item in paths.items():
        if not isinstance(item, dict) or not str(path).startswith('/'):
            continue
        for method, operation in item.items():
            method = str(method).lower()
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            key = str(operation.get('operationId') or f'{method.upper()} {path}')
            operations[key] = {
                'key': key,
                'path': str(path),
                'method': method.upper(),
                'operation_id': str(operation.get('operationId') or ''),
                'summary': str(operation.get('summary') or operation.get('description') or '')[:500],
                'hash': _hash(operation),
                'security': operation.get('security', document.get('security', [])),
                'parameters': operation.get('parameters', []),
                'request_body': operation.get('requestBody', {}),
                'responses': operation.get('responses', {}),
            }
    return operations


def diff_contracts(current: OpenApiContractSpec, baseline: OpenApiContractSpec) -> dict[str, Any]:
    current_ops = contract_operations(current.document)
    baseline_ops = contract_operations(baseline.document)
    added = sorted(set(current_ops) - set(baseline_ops))
    removed = sorted(set(baseline_ops) - set(current_ops))
    changed = sorted(key for key in set(current_ops) & set(baseline_ops) if current_ops[key]['hash'] != baseline_ops[key]['hash'])
    return {
        'current_spec_id': current.pk,
        'baseline_spec_id': baseline.pk,
        'current_version': current.version,
        'baseline_version': baseline.version,
        'added': added,
        'removed': removed,
        'changed': changed,
        'breaking': removed,
        'passed': not removed,
    }


def generate_contract_cases(spec: OpenApiContractSpec) -> list[dict[str, Any]]:
    cases = []
    for operation in contract_operations(spec.document).values():
        key = operation['key']
        cases.append({'external_ref': f'openapi:{key}:positive', 'kind': 'positive', **operation})
        cases.append({'external_ref': f'openapi:{key}:negative', 'kind': 'negative', 'strategy': 'required-field-and-type-boundaries', **operation})
        if operation.get('security'):
            cases.append({'external_ref': f'openapi:{key}:unauthorized', 'kind': 'security', 'strategy': 'missing-credentials', **operation})
    return cases


def coverage_for_spec(spec: OpenApiContractSpec) -> dict[str, Any]:
    operations = contract_operations(spec.document)
    keys = set(operations)
    if not keys:
        return {'total': 0, 'covered': 0, 'coverage_rate': 0.0, 'uncovered': []}
    rows = ExecutionStepResult.objects.filter(
        attempt__job__project=spec.project,
        case_key__in=keys,
    ).filter(status__in={'passed', 'succeeded', 'completed'})
    covered = set(rows.values_list('case_key', flat=True))
    return {
        'spec_id': spec.pk,
        'version': spec.version,
        'total': len(keys),
        'covered': len(covered),
        'coverage_rate': round(len(covered) / len(keys) * 100, 2),
        'uncovered': sorted(keys - covered),
    }
