"""Typed source references and project-scoped legacy runner bindings."""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.core.exceptions import ObjectDoesNotExist

from .models import ExecutionJob, ExecutionSourceBinding


_SOURCE_REF_RE = re.compile(r"^(?P<source>api|ui|app|performance):(?P<kind>suite|request|case|scenario):(?P<id>[1-9][0-9]*)$")


class SourceBindingError(RuntimeError):
    """Raised when a unified source is not explicitly bound to its project."""


class SourceBindingAccessError(SourceBindingError):
    """Raised when an actor cannot access the native source project."""


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
    raw_id = match.group('id')
    if len(raw_id) > 19 or int(raw_id) > 9_223_372_036_854_775_807:
        raise SourceBindingError('source identifier is outside the supported database range')
    return SourceReference(
        source_type=match.group('source'),
        kind=match.group('kind'),
        object_id=int(raw_id),
        canonical=value,
    )


def is_active_actor(user) -> bool:
    if user is None or not getattr(user, 'is_authenticated', False) or getattr(user, 'pk', None) is None:
        return False
    try:
        user.refresh_from_db(fields=('is_active', 'is_staff', 'is_superuser'))
    except (AttributeError, ObjectDoesNotExist):
        return False
    return bool(getattr(user, 'is_active', False))


def is_privileged_actor(user) -> bool:
    return bool(
        is_active_actor(user)
        and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))
    )


def _native_source(reference):
    parsed = parse_source_id(reference.source_type, reference.canonical)
    if (parsed.kind, parsed.object_id) != (reference.kind, reference.object_id):
        raise SourceBindingError('source reference fields do not match its typed identifier')

    if reference.source_type == 'api':
        from apps.api_testing.models import ApiRequest, TestSuite
        if reference.kind == 'suite':
            source = TestSuite.objects.select_related('project', 'environment').filter(pk=reference.object_id).first()
            project = source.project if source else None
        elif reference.kind == 'request':
            source = ApiRequest.objects.select_related('collection__project').filter(pk=reference.object_id).first()
            project = source.collection.project if source and source.collection_id else None
        else:
            raise SourceBindingError('Unsupported API source kind')
    elif reference.source_type == 'ui':
        from apps.ui_automation.models import TestCase, TestSuite
        model = {'suite': TestSuite, 'case': TestCase}.get(reference.kind)
        if model is None:
            raise SourceBindingError('Unsupported UI source kind')
        source = model.objects.select_related('project').filter(pk=reference.object_id).first()
        project = source.project if source else None
    elif reference.source_type == 'app':
        from apps.app_automation.models import AppTestCase, AppTestSuite
        model = {'suite': AppTestSuite, 'case': AppTestCase}.get(reference.kind)
        if model is None:
            raise SourceBindingError('Unsupported APP source kind')
        source = model.objects.select_related('project').filter(pk=reference.object_id).first()
        project = source.project if source else None
    elif reference.source_type == 'performance':
        if reference.kind != 'scenario':
            raise SourceBindingError('Unsupported performance source kind')
        from apps.perf_testing.models import PerfScenario
        source = PerfScenario.objects.select_related('project').filter(pk=reference.object_id).first()
        project = source.project if source else None
    else:
        raise SourceBindingError('Unsupported source type')

    if source is None:
        raise SourceBindingError('Native source was not found')
    return source, project


def _native_project_accessible(source_type, actor, project) -> bool:
    # resolve_native_source already refreshed the actor immediately before
    # this check; do not refresh the same role fields several times per poll.
    if project is None or not getattr(actor, 'is_active', False):
        return False
    if source_type == 'api':
        return bool(
            actor.is_staff or actor.is_superuser
            or project.owner_id == actor.pk
            or project.members.filter(pk=actor.pk).exists()
        )
    if source_type == 'ui':
        from apps.ui_automation.access import project_accessible
        return project_accessible(actor, project)
    if source_type == 'app':
        from apps.app_automation.access import project_accessible
        return project_accessible(actor, project)
    if source_type == 'performance':
        from apps.perf_testing.access import can_access_project
        return can_access_project(actor, project)
    return False


def resolve_native_source(reference: SourceReference, source_project_id, actor):
    """Load a typed native source and enforce its independent project boundary."""
    if not is_active_actor(actor):
        raise SourceBindingAccessError('an active authenticated execution actor is required')
    source, project = _native_source(reference)
    actual_project_id = str(getattr(project, 'pk', '') or '')
    expected_project_id = str(source_project_id or '').strip()
    if not actual_project_id or expected_project_id != actual_project_id:
        raise SourceBindingError('native source project does not match source_project_id')
    if not _native_project_accessible(reference.source_type, actor, project):
        raise SourceBindingAccessError('execution actor cannot access the native source project')
    return source


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


__all__ = [
    'SourceBindingAccessError', 'SourceBindingError', 'SourceReference',
    'is_active_actor', 'is_privileged_actor', 'parse_source_id',
    'resolve_native_source', 'resolve_source_binding',
]
