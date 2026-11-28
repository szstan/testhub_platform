"""Opaque fingerprints for persisted API case execution inputs."""

from __future__ import annotations

import hashlib
import hmac
import json

from django.conf import settings


_SNAPSHOT_DOMAIN = b'api-case-execution-snapshot:v1\0'
_AUDIT_FIELDS = {'created_by_id', 'created_at', 'updated_at'}


def _model_configuration(instance, *, excluded_fields=frozenset()):
    if instance is None:
        return None
    excluded = _AUDIT_FIELDS | set(excluded_fields)
    return {
        field.attname: getattr(instance, field.attname)
        for field in instance._meta.concrete_fields
        if not field.primary_key and field.attname not in excluded
    }


def _request_configuration(api_request):
    configuration = _model_configuration(api_request)
    collection = getattr(api_request, 'collection', None)
    configuration['collection_project_id'] = getattr(collection, 'project_id', None)
    return configuration


def build_api_case_execution_snapshot(test_case, *, environment, steps=None):
    """Capture stored execution definitions without resolving templates or secrets."""
    if steps is None:
        steps = test_case.steps.select_related('request__collection').order_by('order', 'pk')

    step_snapshots = []
    for step in steps:
        step_snapshots.append({
            'step': _model_configuration(step, excluded_fields={'test_case_id'}),
            'request': _request_configuration(step.request),
        })

    environment_snapshot = _model_configuration(environment, excluded_fields={'id'})
    if environment_snapshot is not None:
        for field_name in ('headers', 'default_headers'):
            if field_name not in environment_snapshot and hasattr(environment, field_name):
                environment_snapshot[field_name] = getattr(environment, field_name)

    return {
        'version': 1,
        'case': _model_configuration(test_case, excluded_fields={'id'}),
        'environment': environment_snapshot,
        'steps': step_snapshots,
    }


def api_case_execution_payload_hash(execution_fingerprint, *, test_case_id, environment_id, variables):
    """Bind the exact case, environment and runtime overrides to the source digest."""
    payload = json.dumps(
        {
            'version': 2,
            'test_case_id': test_case_id,
            'execution_fingerprint': execution_fingerprint,
            'environment_id': environment_id,
            'variables': variables,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def api_case_execution_fingerprint(test_case, *, environment, steps=None):
    """Return an HMAC digest so configuration values never leave the server."""
    snapshot = build_api_case_execution_snapshot(
        test_case, environment=environment, steps=steps,
    )
    serialized = json.dumps(
        snapshot,
        sort_keys=True,
        ensure_ascii=False,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return hmac.new(
        str(settings.SECRET_KEY).encode('utf-8'),
        _SNAPSHOT_DOMAIN + serialized,
        hashlib.sha256,
    ).hexdigest()
