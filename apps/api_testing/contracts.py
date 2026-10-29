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
    breaking_changes = [
        {'operation_key': key, 'type': 'removed'} for key in removed
    ]
    return {
        'current_spec_id': current.pk,
        'baseline_spec_id': baseline.pk,
        'current_version': current.version,
        'baseline_version': baseline.version,
        'added': added,
        'removed': removed,
        'changed': changed,
        'breaking': removed,
        'breaking_changes': breaking_changes,
        'passed': not removed,
    }


CASE_KINDS = ('positive', 'negative', 'security')


def _bounded_case_key(value: str) -> str:
    value = str(value or '')
    if len(value) <= 255:
        return value
    digest = hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]
    return f'{value[:238]}-{digest}'


def legacy_contract_case_key(operation_key: str, kind: str) -> str:
    legacy_kind = 'unauthorized' if kind == 'security' else kind
    return _bounded_case_key(f'openapi:{operation_key}:{legacy_kind}')


def contract_case_key(spec: OpenApiContractSpec, operation_key: str, kind: str) -> str:
    return _bounded_case_key(
        f'openapi:{spec.pk}:{spec.version}:{operation_key}:{kind}'
    )


def contract_case_identity(spec: OpenApiContractSpec, case_key: str) -> dict[str, Any] | None:
    value = str(case_key or '')
    for operation_key, operation in contract_operations(spec.document).items():
        for kind in CASE_KINDS:
            if value in {
                operation_key,
                legacy_contract_case_key(operation_key, kind),
                contract_case_key(spec, operation_key, kind),
            }:
                return {
                    'operation_key': operation_key,
                    'method': operation['method'],
                    'path': operation['path'],
                    'kind': kind if value != operation_key else '',
                    'contract_key': contract_case_key(spec, operation_key, kind),
                    'external_ref': legacy_contract_case_key(operation_key, kind),
                }
    return None


def generate_contract_cases(spec: OpenApiContractSpec) -> list[dict[str, Any]]:
    cases = []
    for operation in contract_operations(spec.document).values():
        key = operation['key']
        case_definitions = [
            ('positive', 'schema-driven'),
            ('negative', 'required-field-and-type-boundaries'),
        ]
        if operation.get('security'):
            case_definitions.append(('security', 'missing-credentials'))
        for kind, strategy in case_definitions:
            cases.append({
                'external_ref': legacy_contract_case_key(key, kind),
                'contract_key': contract_case_key(spec, key, kind),
                'operation_key': key,
                'case_kind': kind,
                'kind': kind,
                'strategy': strategy,
                **operation,
            })
    return cases


def coverage_for_spec(spec: OpenApiContractSpec) -> dict[str, Any]:
    operations = contract_operations(spec.document)
    expected_cases = generate_contract_cases(spec)
    operation_keys = set(operations)
    match_keys = {
        key
        for case in expected_cases
        for key in (case['contract_key'], case['external_ref'], case['operation_key'])
    }
    rows = ExecutionStepResult.objects.filter(
        attempt__job__project=spec.project,
        case_key__in=match_keys,
        status__in={'passed', 'succeeded', 'completed'},
    ).values_list('case_key', flat=True)
    covered_keys = set(rows)
    covered_cases = []
    for case in expected_cases:
        if case['contract_key'] in covered_keys or case['external_ref'] in covered_keys:
            covered_cases.append(case)
    operation_covered = set(
        case['operation_key'] for case in covered_cases
    ) | (covered_keys & operation_keys)
    case_rows = []
    for case in expected_cases:
        identity = {
            'operation_key': case['operation_key'],
            'method': case['method'],
            'path': case['path'],
            'kind': case['case_kind'],
            'contract_key': case['contract_key'],
            'external_ref': case['external_ref'],
            'covered': case in covered_cases,
        }
        case_rows.append(identity)
    by_kind = {}
    for kind in CASE_KINDS:
        kind_rows = [row for row in case_rows if row['kind'] == kind]
        covered = sum(1 for row in kind_rows if row['covered'])
        by_kind[kind] = {
            'total': len(kind_rows),
            'covered': covered,
            'coverage_rate': round(covered / len(kind_rows) * 100, 2) if kind_rows else 0.0,
            'uncovered': [row['contract_key'] for row in kind_rows if not row['covered']],
        }
    return {
        'spec_id': spec.pk,
        'version': spec.version,
        'total': len(operation_keys),
        'covered': len(operation_covered),
        'coverage_rate': round(len(operation_covered) / len(operation_keys) * 100, 2) if operation_keys else 0.0,
        'uncovered': sorted(operation_keys - operation_covered),
        'case_total': len(case_rows),
        'case_covered': sum(1 for row in case_rows if row['covered']),
        'case_coverage_rate': round(sum(1 for row in case_rows if row['covered']) / len(case_rows) * 100, 2) if case_rows else 0.0,
        'by_kind': by_kind,
        'cases': case_rows,
        'uncovered_cases': [row for row in case_rows if not row['covered']],
    }
