"""Typed source references and project-scoped legacy runner bindings."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import ExecutionJob, ExecutionSourceBinding


_SOURCE_REF_RE = re.compile(r"^(?P<source>api|ui|app|performance):(?P<kind>suite|request|case|scenario):(?P<id>[1-9][0-9]*)$")


class SourceBindingError(RuntimeError):
    """Raised when a unified source is not explicitly bound to its project."""


@dataclass(frozen=True)
class SourceReference:
    source_type: str
    kind: str
    object_id: int
    canonical: str


def parse_source_id(source_type: str, source_id: str) -> SourceReference:
    value = str(source_id or '').strip()
    match = _SOURCE_REF_RE.fullmatch(value)
    if match is None or match.group('source') != str(source_type or '').strip():
        raise SourceBindingError(
            f"source_id must use a typed reference for {source_type!r}"
        )
    return SourceReference(
        source_type=match.group('source'),
        kind=match.group('kind'),
        object_id=int(match.group('id')),
        canonical=value,
    )


def resolve_source_binding(job: ExecutionJob) -> tuple[SourceReference, ExecutionSourceBinding]:
    reference = parse_source_id(job.source_type, job.source_id)
    binding = ExecutionSourceBinding.objects.filter(
        project_id=job.project_id,
        source_type=reference.source_type,
        source_id=reference.canonical,
        enabled=True,
    ).first()
    if binding is None:
        raise SourceBindingError(
            f"no enabled source binding for project={job.project_id} source={reference.canonical!r}"
        )
    return reference, binding


__all__ = ['SourceBindingError', 'SourceReference', 'parse_source_id', 'resolve_source_binding']
