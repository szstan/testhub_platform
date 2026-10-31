"""Read-only requirement coverage over finalized execution evidence."""

from __future__ import annotations

from django.db.models import Count, F, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce

from apps.requirement_analysis.models import BusinessRequirement

from .models import ExecutionAttempt, ExecutionJobVariant, ExecutionStepResult, RequirementTestCaseLink

_SUCCESS = {'passed', 'succeeded', 'completed'}
_FAILURE = {'failed', 'blocked', 'error'}
_EXECUTED = _SUCCESS | _FAILURE | {'skipped', 'inconclusive'}
_TERMINAL = _SUCCESS | {'failed', 'blocked', 'cancelled', 'unsupported'}


def final_step_results(*, project_id=None, job_id=None):
    """Select current evidence only after authoritative job finalization.

    A matrix has one representative final pointer, while every variant retains
    its own latest terminal attempt. Historical and unfinalized rows stay out of
    both coverage and failure analysis.
    """
    latest_variant_attempt = ExecutionAttempt.objects.filter(
        job_id=OuterRef('attempt__job_id'), variant_id=OuterRef('attempt__variant_id'),
    ).order_by('-attempt_number', '-pk').values('pk')[:1]
    current_attempt = (
        Q(attempt__variant__isnull=True, attempt__is_final=True,
          attempt_id=F('attempt__job__final_attempt_id'))
        | Q(attempt__variant__isnull=False,
            attempt__variant__job_id=F('attempt__job_id'),
            attempt_id=Subquery(latest_variant_attempt))
    )
    # Older result imports recorded finished_at before finalized_at existed.
    # A terminal state plus the owned final pointer/is_final flag remains the
    # authority; accept that completion timestamp without writing on a GET.
    rows = ExecutionStepResult.objects.filter(
        current_attempt,
        Q(attempt__job__finalized_at__isnull=False) | Q(attempt__job__finished_at__isnull=False),
        attempt__job__final_status__in=_TERMINAL,
        attempt__job__status=F('attempt__job__final_status'),
        attempt__job__final_attempt__is_final=True,
        attempt__job__final_attempt__job_id=F('attempt__job_id'),
        attempt__status__in=_TERMINAL,
    )
    if project_id is not None:
        rows = rows.filter(attempt__job__project_id=project_id)
    if job_id is not None:
        rows = rows.filter(attempt__job_id=job_id)
    return rows


def requirement_coverage(project_id: int, version_id: int | None = None) -> dict:
    # Requirements have project scope, with no version field. The denominator
    # stays project-wide; selecting a version scopes both eligible case links
    # and their execution evidence to that version.
    requirements = BusinessRequirement.objects.filter(analysis__document__project_id=project_id)
    links = RequirementTestCaseLink.objects.filter(
        requirement__in=requirements, test_case__project_id=project_id,
    )
    if version_id is not None:
        links = links.filter(test_case__versions__id=version_id)
    requirement_keys = {}
    for requirement_id, case_key in links.values_list('requirement_id', 'test_case__external_ref').iterator():
        requirement_keys.setdefault(requirement_id, set()).add(case_key)
    linked_ids = set(requirement_keys)

    expected_variants = ExecutionJobVariant.objects.filter(
        job_id=OuterRef('attempt__job_id'),
    ).order_by().values('job_id').annotate(total=Count('pk')).values('total')[:1]
    rows = final_step_results(project_id=project_id).filter(
        case_key__in=links.values('test_case__external_ref'),
    )
    if version_id is not None:
        rows = rows.filter(attempt__job__version_id=version_id)
    grouped = rows.order_by().values('case_key', 'attempt__job_id').annotate(
        finalized_order=Coalesce('attempt__job__finalized_at', 'attempt__job__finished_at'),
        total_steps=Count('pk'),
        passed_steps=Count('pk', filter=Q(status__in=_SUCCESS)),
        failed_steps=Count('pk', filter=Q(status__in=_FAILURE)),
        executed_steps=Count('pk', filter=Q(status__in=_EXECUTED)),
        participating_variants=Count('attempt__variant_id', distinct=True),
        expected_variants=Coalesce(Subquery(expected_variants, output_field=IntegerField()), Value(0)),
    ).order_by('case_key', '-finalized_order', '-attempt__job_id')
    latest_by_case = {}
    for row in grouped.iterator(chunk_size=1000):
        key = row['case_key']
        if key in latest_by_case:
            continue
        if row['failed_steps']:
            case_status = 'failed'
        elif (
            row['passed_steps'] == row['total_steps']
            and row['participating_variants'] == row['expected_variants']
        ):
            case_status = 'passed'
        elif row['executed_steps']:
            case_status = 'inconclusive'
        else:
            case_status = 'pending'
        latest_by_case[key] = case_status

    executed_ids, passed_ids, failed_ids = set(), set(), set()
    for requirement_id, keys in requirement_keys.items():
        statuses = [latest_by_case.get(key) for key in keys]
        if any(value in _EXECUTED for value in statuses):
            executed_ids.add(requirement_id)
        # Missing evidence is deliberately retained in statuses: every linked
        # case, and every step/variant of it, must have trustworthy final success.
        if statuses and all(value in _SUCCESS for value in statuses):
            passed_ids.add(requirement_id)
        if any(value in _FAILURE for value in statuses):
            failed_ids.add(requirement_id)

    requirement_ids = set(requirements.values_list('pk', flat=True))
    total = len(requirement_ids)
    return {
        'project': project_id,
        'version': version_id,
        'total_requirements': total,
        'covered_requirements': len(linked_ids),
        'linked_requirements': len(linked_ids),
        'executed_requirements': len(executed_ids),
        'passed_requirements': len(passed_ids),
        'failed_requirements': len(failed_ids),
        'unexecuted_requirements': sorted(linked_ids - executed_ids),
        'uncovered_requirement_ids': sorted(requirement_ids - linked_ids),
        'executed_requirement_ids': sorted(executed_ids),
        'passed_requirement_ids': sorted(passed_ids),
        'failed_requirement_ids': sorted(failed_ids),
        'coverage_rate': round(len(linked_ids) / total * 100, 1) if total else 0,
        'executed_coverage_rate': round(len(executed_ids) / total * 100, 1) if total else 0,
        'passed_coverage_rate': round(len(passed_ids) / total * 100, 1) if total else 0,
    }
