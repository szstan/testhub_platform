"""Deterministic failure classification and flaky context for execution jobs."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .models import ExecutionJob, ExecutionStepResult
from .result_events import redact_result_payload


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


def analyze_job_failures(job: ExecutionJob) -> dict:
    attempt_id = job.final_attempt_id or job.attempts.filter(is_final=True).order_by('-attempt_number').values_list('id', flat=True).first()
    rows = ExecutionStepResult.objects.filter(attempt_id=attempt_id, status__in={'failed', 'blocked'}) if attempt_id else ExecutionStepResult.objects.none()
    clusters = defaultdict(lambda: {'signature': '', 'category': '', 'subtype': '', 'count': 0, 'cases': [], 'sample_message': ''})
    flaky_cases = []
    try:
        from apps.quality_intelligence.models import QualityTestProfile
        profiles = {
            profile.test_key: profile.reliability_status
            for profile in QualityTestProfile.objects.filter(
                project_id=str(job.project_id),
                test_key__in=list(rows.values_list('case_key', flat=True)),
            )
        }
    except Exception:
        profiles = {}

    for row in rows:
        category, subtype = classify_failure(row.error_message, row.error_type)
        signature = _signature(category, subtype, row.error_message)
        cluster = clusters[signature]
        cluster['signature'] = signature
        cluster['category'] = category
        cluster['subtype'] = subtype
        cluster['count'] += 1
        if len(cluster['cases']) < 20:
            cluster['cases'].append(row.case_key)
        if not cluster['sample_message']:
            cluster['sample_message'] = row.error_message[:1000]
        if profiles.get(row.case_key) == 'FLAKY' and row.case_key not in flaky_cases:
            flaky_cases.append(row.case_key)

    result = {
        'job_id': job.pk,
        'failure_count': sum(item['count'] for item in clusters.values()),
        'clusters': sorted(clusters.values(), key=lambda item: (-item['count'], item['signature'])),
        'flaky_cases': sorted(flaky_cases),
        'classified': bool(clusters),
    }
    return redact_result_payload(result)
