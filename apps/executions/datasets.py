"""Deterministic data-matrix expansion and safe fixture lifecycle helpers."""

from __future__ import annotations

import itertools
import os
from typing import Any, Callable, Mapping

from django.utils import timezone

from .models import ExecutionDataset, ExecutionFixture
from .result_events import redact_result_payload


class DatasetError(ValueError):
    pass


def expand_dataset(dataset: ExecutionDataset | Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return deterministic variants from rows or a cartesian matrix."""
    if isinstance(dataset, ExecutionDataset):
        data = dataset.data or []
        matrix = dataset.matrix or {}
    else:
        data = dataset.get('data') or []
        matrix = dataset.get('matrix') or {}
    if data:
        if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
            raise DatasetError('dataset.data must be a list of objects')
        return [dict(item) for item in data]
    if not matrix:
        return [{}]
    if not isinstance(matrix, Mapping) or not all(isinstance(value, list) for value in matrix.values()):
        raise DatasetError('dataset.matrix must map column names to lists')
    columns = sorted(matrix)
    return [dict(zip(columns, values)) for values in itertools.product(*(matrix[column] for column in columns))]


def redact_dataset(dataset: Mapping[str, Any]) -> dict[str, Any]:
    return redact_result_payload(dict(dataset))


def secret_reference_name(reference: str) -> str:
    """Validate a secret reference without returning the secret value."""
    reference = str(reference or '').strip()
    if not reference or not reference.replace('_', 'a').isalnum() or reference[0].isdigit():
        raise DatasetError('secret reference must be an environment variable name')
    return reference


def resolve_secret_reference(reference: str) -> str:
    """Resolve only for an execution adapter; callers must never serialize it."""
    return os.environ.get(secret_reference_name(reference), '')


def register_fixture(project, run_id: str, *, dataset=None, references=None, created_by=None):
    references = dict(references or {})
    return ExecutionFixture.objects.create(
        project=project,
        dataset=dataset,
        run_id=str(run_id),
        references=redact_dataset(references),
        created_by=created_by,
        cleanup_required=bool(references),
    )


def cleanup_fixture(fixture: ExecutionFixture, cleaner: Callable[[str, Any], Any] | None = None):
    """Clean only explicit references; an empty fixture is always a safe skip."""
    if not fixture.cleanup_required or not fixture.references:
        fixture.status = 'skipped'
        fixture.cleanup_required = False
        fixture.cleaned_at = timezone.now()
        fixture.save(update_fields=['status', 'cleanup_required', 'cleaned_at'])
        return {'status': 'skipped', 'count': 0}
    if cleaner is None:
        fixture.status = 'skipped'
        fixture.cleanup_required = True
        fixture.cleanup_error = 'No cleanup handler was supplied; no destructive action was attempted.'
        fixture.save(update_fields=['status', 'cleanup_error'])
        return {'status': 'skipped', 'count': 0, 'reason': fixture.cleanup_error}
    fixture.status = 'cleaning'
    fixture.save(update_fields=['status'])
    try:
        count = 0
        for entity_type, entity_ids in fixture.references.items():
            if not isinstance(entity_ids, list):
                continue
            for entity_id in entity_ids:
                if entity_id in (None, ''):
                    continue
                cleaner(entity_type, entity_id)
                count += 1
        fixture.status = 'cleaned'
        fixture.cleanup_required = False
        fixture.cleaned_at = timezone.now()
        fixture.save(update_fields=['status', 'cleanup_required', 'cleaned_at'])
        return {'status': 'cleaned', 'count': count}
    except Exception as exc:  # noqa: BLE001
        fixture.status = 'failed'
        fixture.cleanup_error = str(exc)[:4000]
        fixture.save(update_fields=['status', 'cleanup_error'])
        raise
