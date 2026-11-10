"""Deterministic failure classification and flaky context for execution jobs."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping

from .control_plane import final_step_results
from .models import ExecutionJob
from .result_events import redact_result_payload


_QI_SOURCE_TYPES = {'api': 'API', 'ui': 'UI', 'app': 'APP', 'performance': 'PERF'}


def classify_failure(message: str, error_type: str = '') -> tuple[str, str]:
    text = f'{error_type} {message}'.lower()
    if any(token in text for token in ('timeout', 'timed out', 'deadline exceeded')):
        return 'TIMEOUT', 'timeout'
    if any(token in text for token in ('connection refused', 'connection reset', 'dns', '502', '503', 'service unavailable')):
        return 'ENVIRONMENT', 'transport'
    if any(token in text for token in ('fixture', 'seed data', 'test data', 'data setup')):
        return 'DATA', 'fixture'
    if any(token in text for token in ('selenium', 'playwright', 'locator', 'element not found', 'selector')):
        return 'TEST', 'automation'
    if any(token in text for token in ('assertion', 'expected', 'actual', 'status code')):
        return 'PRODUCT', 'assertion'
    return 'UNKNOWN', 'unclassified'


def _signature(category: str, subtype: str, message: str) -> str:
    normalized = re.sub(r'\b[0-9a-f]{8,}\b', '<id>', str(message or '').lower())
    normalized = re.sub(r'\s+', ' ', normalized).strip()[:500]
    return hashlib.sha256(f'{category}|{subtype}|{normalized}'.encode()).hexdigest()[:32]


def _quality_environment(job: ExecutionJob) -> str:
    snapshot = getattr(job, 'environment_snapshot', {}) or {}
    if isinstance(snapshot, Mapping):
        return str(snapshot.get('environment_fingerprint') or snapshot.get('name') or 'unknown')[:128]
    return 'unknown'


def analyze_job_failures(job: ExecutionJob) -> dict:
    rows = final_step_results(project_id=job.project_id, job_id=job.pk).filter(
        status__in={'failed', 'blocked'},
    )
    clusters = defaultdict(lambda: {'signature': '', 'category': '', 'subtype': '', 'count': 0, 'cases': [], 'sample_message': ''})
    flaky_profiles = set()
    try:
        from apps.quality_intelligence.models import QualityTestProfile

        source_type = _QI_SOURCE_TYPES.get(str(getattr(job, 'source_type', '') or '').lower())
        if source_type:
            case_keys = rows.order_by().values('case_key').distinct()
            flaky_profiles = set(QualityTestProfile.objects.filter(
                source_type=source_type,
                project_type='PROJECT',
                project_id=str(job.project_id),
                environment_fingerprint=_quality_environment(job),
                test_key__in=case_keys,
                reliability_status='FLAKY',
            ).values_list('test_key', flat=True))
    except Exception:
        flaky_profiles = set()

    for row in rows.order_by('case_key', 'step_key').values(
        'case_key', 'error_message', 'error_type',
    ).iterator(chunk_size=1000):
        category, subtype = classify_failure(row['error_message'], row['error_type'])
        signature = _signature(category, subtype, row['error_message'])
        cluster = clusters[signature]
        cluster['signature'] = signature
        cluster['category'] = category
        cluster['subtype'] = subtype
        cluster['count'] += 1
        if len(cluster['cases']) < 20:
            cluster['cases'].append(row['case_key'])
        if not cluster['sample_message']:
            cluster['sample_message'] = row['error_message'][:1000]

    result = {
        'job_id': job.pk,
        'failure_count': sum(item['count'] for item in clusters.values()),
        'clusters': sorted(clusters.values(), key=lambda item: (-item['count'], item['signature'])),
        'flaky_cases': sorted(flaky_profiles),
        'classified': bool(clusters),
    }
    return redact_result_payload(result)
