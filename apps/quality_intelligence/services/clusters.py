"""Deterministic cross-run failure clustering and repair feedback."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.quality_intelligence.models import (
    QualityFailure,
    QualityFailureCluster,
    QualityRun,
)
from apps.quality_intelligence.services.redaction import redact_payload


def failure_cluster_key(
    *,
    source_type: str,
    project_type: str,
    project_id: str,
    test_key: str,
    environment_fingerprint: str,
    signature: str,
) -> str:
    identity = {
        'source_type': str(source_type or '').upper(),
        'project_type': str(project_type or ''),
        'project_id': str(project_id or ''),
        'test_key': str(test_key or ''),
        'environment_fingerprint': str(environment_fingerprint or 'unknown'),
        'signature': str(signature or ''),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()


def _run_time(run: QualityRun) -> datetime:
    return run.finished_at or run.started_at or run.created_at or timezone.now()


def _item_identity(failure: QualityFailure) -> tuple[str, str]:
    item = failure.item
    return (
        str(getattr(item, 'test_key', '') or ''),
        str(
            getattr(item, 'environment_fingerprint', '')
            or getattr(failure.run, 'environment_fingerprint', '')
            or 'unknown'
        ),
    )


def _mark_fixed(run: QualityRun) -> None:
    timestamp = _run_time(run)
    for item in run.items.filter(outcome='PASS').only('test_key', 'environment_fingerprint'):
        queryset = QualityFailureCluster.objects.filter(
            source_type=run.source_type,
            project_type=run.project_type,
            project_id=run.project_id,
            test_key=item.test_key,
            environment_fingerprint=item.environment_fingerprint or run.environment_fingerprint or 'unknown',
            resolved_at__isnull=True,
        )
        for cluster in queryset:
            cluster.resolved_at = timestamp
            cluster.last_fixed_run = run
            cluster.save(update_fields=['resolved_at', 'last_fixed_run', 'updated_at'])


@transaction.atomic
def sync_failure_clusters(run: QualityRun) -> list[QualityFailureCluster]:
    """Upsert failure clusters for one run and mark matching later passes fixed."""

    failures = list(run.failures.select_related('item').all())
    clusters: dict[str, QualityFailureCluster] = {}
    timestamp = _run_time(run)
    for failure in failures:
        test_key, environment = _item_identity(failure)
        cluster_key = failure.cluster_key or failure_cluster_key(
            source_type=run.source_type,
            project_type=run.project_type,
            project_id=run.project_id,
            test_key=test_key,
            environment_fingerprint=environment,
            signature=failure.signature,
        )
        if failure.cluster_key != cluster_key:
            failure.cluster_key = cluster_key
            failure.save(update_fields=['cluster_key', 'updated_at'])
        cluster, _created = QualityFailureCluster.objects.get_or_create(
            cluster_key=cluster_key,
            defaults={
                'source_type': run.source_type,
                'project_type': run.project_type,
                'project_id': run.project_id,
                'test_key': test_key,
                'environment_fingerprint': environment,
                'signature': failure.signature,
                'taxonomy_code': failure.taxonomy_code,
                'subtype': failure.subtype,
                'first_seen_at': timestamp,
            },
        )
        cluster.source_type = run.source_type
        cluster.project_type = run.project_type
        cluster.project_id = run.project_id
        cluster.test_key = test_key
        cluster.environment_fingerprint = environment
        cluster.signature = failure.signature
        cluster.taxonomy_code = failure.taxonomy_code
        cluster.subtype = failure.subtype
        cluster.first_seen_at = min(
            value for value in (cluster.first_seen_at, timestamp) if value is not None
        )
        cluster.last_seen_at = max(
            value for value in (cluster.last_seen_at, timestamp) if value is not None
        )
        cluster.occurrence_count = QualityFailure.objects.filter(cluster_key=cluster_key).count()
        cluster.last_failure_run = run
        cluster.resolved_at = None
        cluster.metadata = redact_payload({
            **(cluster.metadata or {}),
            'last_message': failure.message,
            'last_phase': failure.phase,
        })
        cluster.save()
        clusters[cluster_key] = cluster
        if cluster.occurrence_count > 1 and cluster.project_type in {'PROJECT', 'EXECUTION'}:
            from apps.executions.notifications import emit_project_notification
            from apps.projects.models import Project
            project = Project.objects.filter(pk=cluster.project_id).first()
            if project is not None:
                emit_project_notification(
                    project,
                    'failure_reopened',
                    f'failure-cluster:{cluster.cluster_key}:{cluster.occurrence_count}',
                    {
                        'cluster_id': cluster.pk,
                        'cluster_key': cluster.cluster_key,
                        'signature': cluster.signature,
                        'occurrence_count': cluster.occurrence_count,
                        'test_key': cluster.test_key,
                    },
                )

    _mark_fixed(run)
    return list(clusters.values())

def sync_execution_job_clusters(job) -> list[QualityFailureCluster]:
    """Project unified execution failures into the same cluster table."""

    from apps.executions.failure_intelligence import classify_failure
    from apps.executions.models import ExecutionStepResult
    from apps.projects.models import Project

    rows = list(ExecutionStepResult.objects.filter(
        attempt__job=job,
        status__in={'failed', 'blocked'},
    ).select_related('attempt'))
    clusters = []
    project = Project.objects.filter(pk=job.project_id).first()
    environment = str((job.environment_snapshot or {}).get('fingerprint') or (job.environment_snapshot or {}).get('name') or 'unknown')
    for row in rows:
        message = str(row.error_message or '')[:4000]
        category, subtype = classify_failure(message, row.error_type)
        signature = hashlib.sha256(json.dumps({
            'error_type': row.error_type,
            'message': message,
            'case_key': row.case_key,
        }, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:128]
        key = failure_cluster_key(
            source_type='EXECUTION',
            project_type='PROJECT',
            project_id=str(job.project_id),
            test_key=row.case_key,
            environment_fingerprint=environment,
            signature=signature,
        )
        cluster, _created = QualityFailureCluster.objects.get_or_create(
            cluster_key=key,
            defaults={
                'source_type': 'EXECUTION',
                'project_type': 'PROJECT',
                'project_id': str(job.project_id),
                'test_key': row.case_key,
                'environment_fingerprint': environment,
                'signature': signature,
                'taxonomy_code': category,
                'subtype': subtype,
                'first_seen_at': job.finished_at or job.created_at,
                'mapped_project': project,
            },
        )
        metadata = dict(cluster.metadata or {})
        job_ids = list(dict.fromkeys([*(metadata.get('execution_job_ids') or []), job.pk]))[-100:]
        cluster.taxonomy_code = category
        cluster.subtype = subtype
        cluster.last_seen_at = job.finished_at or job.created_at
        cluster.last_failure_run = None
        if project is not None:
            cluster.mapped_project = project
        cluster.occurrence_count = len(job_ids)
        cluster.metadata = redact_payload({
            **metadata,
            'execution_job_ids': job_ids,
            'last_message': message,
        })
        cluster.resolved_at = None
        cluster.save()
        clusters.append(cluster)

    passed_keys = set(ExecutionStepResult.objects.filter(
        attempt__job=job,
        status__in={'passed', 'succeeded', 'completed'},
    ).values_list('case_key', flat=True))
    if passed_keys:
        fixed = QualityFailureCluster.objects.filter(
            source_type='EXECUTION',
            project_type='PROJECT',
            project_id=str(job.project_id),
            test_key__in=passed_keys,
            resolved_at__isnull=True,
        )
        timestamp = job.finished_at or job.created_at or timezone.now()
        for cluster in fixed:
            cluster.resolved_at = timestamp
            cluster.metadata = redact_payload({**(cluster.metadata or {}), 'last_fixed_job_id': job.pk})
            cluster.save(update_fields=['resolved_at', 'metadata', 'updated_at'])
    return clusters


__all__ = ['failure_cluster_key', 'sync_failure_clusters', 'sync_execution_job_clusters']
