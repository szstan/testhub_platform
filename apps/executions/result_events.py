"""Result event ingestion for the unified execution history."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    ExecutionArtifact,
    ExecutionAttempt,
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

logger = logging.getLogger(__name__)

_SECRET_KEY = re.compile(r"(token|secret|password|api[_-]?key|authorization|cookie|private[_-]?key)", re.I)
_SECRET_TEXT_PREFIX = r"[\"']?(?:password|token|secret|api[_-]?key|authorization|cookie|private[_-]?key)[\"']?\s*[:=]\s*"
_QUOTED_SECRET = re.compile(r"(?i)(?P<prefix>" + _SECRET_TEXT_PREFIX + r")(?P<quote>[\"'])(?P<value>.*?)(?P=quote)")
_PLAIN_SECRET = re.compile(r"(?i)(?P<prefix>" + _SECRET_TEXT_PREFIX + r")(?P<value>[^\"'\s,;&}]+)")
_AUTH_CREDENTIAL = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")
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


def redact_result_text(value: Any) -> str:
    """Redact credentials embedded in free text, query strings, and URLs."""
    text = str(value if value is not None else '')[:8192]
    text = _URL_USERINFO.sub(lambda match: match.group(1) + '[REDACTED]@', text)
    text = _AUTH_CREDENTIAL.sub(lambda match: match.group(1) + ' [REDACTED]', text)
    text = _QUOTED_SECRET.sub(
        lambda match: match.group('prefix') + match.group('quote') + '[REDACTED]' + match.group('quote'),
        text,
    )
    return _PLAIN_SECRET.sub(lambda match: match.group('prefix') + '[REDACTED]', text)


def redact_result_payload(value: Any, *, depth: int = 0) -> Any:
    """Bound and redact event payloads before they are retained."""
    if depth > 8:
        return '[TRUNCATED]'
    if isinstance(value, Mapping):
        return {
            str(key): '[REDACTED]' if _SECRET_KEY.search(str(key)) else redact_result_payload(child, depth=depth + 1)
            for key, child in list(value.items())[:500]
        }
    if isinstance(value, (list, tuple)):
        return [redact_result_payload(child, depth=depth + 1) for child in value[:500]]
    if isinstance(value, str):
        return redact_result_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _event_key(
    job_id: int,
    attempt_number: int,
    event_type: str,
    raw_key: str,
    payload: Mapping[str, Any],
) -> str:
    identity = {
        'job': job_id,
        'attempt': attempt_number,
        'event_type': event_type,
        'raw_key': raw_key,
        'payload': payload,
    }
    source = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str, allow_nan=False)
    return hashlib.sha256(source.encode('utf-8')).hexdigest()


def _storage_event_key(job_id: int, attempt_number: int, event_type: str, raw_key: str, digest: str) -> str:
    prefix = f'event:{job_id}:{attempt_number}:{event_type}:'
    if not raw_key:
        return prefix + 'sha256:' + digest
    raw_prefix = prefix + 'raw:'
    safe_key = redact_result_text(raw_key)
    if safe_key == raw_key and len(raw_prefix) + len(safe_key) <= 128:
        return raw_prefix + safe_key
    return prefix + 'sha256:' + hashlib.sha256(raw_key.encode('utf-8')).hexdigest()


def _validate_attempt_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_147_483_647:
        raise ResultEventError('attempt must be a positive integer no greater than 2147483647')
    return value


def _validate_artifact_payload(payload: Mapping[str, Any]) -> None:
    uri = payload.get('uri')
    if not isinstance(uri, str) or not uri.strip():
        raise ResultEventError('artifact_uploaded requires uri')
    metadata = payload.get('metadata', {})
    if not isinstance(metadata, Mapping):
        raise ResultEventError('artifact metadata must be an object')
    size_bytes = payload.get('size_bytes')
    if size_bytes is not None and (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 <= size_bytes <= 9_223_372_036_854_775_807
    ):
        raise ResultEventError('artifact size_bytes must be a non-negative bounded integer')
    for key in ('artifact_idempotency_key', 'idempotency_key'):
        if key in payload and not isinstance(payload[key], str):
            raise ResultEventError(f'{key} must be a string')


def _validate_step_payload(payload: Any) -> tuple[str, str, str]:
    if not isinstance(payload, Mapping):
        raise ResultEventError('step payload must be an object')
    if 'result' in payload and not isinstance(payload['result'], Mapping):
        raise ResultEventError('step result must be an object')
    step_status = _step_status(payload.get('status') or StepStatus.COMPLETED)
    if step_status not in set(StepStatus.values):
        raise ResultEventError(f'Unknown step status: {step_status}')
    duration = payload.get('duration_ms')
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 0 <= duration <= 2_147_483_647
    ):
        raise ResultEventError('duration_ms must be a non-negative bounded integer')

    case_key = str(payload.get('case_key') or 'default')
    step_key = str(payload.get('step_key') or 'step')
    if (
        len(case_key) > 255
        or len(step_key) > 255
        or len(redact_result_text(case_key)) > 255
        or len(redact_result_text(step_key)) > 255
    ):
        raise ResultEventError('case_key and step_key must be no longer than 255 characters')
    supplied_key = payload.get('step_idempotency_key') or payload.get('idempotency_key')
    for key in ('step_idempotency_key', 'idempotency_key'):
        if key in payload and payload[key] is not None and not isinstance(payload[key], str):
            raise ResultEventError(f'{key} must be a string')
    return case_key, step_key, supplied_key or ''


def _validate_payload(event_type: str, payload: Any) -> tuple[dict[str, Any], int]:
    if not isinstance(payload, Mapping):
        raise ResultEventError('payload must be an object')
    if 'attempt' not in payload:
        raise ResultEventError('attempt is required')
    attempt_number = _validate_attempt_number(payload['attempt'])
    for key in ('result', 'summary'):
        if key in payload and not isinstance(payload[key], Mapping):
            raise ResultEventError(f'{key} must be an object')
    if event_type == 'run_finished':
        if 'status' not in payload:
            raise ResultEventError('run_finished requires a terminal status')
        target = _status(payload['status'])
        if target not in _TERMINAL_JOB:
            raise ResultEventError('run_finished status must be terminal')
    if event_type == 'step_finished':
        _validate_step_payload(payload)
    if event_type == 'artifact_uploaded':
        _validate_artifact_payload(payload)
    return dict(redact_result_payload(payload)), attempt_number


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


def _is_current_attempt(job: ExecutionJob, attempt: ExecutionAttempt, claim_id: str | None) -> bool:
    from .worker_leases import is_current_attempt

    return is_current_attempt(job, attempt, claim_id=claim_id)


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
        worker_claim_id: str | None = None,
    ) -> EventReceipt:
        if not isinstance(event_type, str) or event_type not in cls.allowed_events:
            raise ResultEventError(f'Unsupported execution event: {event_type}')
        safe_payload, supplied_attempt_number = _validate_payload(event_type, payload)
        raw_key = str(idempotency_key or '').strip()
        occurred_at = occurred_at or timezone.now()

        with transaction.atomic():
            locked_job = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            attempt_number = supplied_attempt_number
            attempt_number = _validate_attempt_number(attempt_number)
            event_key = _event_key(locked_job.pk, attempt_number, event_type, raw_key, safe_payload)
            storage_key = _storage_event_key(locked_job.pk, attempt_number, event_type, raw_key, event_key)
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
            key_reuse = ExecutionEvent.objects.filter(idempotency_key=storage_key).first()
            if key_reuse is not None:
                raise ResultEventError('event idempotency key was reused with different content')

            attempt = ExecutionAttempt.objects.select_related('variant').filter(
                job=locked_job,
                attempt_number=attempt_number,
            ).first()
            if locked_job.dataset_id:
                if attempt is None or attempt.variant_id is None or attempt.variant.job_id != locked_job.pk:
                    raise ResultEventError('dataset execution event requires an existing variant attempt')
            elif attempt is None and worker_claim_id is None:
                latest_number = locked_job.attempts.order_by('-attempt_number').values_list('attempt_number', flat=True).first() or 0
                may_create_first = latest_number == 0 and attempt_number == 1
                if not locked_job.final_attempt_id and not locked_job.is_terminal and may_create_first:
                    attempt = ExecutionAttempt.objects.create(job=locked_job, attempt_number=attempt_number)
            if attempt is None and not locked_job.final_attempt_id and not locked_job.is_terminal:
                raise ResultEventError('attempt does not exist; create retries through the execution lifecycle')

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

            is_current = attempt is not None and _is_current_attempt(locked_job, attempt, worker_claim_id)
            if event_type == 'artifact_uploaded' and attempt is not None:
                cls._record_artifact(locked_job, attempt, safe_payload)
            if is_current:
                if locked_job.attempt < attempt_number:
                    locked_job.attempt = attempt_number
                    locked_job.save(update_fields=['attempt', 'updated_at'])
                if event_type == 'run_started':
                    cls._start_job(locked_job, attempt)
                elif event_type == 'step_finished':
                    cls._record_step(attempt, safe_payload)
                elif event_type == 'run_finished':
                    if attempt.variant_id:
                        from .adapters import get_executor_adapter

                        get_executor_adapter(locked_job.source_type).collect_variant_result(
                            locked_job,
                            attempt.variant,
                            safe_payload,
                            attempt=attempt,
                        )
                        locked_job.refresh_from_db()
                    else:
                        cls._finish_job(locked_job, attempt, safe_payload)

            return EventReceipt(
                event_id=event.pk,
                created=True,
                job_id=locked_job.pk,
                attempt_id=attempt.pk if attempt is not None else None,
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
        if attempt.variant_id and attempt.variant.status not in _TERMINAL_JOB:
            attempt.variant.status = ExecutionStatus.RUNNING
            if attempt.variant.started_at is None:
                attempt.variant.started_at = timezone.now()
            attempt.variant.save(update_fields=['status', 'started_at', 'updated_at'])

    @staticmethod
    def _record_step(attempt, payload):
        case_key, step_key, supplied_key = _validate_step_payload(payload)
        safe_payload = redact_result_payload(payload)
        safe_case_key = str(safe_payload.get('case_key') or 'default')
        safe_step_key = str(safe_payload.get('step_key') or 'step')
        identity = json.dumps(
            {
                'attempt': attempt.pk,
                'case_key': case_key,
                'step_key': step_key,
                'idempotency_key': supplied_key,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            default=str,
        )
        step_idempotency_key = hashlib.sha256(('execution-step-v1:' + identity).encode('utf-8')).hexdigest()
        step, _created = ExecutionStepResult.objects.get_or_create(
            attempt=attempt,
            case_key=safe_case_key,
            step_key=safe_step_key,
            defaults={'idempotency_key': step_idempotency_key},
        )
        if step.status in {StepStatus.PASSED, StepStatus.SUCCEEDED, StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.SKIPPED}:
            return step
        step.status = _step_status(safe_payload.get('status') or StepStatus.COMPLETED)
        step.result = dict(safe_payload.get('result') or {})
        step.error_type = _error_type(safe_payload.get('error_type'))
        step.error_message = redact_result_text(safe_payload.get('error_message') or '')[:4000]
        step.duration_ms = safe_payload.get('duration_ms')
        step.save(update_fields=['status', 'result', 'error_type', 'error_message', 'duration_ms', 'updated_at'])
        return step

    @staticmethod
    def _record_artifact(job, attempt, payload):
        if not isinstance(payload, Mapping):
            raise ResultEventError('artifact payload must be an object')
        _validate_artifact_payload(payload)
        safe_payload = redact_result_payload(payload)
        uri = safe_payload['uri'].strip()
        raw_key = str(
            payload.get('artifact_idempotency_key')
            or payload.get('idempotency_key')
            or payload.get('uri')
            or ''
        )
        identity = json.dumps(
            {'job': job.pk, 'attempt': attempt.pk, 'key': raw_key},
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        key = hashlib.sha256(('execution-artifact-v1:' + identity).encode('utf-8')).hexdigest()
        values = {
            'job': job,
            'attempt': attempt,
            'uri': uri,
            'content_hash': str(safe_payload.get('content_hash') or ''),
            'media_type': str(safe_payload.get('media_type') or ''),
            'size_bytes': safe_payload.get('size_bytes'),
            'retention_policy': str(safe_payload.get('retention_policy') or 'default'),
            'metadata': dict(safe_payload.get('metadata') or {}),
        }

        def get_existing():
            return ExecutionArtifact.objects.filter(idempotency_key=key).first()

        def assert_same(existing):
            same = (
                existing.job_id == job.pk
                and existing.attempt_id == attempt.pk
                and existing.uri == values['uri']
                and existing.content_hash == values['content_hash']
                and existing.media_type == values['media_type']
                and existing.size_bytes == values['size_bytes']
                and existing.retention_policy == values['retention_policy']
                and existing.metadata == values['metadata']
            )
            if not same:
                raise ResultEventError('artifact idempotency key was reused with different content')
            return existing

        existing = get_existing()
        if existing is not None:
            return assert_same(existing)
        try:
            with transaction.atomic():
                return ExecutionArtifact.objects.create(idempotency_key=key, **values)
        except IntegrityError as exc:
            existing = get_existing()
            if existing is not None:
                return assert_same(existing)
            raise ResultEventError('artifact conflicts with an existing attempt artifact') from exc

    @staticmethod
    def _finish_job(job, attempt, payload):
        if job.final_attempt_id:
            return
        target = _status(payload.get('status', ExecutionStatus.COMPLETED))
        if target not in _TERMINAL_JOB:
            raise ResultEventError(f'run_finished status must be terminal: {target}')
        if job.status == ExecutionStatus.CANCEL_REQUESTED or attempt.status == AttemptStatus.CANCEL_REQUESTED:
            target = ExecutionStatus.CANCELLED
        result = dict(payload.get('result') or payload.get('summary') or {})
        error_type = _error_type(payload.get('error_type')) if target in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED} else ''
        error_message = redact_result_text(payload.get('error_message') or '')[:4000]
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
        if getattr(settings, 'QUALITY_INTELLIGENCE_ENABLED', False):
            try:
                from apps.quality_intelligence.services.clusters import sync_execution_job_clusters
                sync_execution_job_clusters(job)
            except Exception:  # noqa: BLE001 - quality projection must not break execution finalization
                logger.exception('execution failure cluster projection failed: job=%s', job.pk)

    @staticmethod
    def _sync_legacy_report(job, payload):
        """Mirror only the immutable final snapshot into the legacy report."""
        if not job.requested_by_id or not job.final_attempt_id:
            return
        from apps.reports.models import TestReport
        result = redact_result_payload(job.final_result or {})
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
