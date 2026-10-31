"""Deterministic cross-run failure clustering and repair feedback."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.quality_intelligence.models import QualityFailure, QualityFailureCluster, QualityRun
from apps.quality_intelligence.services.identity import effective_environment
from apps.quality_intelligence.services.redaction import redact_payload


def failure_cluster_key(*, source_type, project_type, project_id, test_key, environment_fingerprint, signature):
    identity = {
        'source_type': str(source_type or '').upper(),
        'project_type': str(project_type or ''),
        'project_id': str(project_id or ''),
        'test_key': str(test_key or ''),
        'environment_fingerprint': str(environment_fingerprint or 'unknown'),
        'signature': str(signature or ''),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _run_time(run: QualityRun) -> datetime:
    return run.finished_at or run.started_at or run.created_at or timezone.now()


def _item_identity(failure: QualityFailure) -> tuple[str, str]:
    return (
        str(getattr(failure.item, 'test_key', '') or ''),
        effective_environment(getattr(failure.item, 'environment_fingerprint', ''), failure.run.environment_fingerprint),
    )


def _matching_passes(queryset, identities, timestamp):
    if not identities:
        return []
    # One indexed lookup for the batch; exact pairs are checked below to avoid
    # combining a test key from one identity with another environment.
    return [cluster for cluster in queryset.select_for_update().filter(
        Q(last_seen_at__lte=timestamp) | Q(last_seen_at__isnull=True),
        Q(resolved_at__lte=timestamp) | Q(resolved_at__isnull=True),
        test_key__in={key for key, _env in identities},
        environment_fingerprint__in={env for _key, env in identities},
    ) if (cluster.test_key, cluster.environment_fingerprint) in identities]


def _mark_fixed(run: QualityRun) -> None:
    timestamp = _run_time(run)
    identities = {
        (key, effective_environment(env, run.environment_fingerprint))
        for key, env in run.items.filter(outcome='PASS').values_list('test_key', 'environment_fingerprint')
    }
    clusters = _matching_passes(QualityFailureCluster.objects.filter(
        source_type=run.source_type, project_type=run.project_type, project_id=run.project_id,
    ), identities, timestamp)
    now = timezone.now()
    for cluster in clusters:
        cluster.resolved_at = timestamp
        cluster.last_fixed_run = run
        cluster.updated_at = now
    if clusters:
        QualityFailureCluster.objects.bulk_update(clusters, ['resolved_at', 'last_fixed_run', 'updated_at'], batch_size=200)


@transaction.atomic
def sync_failure_clusters(run: QualityRun) -> list[QualityFailureCluster]:
    """Upsert each cluster once per batch, preserving event-time lifecycle order."""
    failures = list(run.failures.select_related('item').all())
    grouped = defaultdict(list)
    changed = []
    new = []
    timestamp = _run_time(run)
    for failure in failures:
        test_key, environment = _item_identity(failure)
        # Recompute from the namespace as well: cached keys from older records
        # cannot carry a legacy project identity into a unified projection.
        key = failure_cluster_key(
            source_type=run.source_type, project_type=run.project_type, project_id=run.project_id,
            test_key=test_key, environment_fingerprint=environment, signature=failure.signature,
        )
        if failure.cluster_key != key:
            failure.cluster_key = key
            failure.updated_at = timezone.now()
            changed.append(failure)
        grouped[key].append(failure)
    if changed:
        QualityFailure.objects.bulk_update(changed, ['cluster_key', 'updated_at'], batch_size=200)
    keys = list(grouped)
    existing = {c.cluster_key: c for c in QualityFailureCluster.objects.select_for_update().filter(cluster_key__in=keys)}
    for key, rows in grouped.items():
        if key in existing:
            continue
        failure = rows[0]
        test_key, environment = _item_identity(failure)
        new.append(QualityFailureCluster(
            cluster_key=key, source_type=run.source_type, project_type=run.project_type,
            project_id=run.project_id, test_key=test_key, environment_fingerprint=environment,
            signature=failure.signature, taxonomy_code=failure.taxonomy_code, subtype=failure.subtype,
            first_seen_at=timestamp,
        ))
    if new:
        QualityFailureCluster.objects.bulk_create(new, ignore_conflicts=True, batch_size=200)
        existing = {c.cluster_key: c for c in QualityFailureCluster.objects.select_for_update().filter(cluster_key__in=keys)}
    # Forced re-ingestion is idempotent: derive counts once for all affected keys.
    counts = dict(QualityFailure.objects.filter(cluster_key__in=keys).order_by().values('cluster_key').annotate(
        total=Count('id')
    ).values_list('cluster_key', 'total'))
    now = timezone.now()
    for key, rows in grouped.items():
        cluster = existing[key]
        failure = rows[0]
        newer_failure = cluster.last_seen_at is None or timestamp >= cluster.last_seen_at
        cluster.first_seen_at = min(value for value in (cluster.first_seen_at, timestamp) if value is not None)
        cluster.last_seen_at = max(value for value in (cluster.last_seen_at, timestamp) if value is not None)
        cluster.occurrence_count = counts.get(key, 0)
        if newer_failure:
            cluster.last_failure_run = run
            cluster.taxonomy_code = failure.taxonomy_code
            cluster.subtype = failure.subtype
            cluster.metadata = redact_payload({**(cluster.metadata or {}), 'last_message': failure.message, 'last_phase': failure.phase})
        # A late backfill must not reopen a cluster already fixed at a later time.
        if cluster.resolved_at is None or timestamp >= cluster.resolved_at:
            cluster.resolved_at = None
        cluster.updated_at = now
    clusters = [existing[key] for key in keys]
    if clusters:
        QualityFailureCluster.objects.bulk_update(clusters, [
            'first_seen_at', 'last_seen_at', 'occurrence_count', 'last_failure_run',
            'taxonomy_code', 'subtype', 'metadata', 'resolved_at', 'updated_at',
        ], batch_size=200)
    project = None
    if any(c.occurrence_count > 1 and c.resolved_at is None for c in clusters) and run.project_type == 'PROJECT':
        from apps.projects.models import Project
        project = Project.objects.filter(pk=run.project_id).first()
    if project is not None:
        from apps.executions.notifications import emit_project_notification
        for cluster in clusters:
            if cluster.occurrence_count > 1 and cluster.resolved_at is None:
                emit_project_notification(project, 'failure_reopened', f'failure-cluster:{cluster.cluster_key}:{cluster.occurrence_count}', {
                    'cluster_id': cluster.pk, 'cluster_key': cluster.cluster_key, 'signature': cluster.signature,
                    'occurrence_count': cluster.occurrence_count, 'test_key': cluster.test_key,
                })
    _mark_fixed(run)
    return clusters


@transaction.atomic
def sync_execution_job_clusters(job) -> list[QualityFailureCluster]:
    """Project the final unified attempt into the same cluster lifecycle."""
    from apps.executions.failure_intelligence import classify_failure
    from apps.executions.models import ExecutionStepResult
    from apps.projects.models import Project

    results = ExecutionStepResult.objects.filter(attempt__job=job, attempt__is_final=True)
    rows = results.filter(status__in={'failed', 'blocked'}).order_by().values(
        'case_key', 'error_type', 'error_message',
    ).distinct().iterator(chunk_size=200)
    timestamp = job.finished_at or job.created_at or timezone.now()
    snapshot = job.environment_snapshot or {}
    environment = str(snapshot.get('environment_fingerprint') or snapshot.get('fingerprint') or snapshot.get('name') or 'unknown')
    project = Project.objects.filter(pk=job.project_id).first()
    grouped = {}
    for row in rows:
        message = str(row['error_message'] or '')[:4000]
        category, subtype = classify_failure(message, row['error_type'])
        signature = hashlib.sha256(json.dumps({
            'error_type': row['error_type'], 'message': message, 'case_key': row['case_key'],
        }, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:128]
        key = failure_cluster_key(
            source_type='EXECUTION', project_type='PROJECT', project_id=str(job.project_id),
            test_key=row['case_key'], environment_fingerprint=environment, signature=signature,
        )
        grouped[key] = (row['case_key'], message, category, subtype, signature)
    keys = list(grouped)

    def load_existing():
        loaded = {}
        for offset in range(0, len(keys), 200):
            loaded.update({c.cluster_key: c for c in QualityFailureCluster.objects.select_for_update().filter(
                cluster_key__in=keys[offset:offset + 200],
            )})
        return loaded

    existing = load_existing()
    new = [QualityFailureCluster(
        cluster_key=key, source_type='EXECUTION', project_type='PROJECT', project_id=str(job.project_id),
        test_key=case_key, environment_fingerprint=environment, signature=signature,
        taxonomy_code=category, subtype=subtype, first_seen_at=timestamp, mapped_project=project,
    ) for key, (case_key, _message, category, subtype, signature) in grouped.items() if key not in existing]
    if new:
        QualityFailureCluster.objects.bulk_create(new, ignore_conflicts=True, batch_size=200)
        existing = load_existing()
    now = timezone.now()
    for key, (_row, message, category, subtype, _signature) in grouped.items():
        cluster = existing[key]
        metadata = dict(cluster.metadata or {})
        job_ids = list(dict.fromkeys([*(metadata.get('execution_job_ids') or []), job.pk]))[-100:]
        newer = cluster.last_seen_at is None or timestamp >= cluster.last_seen_at
        cluster.first_seen_at = min(value for value in (cluster.first_seen_at, timestamp) if value is not None)
        cluster.last_seen_at = max(value for value in (cluster.last_seen_at, timestamp) if value is not None)
        cluster.occurrence_count = max(cluster.occurrence_count, len(job_ids))
        metadata['execution_job_ids'] = job_ids
        if newer:
            cluster.taxonomy_code, cluster.subtype = category, subtype
            metadata['last_message'] = message
        cluster.metadata = redact_payload(metadata)
        if project is not None:
            cluster.mapped_project = project
        if cluster.resolved_at is None or timestamp >= cluster.resolved_at:
            cluster.resolved_at = None
        cluster.updated_at = now
    clusters = [existing[key] for key in keys]
    if clusters:
        QualityFailureCluster.objects.bulk_update(clusters, [
            'first_seen_at', 'last_seen_at', 'occurrence_count', 'taxonomy_code', 'subtype',
            'metadata', 'mapped_project', 'resolved_at', 'updated_at',
        ], batch_size=200)
    failed_cases = results.filter(status__in={'failed', 'blocked'}).order_by().values('case_key')
    passing_cases = results.filter(status__in={'passed', 'succeeded', 'completed'}).exclude(
        case_key__in=failed_cases,
    ).order_by().values_list('case_key', flat=True).distinct()
    passed = {(key, environment) for key in passing_cases.iterator(chunk_size=200)}
    fixed = _matching_passes(QualityFailureCluster.objects.filter(
        source_type='EXECUTION', project_type='PROJECT', project_id=str(job.project_id),
    ), passed, timestamp)
    for cluster in fixed:
        cluster.resolved_at = timestamp
        cluster.metadata = redact_payload({**(cluster.metadata or {}), 'last_fixed_job_id': job.pk})
        cluster.updated_at = now
    if fixed:
        QualityFailureCluster.objects.bulk_update(fixed, ['resolved_at', 'metadata', 'updated_at'], batch_size=200)
    return clusters


__all__ = ['failure_cluster_key', 'sync_failure_clusters', 'sync_execution_job_clusters']
