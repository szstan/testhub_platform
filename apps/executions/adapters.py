"""Executor-neutral adapters for the unified execution domain.

Adapters deliberately keep the legacy module entry points behind a small
contract.  A caller may inject the existing Celery/thread/task callable as the
``dispatcher``; this module owns durable job/attempt state and result mapping,
not the implementation of an API/UI/APP/performance runner.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from django.db import transaction
from django.utils import timezone

from .models import ExecutionArtifact, ExecutionAttempt, ExecutionJob, ExecutionRetryRequest, ExecutionStepResult
from .result_events import redact_result_payload
from .state_machine import (
    AttemptStatus,
    ErrorType,
    ExecutionStatus,
    InvalidExecutionTransition,
    StepStatus,
)
from .policies import (
    apply_quality_gate,
    check_dispatch_governance,
    clear_final_for_retry,
    create_retry_request,
    mark_retry_requested,
    preserve_final_result,
    resolve_retry_policy,
    should_retry,
    sync_quality_result,
)


class AdapterError(RuntimeError):
    """Base error for an invalid or failed adapter operation."""


class UnsupportedExecutorError(AdapterError):
    """Raised when a source has no registered runtime dispatcher."""


class AdapterConflict(AdapterError):
    """Raised when an operation cannot be applied to the current lifecycle."""


@dataclass(frozen=True)
class AdapterReceipt:
    job_id: int
    attempt_id: int
    attempt_number: int
    status: str
    supported: bool
    dispatch_id: str = ""
    message: str = ""
    created_attempt: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)


_TERMINAL_SUCCESS = {
    ExecutionStatus.PASSED,
    ExecutionStatus.SUCCEEDED,
    ExecutionStatus.COMPLETED,
}
_TERMINAL_FAILURE = {
    ExecutionStatus.FAILED,
    ExecutionStatus.BLOCKED,
    ExecutionStatus.UNSUPPORTED,
    ExecutionStatus.CANCELLED,
}


def _normalise_job_status(value: Any) -> str:
    raw = getattr(value, "value", value)
    raw = str(raw or "").strip().lower()
    aliases = {
        "success": ExecutionStatus.SUCCEEDED,
        "passed": ExecutionStatus.PASSED,
        "complete": ExecutionStatus.COMPLETED,
        "completed": ExecutionStatus.COMPLETED,
        "error": ExecutionStatus.FAILED,
        "failure": ExecutionStatus.FAILED,
        "stopped": ExecutionStatus.CANCELLED,
        "skip": ExecutionStatus.UNSUPPORTED,
        "skipped": ExecutionStatus.UNSUPPORTED,
    }
    return aliases.get(raw, raw)


def _normalise_step_status(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "").strip().lower()
    aliases = {
        "success": StepStatus.SUCCEEDED,
        "passed": StepStatus.PASSED,
        "complete": StepStatus.COMPLETED,
        "completed": StepStatus.COMPLETED,
        "error": StepStatus.FAILED,
        "failure": StepStatus.FAILED,
        "skipped": StepStatus.SKIPPED,
    }
    return aliases.get(raw, raw)


def _error_type(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "").strip().lower()
    allowed = {choice for choice, _label in ErrorType.choices}
    return raw if raw in allowed else ErrorType.UNKNOWN


def _call_dispatcher(dispatcher, job, attempt, variant):
    """Call old two-argument dispatchers and new variant-aware ones safely."""
    try:
        parameters = inspect.signature(dispatcher).parameters.values()
        positional = [
            parameter for parameter in parameters
            if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        accepts_varargs = any(parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters)
    except (TypeError, ValueError):
        accepts_varargs = True
        positional = []
    if accepts_varargs or len(positional) >= 3:
        return dispatcher(job, attempt, variant)
    return dispatcher(job, attempt)


def _variant_aggregate(job):
    variants = list(job.variants.order_by('ordinal'))
    statuses = [str(variant.status) for variant in variants]
    active = {
        ExecutionStatus.PENDING,
        ExecutionStatus.QUEUED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCEL_REQUESTED,
    }
    if not variants or any(value in active for value in statuses):
        aggregate_status = ExecutionStatus.RUNNING if variants else ExecutionStatus.PENDING
    elif any(value == ExecutionStatus.BLOCKED for value in statuses):
        aggregate_status = ExecutionStatus.BLOCKED
    elif any(value == ExecutionStatus.FAILED for value in statuses):
        aggregate_status = ExecutionStatus.FAILED
    elif any(value == ExecutionStatus.CANCELLED for value in statuses):
        aggregate_status = ExecutionStatus.CANCELLED
    elif all(value == ExecutionStatus.UNSUPPORTED for value in statuses):
        aggregate_status = ExecutionStatus.UNSUPPORTED
    else:
        aggregate_status = ExecutionStatus.PASSED
    return aggregate_status, {
        'status': aggregate_status,
        'count': len(variants),
        'variants': [
            {
                'variant_key': variant.variant_key,
                'ordinal': variant.ordinal,
                'status': variant.status,
                'result': variant.result,
                'error_type': variant.error_type,
                'error_message': variant.error_message,
            }
            for variant in variants
        ],
    }


class ExecutorAdapter:
    """Small lifecycle contract shared by API/UI/APP/performance adapters."""

    source_type = "other"

    def validate(self, job: ExecutionJob) -> None:
        if job.source_type != self.source_type:
            raise AdapterError(
                f"Adapter {self.source_type!r} cannot handle source {job.source_type!r}"
            )
        if not job.project_id:
            raise AdapterError("Execution job must belong to a project")

    def _schedule_retry_dispatch(self, request_id: int, dispatcher, worker: str = "") -> None:
        if dispatcher is None:
            return

        def dispatch_after_commit():
            retry_request = ExecutionRetryRequest.objects.select_related('job', 'to_attempt').filter(pk=request_id).first()
            if retry_request is None or retry_request.status != 'queued' or retry_request.scheduled_at > timezone.now():
                return
            try:
                fresh_job = ExecutionJob.objects.get(pk=retry_request.job_id)
                self.submit(fresh_job, dispatcher=dispatcher, worker=worker, requested_by=fresh_job.requested_by)
                retry_request.status = 'dispatched'
                retry_request.dispatched_at = timezone.now()
                retry_request.save(update_fields=['status', 'dispatched_at', 'updated_at'])
            except Exception as exc:  # noqa: BLE001 - outbox retains failure
                retry_request.status = 'failed'
                retry_request.error_message = str(exc)[:4000]
                retry_request.save(update_fields=['status', 'error_message', 'updated_at'])

        transaction.on_commit(dispatch_after_commit)

    def _submit_dataset(
        self,
        job: ExecutionJob,
        *,
        dispatcher=None,
        worker='',
    ) -> AdapterReceipt:
        if not job.dataset_snapshot or not job.variants.exists():
            job.initialize_dataset_variants()

        with transaction.atomic():
            locked_job = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            if locked_job.is_terminal and locked_job.status not in _TERMINAL_FAILURE:
                raise AdapterConflict('A successful execution cannot be submitted again')
            variants = list(locked_job.variants.select_for_update().order_by('ordinal'))
            attempts = []
            for variant in variants:
                attempt = variant.attempts.order_by('-attempt_number').first()
                if attempt is None or attempt.is_terminal:
                    attempt, _created = locked_job.create_attempt(variant=variant, worker=worker)
                attempts.append((variant, attempt))
                if variant.status in _TERMINAL_FAILURE:
                    continue
                variant.status = AttemptStatus.QUEUED
                variant.save(update_fields=['status', 'updated_at'])
            if locked_job.status == ExecutionStatus.PENDING:
                locked_job.transition_to(ExecutionStatus.QUEUED)

        if dispatcher is None:
            message = f'No dispatcher is registered for source type {self.source_type}'
            for variant, attempt in attempts:
                if not attempt.is_terminal:
                    attempt.transition_to(AttemptStatus.UNSUPPORTED)
                variant.status = ExecutionStatus.UNSUPPORTED
                variant.error_type = ErrorType.UNSUPPORTED
                variant.error_message = message
                variant.finished_at = timezone.now()
                variant.save(update_fields=['status', 'error_type', 'error_message', 'finished_at', 'updated_at'])
            locked_job.refresh_from_db()
            locked_job.result = _variant_aggregate(locked_job)[1]
            locked_job.error_type = ErrorType.UNSUPPORTED
            locked_job.error_message = message
            locked_job.transition_to(ExecutionStatus.UNSUPPORTED, reason=message)
            locked_job.save(update_fields=['result', 'error_type', 'error_message', 'updated_at'])
            return AdapterReceipt(
                job_id=locked_job.pk,
                attempt_id=attempts[0][1].pk if attempts else 0,
                attempt_number=attempts[0][1].attempt_number if attempts else 0,
                status=locked_job.status,
                supported=False,
                message=message,
                created_attempt=True,
                details={'variant_count': len(attempts), 'variants': [variant.variant_key for variant, _ in attempts]},
            )

        if locked_job.status == ExecutionStatus.QUEUED:
            locked_job.transition_to(ExecutionStatus.RUNNING)
        dispatch_ids = []
        for variant, attempt in attempts:
            if variant.status in _TERMINAL_FAILURE:
                continue
            if attempt.status == AttemptStatus.QUEUED:
                attempt.transition_to(AttemptStatus.RUNNING)
            variant.status = ExecutionStatus.RUNNING
            variant.started_at = timezone.now()
            variant.save(update_fields=['status', 'started_at', 'updated_at'])
            try:
                dispatch_result = _call_dispatcher(dispatcher, locked_job, attempt, variant)
                if isinstance(dispatch_result, Mapping) and (
                    'status' in dispatch_result or 'result' in dispatch_result or 'summary' in dispatch_result
                ):
                    self.collect_variant_result(locked_job, variant, dispatch_result, attempt=attempt)
                    dispatch_id = dispatch_result.get('dispatch_id', '')
                else:
                    dispatch_id = str(dispatch_result or '')
                    variant.dispatch_id = dispatch_id
                    variant.save(update_fields=['dispatch_id', 'updated_at'])
                dispatch_ids.append(dispatch_id)
            except Exception as exc:  # noqa: BLE001 - preserve per-variant failure
                self.collect_variant_result(
                    locked_job,
                    variant,
                    {'status': ExecutionStatus.FAILED, 'error_type': ErrorType.INFRASTRUCTURE, 'error_message': str(exc)},
                    attempt=attempt,
                )
        locked_job.refresh_from_db()
        return AdapterReceipt(
            job_id=locked_job.pk,
            attempt_id=attempts[0][1].pk if attempts else 0,
            attempt_number=attempts[0][1].attempt_number if attempts else 0,
            status=locked_job.status,
            supported=True,
            dispatch_id=str(dispatch_ids[0] if dispatch_ids else ''),
            created_attempt=bool(attempts),
            details={
                'variant_count': len(attempts),
                'variants': [variant.variant_key for variant, _ in attempts],
                'dispatch_ids': dispatch_ids,
            },
        )

    def submit(
        self,
        job: ExecutionJob,
        *,
        dispatcher: Optional[Callable[[ExecutionJob, ExecutionAttempt], Any]] = None,
        worker: str = "",
        quarantine_override: bool = False,
        override_reason: str = "",
        requested_by=None,
    ) -> AdapterReceipt:
        """Create an attempt and optionally dispatch the legacy executor.

        ``dispatcher`` is injected by the module integration layer.  Without
        it, the request is closed as UNSUPPORTED instead of pretending that a
        pending job was executed.
        """
        self.validate(job)
        governance = check_dispatch_governance(
            job,
            override=quarantine_override,
            override_reason=override_reason,
            actor=requested_by or job.requested_by,
        )
        if not governance.get("allowed"):
            raise AdapterConflict(f"Dispatch denied: {governance.get('reason')}")
        if job.dataset_id:
            return self._submit_dataset(
                job,
                dispatcher=dispatcher,
                worker=worker,
            )
        with transaction.atomic():
            locked_job = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            if locked_job.is_terminal and locked_job.status not in _TERMINAL_FAILURE:
                raise AdapterConflict("A successful execution cannot be submitted again")
            latest_attempt = locked_job.attempts.order_by('-attempt_number').first()
            if (
                locked_job.status == ExecutionStatus.QUEUED
                and latest_attempt is not None
                and latest_attempt.status in {AttemptStatus.PENDING, AttemptStatus.QUEUED}
            ):
                attempt, created = latest_attempt, False
            else:
                attempt, created = locked_job.create_attempt(worker=worker)
            if locked_job.status == ExecutionStatus.PENDING:
                locked_job.transition_to(ExecutionStatus.QUEUED)
            if attempt.status == AttemptStatus.PENDING:
                attempt.transition_to(AttemptStatus.QUEUED)

        if dispatcher is None:
            message = f"No dispatcher is registered for source type {self.source_type}"
            attempt.transition_to(AttemptStatus.UNSUPPORTED)
            locked_job.transition_to(ExecutionStatus.UNSUPPORTED, reason=message)
            return AdapterReceipt(
                job_id=locked_job.pk,
                attempt_id=attempt.pk,
                attempt_number=attempt.attempt_number,
                status=locked_job.status,
                supported=False,
                message=message,
                created_attempt=created,
            )

        try:
            dispatch_id = dispatcher(locked_job, attempt)
        except Exception as exc:  # noqa: BLE001 - adapter boundary records failure
            message = str(exc)[:4000]
            attempt.error_type = ErrorType.INFRASTRUCTURE
            attempt.error_message = message
            attempt.save(update_fields=["error_type", "error_message", "updated_at"])
            attempt.transition_to(AttemptStatus.FAILED)
            locked_job.error_type = ErrorType.INFRASTRUCTURE
            locked_job.error_message = message
            locked_job.save(update_fields=["error_type", "error_message", "updated_at"])
            locked_job.transition_to(ExecutionStatus.FAILED)
            decision = should_retry(locked_job, attempt, automatic=True)
            retry_request_id = None
            if decision.allowed:
                policy = resolve_retry_policy(locked_job)
                clear_final_for_retry(locked_job)
                mark_retry_requested(attempt, "automatic retry: dispatcher failure", policy=policy, decision=decision.reason)
                locked_job.transition_to(ExecutionStatus.QUEUED, reason="automatic retry")
                next_attempt, _created = locked_job.create_attempt()
                next_attempt.transition_to(AttemptStatus.QUEUED)
                retry_request = create_retry_request(locked_job, attempt, next_attempt, policy, "automatic retry: dispatcher failure")
                retry_request_id = retry_request.pk
            if retry_request_id:
                self._schedule_retry_dispatch(retry_request_id, dispatcher, worker)
            return AdapterReceipt(
                job_id=locked_job.pk,
                attempt_id=attempt.pk,
                attempt_number=attempt.attempt_number,
                status=locked_job.status,
                supported=True,
                message=message,
                details={"automatic_retry": bool(retry_request_id), "retry_request_id": retry_request_id},
            )

        if attempt.status == AttemptStatus.QUEUED:
            attempt.transition_to(AttemptStatus.RUNNING)
        if locked_job.status == ExecutionStatus.QUEUED:
            locked_job.worker = worker or locked_job.worker
            locked_job.save(update_fields=["worker", "updated_at"])
            locked_job.transition_to(ExecutionStatus.RUNNING)
        return AdapterReceipt(
            job_id=locked_job.pk,
            attempt_id=attempt.pk,
            attempt_number=attempt.attempt_number,
            status=locked_job.status,
            supported=True,
            dispatch_id=str(dispatch_id or ""),
            created_attempt=created,
        )

    def cancel(
        self,
        job: ExecutionJob,
        *,
        reason: str = "",
        canceller: Optional[Callable[[ExecutionJob, ExecutionAttempt], Any]] = None,
    ) -> AdapterReceipt:
        """Request cancellation and invoke a legacy stop callback when supplied."""
        self.validate(job)
        with transaction.atomic():
            locked_job = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            if locked_job.status in _TERMINAL_SUCCESS:
                raise AdapterConflict("A successful execution cannot be cancelled")
            attempt = locked_job.attempts.order_by("-attempt_number").first()
            locked_job.request_cancel(reason)
            if attempt and attempt.status in {
                AttemptStatus.PENDING,
                AttemptStatus.QUEUED,
                AttemptStatus.RUNNING,
            }:
                attempt.transition_to(AttemptStatus.CANCEL_REQUESTED)

        if canceller and attempt:
            canceller(locked_job, attempt)
        return AdapterReceipt(
            job_id=locked_job.pk,
            attempt_id=attempt.pk if attempt else 0,
            attempt_number=attempt.attempt_number if attempt else 0,
            status=locked_job.status,
            supported=True,
            message=reason,
        )

    def retry(self, job: ExecutionJob, *, reason: str = "retry requested", requested_by=None, automatic: bool = False) -> AdapterReceipt:
        """Queue a new attempt with an auditable reason and policy check."""
        self.validate(job)
        with transaction.atomic():
            locked_job = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            if locked_job.status not in _TERMINAL_FAILURE:
                raise AdapterConflict("Only failed, blocked, cancelled or unsupported jobs can retry")
            previous = locked_job.attempts.order_by('-attempt_number').first()
            decision = should_retry(locked_job, previous, automatic=automatic)
            if not decision.allowed:
                raise AdapterConflict(f"Retry denied: {decision.reason}")
            policy = resolve_retry_policy(locked_job)
            if previous is not None:
                clear_final_for_retry(locked_job)
                mark_retry_requested(
                    previous,
                    reason,
                    requested_by,
                    policy=policy,
                    decision=decision.reason,
                    automatic=automatic,
                )
            locked_job.transition_to(ExecutionStatus.QUEUED, reason=str(reason or "retry requested")[:4000])
            attempt, created = locked_job.create_attempt()
            attempt.transition_to(AttemptStatus.QUEUED)
            retry_request = create_retry_request(
                locked_job,
                previous or attempt,
                attempt,
                policy,
                reason,
                automatic=automatic,
                requested_by=requested_by,
            )
        if getattr(self, 'dispatcher', None) is not None:
            self._schedule_retry_dispatch(retry_request.pk, self.dispatcher)
        return AdapterReceipt(
            job_id=locked_job.pk,
            attempt_id=attempt.pk,
            attempt_number=attempt.attempt_number,
            status=locked_job.status,
            supported=True,
            message="retry queued",
            created_attempt=created,
            details={"retry_reason": str(reason or "retry requested")[:4000], "policy": decision.as_dict(), "retry_request_id": retry_request.pk},
        )

    def collect_variant_result(
        self,
        job: ExecutionJob,
        variant,
        payload: Mapping[str, Any],
        *,
        attempt=None,
    ) -> AdapterReceipt:
        """Persist one variant result and aggregate it into the owning job."""
        status_value = _normalise_job_status(payload.get('status', ExecutionStatus.SUCCEEDED))
        if status_value not in set(ExecutionStatus.values):
            raise AdapterError(f'Unknown execution result status: {status_value}')
        with transaction.atomic():
            locked_job = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            locked_variant = locked_job.variants.select_for_update().get(pk=variant.pk)
            locked_attempt = None
            if attempt is not None:
                locked_attempt = ExecutionAttempt.objects.select_for_update().get(pk=attempt.pk)
            else:
                locked_attempt = locked_variant.attempts.order_by('-attempt_number').first()
            if locked_attempt is None:
                raise AdapterError('Variant has no execution attempt')
            if locked_variant.status in _TERMINAL_SUCCESS | _TERMINAL_FAILURE:
                return AdapterReceipt(
                    job_id=locked_job.pk,
                    attempt_id=locked_attempt.pk,
                    attempt_number=locked_attempt.attempt_number,
                    status=locked_job.status,
                    supported=True,
                    message='late variant result retained as history',
                    details={'ignored': True, 'variant_key': locked_variant.variant_key},
                )

            safe_result = redact_result_payload(payload.get('result') or payload.get('summary') or {})
            error_type = _error_type(payload.get('error_type')) if status_value in _TERMINAL_FAILURE else ''
            error_message = str(payload.get('error_message') or '')[:4000]
            locked_variant.result = safe_result
            locked_variant.status = status_value
            locked_variant.error_type = error_type
            locked_variant.error_message = error_message
            locked_variant.finished_at = timezone.now() if status_value in _TERMINAL_SUCCESS | _TERMINAL_FAILURE else None
            locked_variant.save(update_fields=['result', 'status', 'error_type', 'error_message', 'finished_at', 'updated_at'])
            locked_attempt.result = safe_result
            locked_attempt.error_type = error_type
            locked_attempt.error_message = error_message
            locked_attempt.save(update_fields=['result', 'error_type', 'error_message', 'updated_at'])
            if not locked_attempt.is_terminal:
                locked_attempt.transition_to(status_value)

            aggregate_status, aggregate = _variant_aggregate(locked_job)
            locked_job.result = redact_result_payload(aggregate)
            locked_job.save(update_fields=['result', 'updated_at'])
            if aggregate_status == ExecutionStatus.RUNNING:
                if locked_job.status == ExecutionStatus.PENDING:
                    locked_job.transition_to(ExecutionStatus.QUEUED)
                if locked_job.status == ExecutionStatus.QUEUED:
                    locked_job.transition_to(ExecutionStatus.RUNNING)
            elif not locked_job.is_terminal:
                if locked_job.status == ExecutionStatus.PENDING:
                    locked_job.transition_to(ExecutionStatus.QUEUED)
                if locked_job.status == ExecutionStatus.QUEUED:
                    locked_job.transition_to(ExecutionStatus.RUNNING)
                locked_job.transition_to(aggregate_status)
                locked_attempt.is_final = True
                locked_attempt.save(update_fields=['is_final', 'updated_at'])
                ExecutionAttempt.objects.filter(job=locked_job).exclude(pk=locked_attempt.pk).update(is_final=False)
                locked_job.final_result = redact_result_payload(aggregate)
                locked_job.final_attempt = locked_attempt
                locked_job.final_status = aggregate_status
                locked_job.finalized_at = timezone.now()
                locked_job.save(update_fields=['final_result', 'final_attempt', 'final_status', 'finalized_at', 'updated_at'])

        return AdapterReceipt(
            job_id=locked_job.pk,
            attempt_id=locked_attempt.pk,
            attempt_number=locked_attempt.attempt_number,
            status=locked_job.status,
            supported=True,
            message='variant result collected',
            details={'variant_key': locked_variant.variant_key, 'aggregate': aggregate},
        )

    def collect_result(
        self,
        job: ExecutionJob,
        attempt: ExecutionAttempt,
        payload: Mapping[str, Any],
    ) -> AdapterReceipt:
        """Persist a normalized result payload emitted by a legacy executor."""
        variant = getattr(attempt, 'variant', None)
        if variant is not None:
            return self.collect_variant_result(job, variant, payload, attempt=attempt)
        status_value = _normalise_job_status(payload.get("status", ExecutionStatus.SUCCEEDED))
        if status_value not in set(ExecutionStatus.values):
            raise AdapterError(f"Unknown execution result status: {status_value}")
        step_payloads = payload.get("steps") or []
        artifact_payloads = payload.get("artifacts") or []
        retry_request_id = None
        with transaction.atomic():
            locked_job = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            locked_attempt = ExecutionAttempt.objects.select_for_update().get(pk=attempt.pk)
            if locked_job.final_attempt_id:
                return AdapterReceipt(
                    job_id=locked_job.pk,
                    attempt_id=locked_attempt.pk,
                    attempt_number=locked_attempt.attempt_number,
                    status=locked_job.status,
                    supported=True,
                    message="late result retained as history",
                    details={"ignored": True, "final_attempt_id": locked_job.final_attempt_id},
                )
            locked_attempt.result = dict(payload.get("result") or payload.get("summary") or {})
            locked_attempt.error_type = _error_type(payload.get("error_type")) if status_value in _TERMINAL_FAILURE else ""
            locked_attempt.error_message = str(payload.get("error_message") or "")[:4000]
            locked_attempt.save(update_fields=["result", "error_type", "error_message", "updated_at"])
            if locked_attempt.status not in {AttemptStatus.PENDING, AttemptStatus.QUEUED, AttemptStatus.RUNNING}:
                raise AdapterConflict("Attempt is already terminal")

            for item in step_payloads:
                item = dict(item)
                case_key = str(item.get("case_key") or "default")
                step_key = str(item.get("step_key") or item.get("id") or case_key)
                step, _created = ExecutionStepResult.objects.get_or_create(
                    attempt=locked_attempt,
                    case_key=case_key,
                    step_key=step_key,
                    defaults={
                        "idempotency_key": str(item.get("idempotency_key") or "step-" + case_key + "-" + step_key),
                    },
                )
                step.status = _normalise_step_status(item.get("status", status_value))
                step.result = dict(item.get("result") or {})
                step.error_type = _error_type(item.get("error_type"))
                step.error_message = str(item.get("error_message") or "")[:4000]
                step.duration_ms = item.get("duration_ms")
                step.save(update_fields=["status", "result", "error_type", "error_message", "duration_ms", "updated_at"])

            for item in artifact_payloads:
                item = dict(item)
                uri = str(item.get("uri") or "").strip()
                if not uri:
                    continue
                ExecutionArtifact.objects.get_or_create(
                    idempotency_key=str(item.get("idempotency_key") or f"artifact-{locked_attempt.pk}-{uri}"),
                    defaults={
                        "job": locked_job,
                        "attempt": locked_attempt,
                        "uri": uri,
                        "content_hash": str(item.get("content_hash") or ""),
                        "media_type": str(item.get("media_type") or ""),
                        "size_bytes": item.get("size_bytes"),
                        "retention_policy": str(item.get("retention_policy") or "default"),
                        "metadata": dict(item.get("metadata") or {}),
                    },
                )

            result_payload = dict(payload.get("result") or payload.get("summary") or {})
            gate_result = apply_quality_gate(locked_job, result_payload, stage="result", attempt=locked_attempt)
            effective_status = status_value
            if status_value in _TERMINAL_SUCCESS and gate_result.get("configured") and not gate_result.get("passed"):
                effective_status = ExecutionStatus.BLOCKED
                locked_attempt.error_type = ErrorType.CONFIGURATION
                locked_attempt.error_message = "Quality gate failed"
                locked_attempt.save(update_fields=["error_type", "error_message", "updated_at"])

            locked_job.result = result_payload
            locked_job.error_type = locked_attempt.error_type
            locked_job.error_message = locked_attempt.error_message
            locked_job.save(update_fields=["result", "error_type", "error_message", "updated_at"])
            if locked_job.status not in {ExecutionStatus.PENDING, ExecutionStatus.QUEUED, ExecutionStatus.RUNNING}:
                raise AdapterConflict("Job is already terminal")
            locked_attempt.transition_to(effective_status)
            locked_job.transition_to(effective_status)
            sync_quality_result(locked_job, locked_attempt, effective_status, result_payload)

            decision = should_retry(locked_job, locked_attempt, automatic=True)
            retried = False
            if effective_status in _TERMINAL_FAILURE and decision.allowed:
                policy = resolve_retry_policy(locked_job)
                clear_final_for_retry(locked_job)
                mark_retry_requested(
                    locked_attempt,
                    "automatic retry: " + decision.reason,
                    policy=policy,
                    decision=decision.reason,
                    automatic=True,
                )
                locked_job.transition_to(ExecutionStatus.QUEUED, reason="automatic retry")
                next_attempt, _next_created = locked_job.create_attempt()
                next_attempt.transition_to(AttemptStatus.QUEUED)
                retry_request = create_retry_request(
                    locked_job,
                    locked_attempt,
                    next_attempt,
                    policy,
                    "automatic retry: " + decision.reason,
                )
                retry_request_id = retry_request.pk
                retried = True
            else:
                preserve_final_result(locked_job, locked_attempt, effective_status, result_payload, gate_result)

        if retry_request_id and getattr(self, 'dispatcher', None) is not None:
            self._schedule_retry_dispatch(retry_request_id, self.dispatcher)
        return AdapterReceipt(
            job_id=locked_job.pk,
            attempt_id=locked_attempt.pk,
            attempt_number=locked_attempt.attempt_number,
            status=locked_job.status,
            supported=True,
            message="result collected",
            details={"steps": len(step_payloads), "artifacts": len(artifact_payloads), "quality_gate": gate_result, "automatic_retry": retried},
        )


class SourceExecutorAdapter(ExecutorAdapter):
    """Compatibility adapter for one existing module's executor."""

    def __init__(self, source_type: str, dispatcher=None, canceller=None):
        self.source_type = source_type
        self.dispatcher = dispatcher
        self.canceller = canceller

    def submit(self, job, *, dispatcher=None, worker="", quarantine_override=False, override_reason="", requested_by=None):
        return super().submit(
            job,
            dispatcher=dispatcher or self.dispatcher,
            worker=worker,
            quarantine_override=quarantine_override,
            override_reason=override_reason,
            requested_by=requested_by,
        )

    def cancel(self, job, *, reason="", canceller=None):
        return super().cancel(job, reason=reason, canceller=canceller or self.canceller)


class ApiExecutorAdapter(SourceExecutorAdapter):
    def __init__(self, dispatcher=None, canceller=None):
        super().__init__("api", dispatcher=dispatcher, canceller=canceller)


class UiExecutorAdapter(SourceExecutorAdapter):
    def __init__(self, dispatcher=None, canceller=None):
        super().__init__("ui", dispatcher=dispatcher, canceller=canceller)


class AppExecutorAdapter(SourceExecutorAdapter):
    def __init__(self, dispatcher=None, canceller=None):
        super().__init__("app", dispatcher=dispatcher, canceller=canceller)


class PerformanceExecutorAdapter(SourceExecutorAdapter):
    def __init__(self, dispatcher=None, canceller=None):
        super().__init__("performance", dispatcher=dispatcher, canceller=canceller)


class ExecutorAdapterRegistry:
    def __init__(self, adapters=None):
        self._adapters = {}
        for adapter in adapters or ():
            self.register(adapter)

    def register(self, adapter: ExecutorAdapter):
        self._adapters[adapter.source_type] = adapter
        return adapter

    def get(self, source_type: str) -> ExecutorAdapter:
        try:
            return self._adapters[source_type]
        except KeyError as exc:
            raise UnsupportedExecutorError(f"No adapter for source type {source_type!r}") from exc

    def supported_sources(self):
        return tuple(sorted(self._adapters))


DEFAULT_EXECUTOR_REGISTRY = ExecutorAdapterRegistry(
    [
        ApiExecutorAdapter(),
        UiExecutorAdapter(),
        AppExecutorAdapter(),
        PerformanceExecutorAdapter(),
    ]
)


def get_executor_adapter(source_type: str, registry=None) -> ExecutorAdapter:
    return (registry or DEFAULT_EXECUTOR_REGISTRY).get(source_type)


def register_executor_dispatcher(source_type: str, dispatcher):
    """Register a process-local source dispatcher used by API dispatch."""
    adapter = DEFAULT_EXECUTOR_REGISTRY.get(source_type)
    adapter.dispatcher = dispatcher
    return adapter


def unregister_executor_dispatcher(source_type: str):
    adapter = DEFAULT_EXECUTOR_REGISTRY.get(source_type)
    adapter.dispatcher = None
    return adapter


__all__ = [
    "AdapterConflict",
    "AdapterError",
    "AdapterReceipt",
    "ApiExecutorAdapter",
    "AppExecutorAdapter",
    "DEFAULT_EXECUTOR_REGISTRY",
    "ExecutorAdapter",
    "ExecutorAdapterRegistry",
    "PerformanceExecutorAdapter",
    "SourceExecutorAdapter",
    "UiExecutorAdapter",
    "UnsupportedExecutorError",
    "register_executor_dispatcher",
    "unregister_executor_dispatcher",
    "_call_dispatcher",
    "_variant_aggregate",
]
