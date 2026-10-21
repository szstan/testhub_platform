"""Result event ingestion for the unified execution history."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from django.db import transaction
from django.utils import timezone

from .models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionJob,
    ExecutionStepResult,
)
from .state_machine import (
    AttemptStatus,
    ErrorType,
    ExecutionStatus,
    StepStatus,
)
from .policies import apply_quality_gate, clear_final_for_retry, create_retry_request, mark_retry_requested, preserve_final_result, resolve_retry_policy, should_retry, sync_quality_result

_SECRET_KEY = re.compile(r"(token|secret|password|api[_-]?key|authorization|cookie|private[_-]?key)", re.I)
_TERMINAL_JOB = {
    ExecutionStatus.CANCELLED,
    ExecutionStatus.PASSED,
    ExecutionStatus.SUCCEEDED,
    ExecutionStatus.COMPLETED,
    ExecutionStatus.FAILED,
    ExecutionStatus.BLOCKED,
    ExecutionStatus.UNSUPPORTED,
}


class ResultEventError(ValueError):
    """Raised for malformed or unsupported result events."""


@dataclass(frozen=True)
class EventReceipt:
    event_id: int
    created: bool
    job_id: int
    attempt_id: int | None
    event_type: str
    status: str


def redact_result_payload(value: Any, *, depth: int = 0) -> Any:
    """Bound and redact event payloads before they are retained."""
    if depth > 8:
        return '[TRUNCATED]'
    if isinstance(value, Mapping):
        return {
            str(key): '[REDACTED]' if _SECRET_KEY.search(str(key)) else redact_result_payload(child, depth=depth + 1)
            for key, child in list(value.items())[:500]
        }
    if isinstance(value, list):
        return [redact_result_payload(child, depth=depth + 1) for child in value[:500]]
    if isinstance(value, str):
        return value[:8192]
    return value


def _event_key(job_id: int, event_type: str, payload: Mapping[str, Any], idempotency_key: str) -> str:
    source = f"{job_id}|{idempotency_key}" if idempotency_key else '|'.join(
        [str(job_id), event_type, str(payload.get('attempt', '')), str(payload.get('case_key', '')), str(payload.get('step_key', ''))]
    )
    return hashlib.sha256(source.encode('utf-8')).hexdigest()


def _status(value: Any) -> str:
    raw = str(value or '').strip().lower()
    return {
        'success': ExecutionStatus.SUCCEEDED,
        'passed': ExecutionStatus.PASSED,
        'complete': ExecutionStatus.COMPLETED,
        'completed': ExecutionStatus.COMPLETED,
        'error': ExecutionStatus.FAILED,
        'failure': ExecutionStatus.FAILED,
        'stopped': ExecutionStatus.CANCELLED,
        'skipped': ExecutionStatus.UNSUPPORTED,
    }.get(raw, raw)


def _step_status(value: Any) -> str:
    raw = str(value or '').strip().lower()
    return {
        'success': StepStatus.SUCCEEDED,
        'passed': StepStatus.PASSED,
        'complete': StepStatus.COMPLETED,
        'completed': StepStatus.COMPLETED,
        'error': StepStatus.FAILED,
        'failure': StepStatus.FAILED,
        'skipped': StepStatus.SKIPPED,
    }.get(raw, raw)


def _error_type(value: Any) -> str:
    raw = str(value or '').strip().lower()
    return raw if raw in {choice for choice, _label in ErrorType.choices} else ErrorType.UNKNOWN


class ResultEventIngestor:
    allowed_events = {choice for choice, _label in ExecutionEvent.EVENT_TYPES}

    @classmethod
    def ingest(
        cls,
        job: ExecutionJob,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str = '',
        occurred_at: datetime | None = None,
    ) -> EventReceipt:
        if event_type not in cls.allowed_events:
            raise ResultEventError(f'Unsupported execution event: {event_type}')
        payload = dict(payload or {})
        safe_payload = redact_result_payload(payload)
        raw_key = (idempotency_key or '').strip()
        event_key = _event_key(job.pk, event_type, safe_payload, raw_key)
        storage_key = (f"{job.pk}:{raw_key}" if raw_key else event_key)[:128]
        occurred_at = occurred_at or timezone.now()

        with transaction.atomic():
            locked_job = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            existing = ExecutionEvent.objects.filter(event_key=event_key).first()
            if existing is not None:
                return EventReceipt(
                    event_id=existing.pk,
                    created=False,
                    job_id=locked_job.pk,
                    attempt_id=existing.attempt_id,
                    event_type=existing.event_type,
                    status=locked_job.status,
                )

            attempt_number = int(safe_payload.get('attempt') or locked_job.attempt or 1)
            attempt, _attempt_created = locked_job.create_attempt(attempt_number=attempt_number)
            event = ExecutionEvent.objects.create(
                job=locked_job,
                attempt=attempt,
                event_type=event_type,
                event_key=event_key,
                idempotency_key=storage_key,
                case_key=str(safe_payload.get('case_key') or ''),
                step_key=str(safe_payload.get('step_key') or ''),
                status=str(safe_payload.get('status') or ''),
                payload=safe_payload,
                occurred_at=occurred_at,
            )

            if event_type == 'run_started':
                cls._start_job(locked_job, attempt)
            elif event_type == 'step_finished':
                cls._record_step(attempt, safe_payload)
            elif event_type == 'artifact_uploaded':
                cls._record_artifact(locked_job, attempt, safe_payload)
            elif event_type == 'run_finished':
                cls._finish_job(locked_job, attempt, safe_payload)

            return EventReceipt(
                event_id=event.pk,
                created=True,
                job_id=locked_job.pk,
                attempt_id=attempt.pk,
                event_type=event_type,
                status=locked_job.status,
            )

    @staticmethod
    def _start_job(job, attempt):
        if job.status == ExecutionStatus.PENDING:
            job.transition_to(ExecutionStatus.QUEUED)
        if job.status == ExecutionStatus.QUEUED:
            job.transition_to(ExecutionStatus.RUNNING)
        if attempt.status in {AttemptStatus.PENDING, AttemptStatus.QUEUED}:
            attempt.transition_to(AttemptStatus.RUNNING)

    @staticmethod
    def _record_step(attempt, payload):
        case_key = str(payload.get('case_key') or 'default')
        step_key = str(payload.get('step_key') or 'step')
        step, _created = ExecutionStepResult.objects.get_or_create(
            attempt=attempt,
            case_key=case_key,
            step_key=step_key,
            defaults={'idempotency_key': str(payload.get('step_idempotency_key') or f'{attempt.pk}:{case_key}:{step_key}')[:128]},
        )
        if step.status in {StepStatus.PASSED, StepStatus.SUCCEEDED, StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.SKIPPED}:
            return step
        step.status = _step_status(payload.get('status') or StepStatus.COMPLETED)
        step.result = dict(payload.get('result') or {})
        step.error_type = _error_type(payload.get('error_type'))
        step.error_message = str(payload.get('error_message') or '')[:4000]
        step.duration_ms = payload.get('duration_ms')
        step.save(update_fields=['status', 'result', 'error_type', 'error_message', 'duration_ms', 'updated_at'])
        return step

    @staticmethod
    def _record_artifact(job, attempt, payload):
        uri = str(payload.get('uri') or '').strip()
        if not uri:
            raise ResultEventError('artifact_uploaded requires uri')
        key = str(payload.get('artifact_idempotency_key') or f'{attempt.pk}:{uri}')[:128]
        return ExecutionArtifact.objects.get_or_create(
            idempotency_key=key,
            defaults={
                'job': job,
                'attempt': attempt,
                'uri': uri,
                'content_hash': str(payload.get('content_hash') or ''),
                'media_type': str(payload.get('media_type') or ''),
                'size_bytes': payload.get('size_bytes'),
                'retention_policy': str(payload.get('retention_policy') or 'default'),
                'metadata': dict(payload.get('metadata') or {}),
            },
        )[0]

    @staticmethod
    def _finish_job(job, attempt, payload):
        if job.final_attempt_id:
            return
        target = _status(payload.get('status') or ExecutionStatus.COMPLETED)
        if target not in set(ExecutionStatus.values):
            raise ResultEventError(f'Unknown terminal status: {target}')
        result = dict(payload.get('result') or payload.get('summary') or {})
        error_type = _error_type(payload.get('error_type')) if target in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED} else ''
        error_message = str(payload.get('error_message') or '')[:4000]
        gate_result = apply_quality_gate(job, result, stage='result', attempt=attempt)
        if target in {ExecutionStatus.PASSED, ExecutionStatus.SUCCEEDED, ExecutionStatus.COMPLETED}:
            if gate_result.get('configured') and not gate_result.get('passed'):
                target = ExecutionStatus.BLOCKED
                error_type = ErrorType.CONFIGURATION
                error_message = 'Quality gate failed'
        attempt.result = result
        attempt.error_type = error_type
        attempt.error_message = error_message
        attempt.save(update_fields=['result', 'error_type', 'error_message', 'updated_at'])
        if attempt.status == AttemptStatus.PENDING:
            attempt.transition_to(AttemptStatus.QUEUED)
            attempt.transition_to(AttemptStatus.RUNNING)
        if job.status == ExecutionStatus.PENDING:
            job.transition_to(ExecutionStatus.QUEUED)
            job.transition_to(ExecutionStatus.RUNNING)
        elif job.status == ExecutionStatus.QUEUED:
            job.transition_to(ExecutionStatus.RUNNING)
        if attempt.status not in _TERMINAL_JOB:
            attempt.transition_to(target)
        job.result = result
        job.error_type = attempt.error_type
        job.error_message = attempt.error_message
        job.save(update_fields=['result', 'error_type', 'error_message', 'updated_at'])
        if job.status not in _TERMINAL_JOB:
            job.transition_to(target)
        sync_quality_result(job, attempt, target, result)

        decision = should_retry(job, attempt, automatic=True)
        if target in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED, ExecutionStatus.UNSUPPORTED, ExecutionStatus.CANCELLED} and decision.allowed:
            policy = resolve_retry_policy(job)
            clear_final_for_retry(job)
            mark_retry_requested(
                attempt,
                'automatic retry: ' + decision.reason,
                policy=policy,
                decision=decision.reason,
                automatic=True,
            )
            job.transition_to(ExecutionStatus.QUEUED, reason='automatic retry')
            next_attempt, _created = job.create_attempt()
            next_attempt.transition_to(AttemptStatus.QUEUED)
            create_retry_request(
                job,
                attempt,
                next_attempt,
                policy,
                'automatic retry: ' + decision.reason,
            )
        else:
            preserve_final_result(job, attempt, target, result, gate_result)
        ResultEventIngestor._sync_legacy_report(job, payload)


    @staticmethod
    def _sync_legacy_report(job, payload):
        """Mirror only the immutable final snapshot into the legacy report."""
        if not job.requested_by_id or not job.final_attempt_id:
            return
        from apps.reports.models import TestReport
        result = dict(job.final_result or {})
        TestReport.objects.update_or_create(
            execution_job=job,
            defaults={
                'project': job.project,
                'name': f'Execution job #{job.pk}',
                'report_type': 'execution',
                'summary': result,
                'content': {
                    'execution_job_id': job.pk,
                    'attempt': job.final_attempt_id,
                    'source_type': job.source_type,
                    'source_id': job.source_id,
                    'final_status': job.final_status,
                    'environment_snapshot': redact_result_payload(job.environment_snapshot),
                },
                'generated_by': job.requested_by,
            },
        )
