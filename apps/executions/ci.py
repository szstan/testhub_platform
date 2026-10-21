"""Signed CI webhook handling and execution-profile helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ExecutionBuild, ExecutionJob, ExecutionProfile, WebhookEvent
from .result_events import redact_result_payload
from .policies import apply_quality_gate


class WebhookVerificationError(ValueError):
    """Raised when an inbound CI webhook cannot be trusted."""


@dataclass(frozen=True)
class WebhookReceipt:
    event_id: int
    created: bool
    job_id: int | None
    status: str


def _secret_for_profile(profile: ExecutionProfile) -> str:
    ref = (profile.webhook_secret_ref or '').strip()
    env_name = ref or 'TESTHUB_CI_WEBHOOK_SECRET'
    return os.environ.get(env_name, '')


def _header(request, *names):
    for name in names:
        value = request.headers.get(name)
        if value:
            return value.strip()
    return ''


def verify_webhook_request(request, profile: ExecutionProfile, provider: str, *, max_skew=300):
    secret = _secret_for_profile(profile)
    if not secret:
        raise WebhookVerificationError('Webhook secret is not configured for this profile')
    event_id = _header(request, 'X-TestHub-Event', 'X-GitHub-Delivery', 'X-Gitlab-Event-UUID', 'X-Request-ID')
    if not event_id:
        raise WebhookVerificationError('Webhook event id is required')
    raw_body = request.body or b''
    if provider == 'github':
        signature = _header(request, 'X-Hub-Signature-256', 'X-TestHub-Signature')
        expected = 'sha256=' + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    else:
        timestamp = _header(request, 'X-TestHub-Timestamp')
        if not timestamp:
            raise WebhookVerificationError('Webhook timestamp is required')
        try:
            if abs(int(time.time()) - int(timestamp)) > max_skew:
                raise WebhookVerificationError('Webhook timestamp is outside the allowed window')
        except ValueError as exc:
            raise WebhookVerificationError('Webhook timestamp is invalid') from exc
        signature = _header(request, 'X-TestHub-Signature')
        expected = hmac.new(secret.encode(), f'{timestamp}.'.encode() + raw_body, hashlib.sha256).hexdigest()
    supplied = signature.removeprefix('sha256=')
    expected_digest = expected.removeprefix('sha256=')
    if not hmac.compare_digest(supplied, expected_digest):
        raise WebhookVerificationError('Webhook signature is invalid')
    return event_id, hashlib.sha256(raw_body).hexdigest()


def normalize_ci_payload(provider: str, payload: Mapping[str, Any], event_id: str) -> dict[str, str]:
    if provider == 'github':
        workflow = payload.get('workflow_run') or {}
        commit_sha = str(workflow.get('head_sha') or payload.get('after') or '')
        branch = str(workflow.get('head_branch') or payload.get('ref') or '')
        build_number = str(workflow.get('run_number') or workflow.get('id') or event_id)
        artifact_url = str(workflow.get('html_url') or '')
    elif provider == 'gitlab':
        attrs = payload.get('object_attributes') or {}
        commit_sha = str(payload.get('checkout_sha') or payload.get('after_sha') or '')
        branch = str(payload.get('ref') or attrs.get('ref') or '')
        build_number = str(payload.get('build_id') or attrs.get('id') or event_id)
        artifact_url = str(payload.get('build_url') or '')
    elif provider == 'jenkins':
        build = payload.get('build') or payload
        commit_sha = str(payload.get('commit_sha') or payload.get('sha1') or '')
        branch = str(payload.get('branch') or payload.get('branchName') or '')
        build_number = str(build.get('number') or payload.get('build_number') or event_id)
        artifact_url = str(build.get('full_url') or payload.get('url') or '')
    else:
        commit_sha = str(payload.get('commit_sha') or payload.get('sha') or '')
        branch = str(payload.get('branch') or payload.get('ref') or '')
        build_number = str(payload.get('build_number') or event_id)
        artifact_url = str(payload.get('artifact_url') or '')
    return {
        'build_number': build_number[:128],
        'commit_sha': commit_sha[:128],
        'branch': branch[:255],
        'artifact_url': artifact_url[:2048],
    }


def process_webhook(profile: ExecutionProfile, provider: str, event_id: str, payload: Mapping[str, Any], payload_hash: str) -> WebhookReceipt:
    normalized = normalize_ci_payload(provider, payload, event_id)
    safe_summary = redact_result_payload({
        'provider': provider,
        'event_id': event_id,
        'commit_sha': normalized['commit_sha'],
        'branch': normalized['branch'],
        'build_number': normalized['build_number'],
    })
    with transaction.atomic():
        existing = WebhookEvent.objects.select_for_update().filter(
            profile=profile,
            external_event_id=event_id,
        ).first()
        if existing:
            return WebhookReceipt(existing.pk, False, existing.job_id, existing.status)
        build, _ = ExecutionBuild.objects.get_or_create(
            project=profile.project,
            build_number=normalized['build_number'],
            defaults={
                'version': profile.version,
                'name': f'{provider} {normalized["build_number"]}',
                'branch': normalized['branch'],
                'commit_sha': normalized['commit_sha'],
                'artifact_url': normalized['artifact_url'],
                'environment': str((profile.environment_snapshot or {}).get('name') or ''),
                'status': 'pending',
                'created_by': profile.created_by,
            },
        )
        job, _created = ExecutionJob.objects.create_idempotent(
            project=profile.project,
            idempotency_key=f'ci:{profile.pk}:{event_id}',
            requested_by=profile.created_by,
            defaults={
                'profile': profile,
                'source_type': profile.source_type,
                'source_id': event_id,
                'version': profile.version,
                'build': build,
                'build_reference': normalized['build_number'],
                'trigger': 'ci',
                'environment_snapshot': profile.environment_snapshot or {},
                'priority': 'normal',
            },
        )
        event_status = 'processed'
        event_error = ''
        quality_summary = payload.get('quality_summary') or payload.get('summary')
        if isinstance(quality_summary, Mapping) and (profile.quality_gate or {}):
            gate_result = apply_quality_gate(job, quality_summary, stage='webhook', evaluated_by=profile.created_by)
            if not gate_result.get('passed'):
                event_status = 'rejected'
                event_error = 'CI quality gate failed'
                job.error_type = 'configuration'
                job.error_message = event_error
                job.save(update_fields=['error_type', 'error_message', 'updated_at'])

        event = WebhookEvent.objects.create(
            profile=profile,
            provider=provider,
            external_event_id=event_id,
            payload_hash=payload_hash,
            payload_summary=safe_summary,
            status=event_status,
            job=job,
            error_message=event_error,
            processed_at=timezone.now() if event_status == 'processed' else None,
        )
    return WebhookReceipt(event.pk, True, job.pk, event.status)


def quality_gate_result(profile: ExecutionProfile, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate configured thresholds deterministically and fail closed.

    An absent profile or empty gate is intentionally compatible with the
    original helper. Once a gate is configured, malformed rules, missing
    metrics, and non-numeric values become explicit failures instead of being
    silently ignored.
    """
    gate = getattr(profile, 'quality_gate', {}) or {}
    if not isinstance(gate, Mapping):
        return {
            'passed': False,
            'failures': [{'metric': '__gate__', 'reason': 'invalid_gate_configuration'}],
        }
    failures = []
    summary = summary if isinstance(summary, Mapping) else {}
    for key, rule in gate.items():
        if not isinstance(rule, Mapping):
            failures.append({'metric': key, 'reason': 'invalid_rule'})
            continue
        if key not in summary:
            failures.append({'metric': key, 'reason': 'missing_metric'})
            continue
        actual = summary.get(key)
        try:
            actual = float(actual)
        except (TypeError, ValueError):
            failures.append({'metric': key, 'actual': actual, 'reason': 'invalid_metric'})
            continue
        if 'max' in rule:
            try:
                maximum = float(rule['max'])
            except (TypeError, ValueError):
                failures.append({'metric': key, 'reason': 'invalid_max'})
            else:
                if actual > maximum:
                    failures.append({'metric': key, 'actual': actual, 'max': rule['max']})
        if 'min' in rule:
            try:
                minimum = float(rule['min'])
            except (TypeError, ValueError):
                failures.append({'metric': key, 'reason': 'invalid_min'})
            else:
                if actual < minimum:
                    failures.append({'metric': key, 'actual': actual, 'min': rule['min']})
        if not any(bound in rule for bound in ('min', 'max')):
            failures.append({'metric': key, 'reason': 'missing_bounds'})
    return {'passed': not failures, 'failures': failures}
