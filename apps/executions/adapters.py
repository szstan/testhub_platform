"""Durable, executor-neutral dispatch, retry and result aggregation."""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone

from .dispatch import CeleryExecutionDispatcher
from .models import ExecutionAttempt, ExecutionJob, ExecutionRetryRequest, ExecutionStepResult
from .result_events import ResultEventIngestor, redact_result_payload
from .state_machine import AttemptStatus, ErrorType, ExecutionStatus, StepStatus
from .policies import (
    apply_quality_gate, check_dispatch_governance, clear_final_for_retry,
    create_retry_request, mark_retry_requested, preserve_final_result,
    resolve_retry_policy, should_retry, sync_quality_result,
)


class AdapterError(RuntimeError):
    pass


class UnsupportedExecutorError(AdapterError):
    pass


class AdapterConflict(AdapterError):
    pass


@dataclass(frozen=True)
class AdapterReceipt:
    job_id: int
    attempt_id: int
    attempt_number: int
    status: str
    supported: bool
    dispatch_id: str = ''
    message: str = ''
    created_attempt: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)


_TERMINAL_SUCCESS = {ExecutionStatus.PASSED, ExecutionStatus.SUCCEEDED, ExecutionStatus.COMPLETED}
_TERMINAL_FAILURE = {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED, ExecutionStatus.UNSUPPORTED, ExecutionStatus.CANCELLED}
_TERMINAL = _TERMINAL_SUCCESS | _TERMINAL_FAILURE


def _normalise_job_status(value):
    raw = str(getattr(value, 'value', value) or '').strip().lower()
    return {'success': 'succeeded', 'complete': 'completed', 'error': 'failed',
            'failure': 'failed', 'stopped': 'cancelled', 'skip': 'unsupported',
            'skipped': 'unsupported'}.get(raw, raw)


def _normalise_step_status(value):
    raw = str(getattr(value, 'value', value) or '').strip().lower()
    return {'success': 'succeeded', 'complete': 'completed', 'error': 'failed', 'failure': 'failed'}.get(raw, raw)


def _error_type(value):
    raw = str(getattr(value, 'value', value) or '').strip().lower()
    return raw if raw in ErrorType.values else ErrorType.UNKNOWN


def _safe_text(value):
    return redact_result_payload(str(value or ''))[:4000]


def _call_dispatcher(dispatcher, job, attempt, variant):
    try:
        parameters = inspect.signature(dispatcher).parameters.values()
        positional = [p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        variadic = any(p.kind == p.VAR_POSITIONAL for p in parameters)
    except (TypeError, ValueError):
        variadic, positional = True, []
    if variadic or len(positional) >= 3:
        return dispatcher(job, attempt, variant)
    return dispatcher(job, attempt)


def _variant_aggregate(job):
    from .gate_policy import extract_metrics, normalize_metric_rules, evaluate_metric_rules

    variants = list(job.variants.order_by('ordinal'))
    statuses = [v.status for v in variants]
    if not variants or any(s not in _TERMINAL for s in statuses):
        status = ExecutionStatus.RUNNING if variants else ExecutionStatus.PENDING
    elif ExecutionStatus.BLOCKED in statuses:
        status = ExecutionStatus.BLOCKED
    elif ExecutionStatus.FAILED in statuses:
        status = ExecutionStatus.FAILED
    elif ExecutionStatus.CANCELLED in statuses:
        status = ExecutionStatus.CANCELLED
    elif ExecutionStatus.UNSUPPORTED in statuses:
        status = ExecutionStatus.UNSUPPORTED
    else:
        status = ExecutionStatus.PASSED
    aggregate = {
        'status': status, 'count': len(variants),
        'total': len(variants),
        'passed': sum(v.status in _TERMINAL_SUCCESS for v in variants),
        'failed': sum(v.status in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED} for v in variants),
        'skipped': sum(v.status in {ExecutionStatus.CANCELLED, ExecutionStatus.UNSUPPORTED} for v in variants),
        'variants': [dict(variant_key=v.variant_key, ordinal=v.ordinal, status=v.status,
                          result=v.result, error_type=v.error_type, error_message=v.error_message) for v in variants],
    }
    # Count metrics are additive; other configured metrics must hold for EVERY
    # variant. Select a failing value if present, otherwise the strictest bound.
    gate = getattr(getattr(job, 'profile', None), 'quality_gate', {})
    try:
        rules = normalize_metric_rules(gate)
    except ValueError:
        rules = {}
    count_metrics = {'total', 'passed', 'failed', 'skipped', 'matched', 'unmatched'}
    count_aggregation = {}
    rows = [extract_metrics(v.result) for v in variants]
    for metric, rule in rules.items():
        if metric in count_metrics:
            reported = [row[metric] for row in rows if metric in row]
            if not reported:
                count_aggregation[metric] = {
                    'state': 'derived', 'reported_variants': 0,
                    'expected_variants': len(rows), 'reported_total': None,
                }
                continue

            def is_valid_count(value):
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return False
                try:
                    number = float(value)
                except (OverflowError, TypeError, ValueError):
                    return False
                return math.isfinite(number) and number >= 0 and number.is_integer()

            valid = [value for value in reported if is_valid_count(value)]
            if len(reported) != len(rows):
                aggregate[metric] = None
                count_aggregation[metric] = {
                    'state': 'partial', 'reported_variants': len(reported),
                    'expected_variants': len(rows), 'reported_total': sum(valid),
                    'invalid_values': len(reported) - len(valid),
                }
            elif len(valid) != len(reported):
                aggregate[metric] = None
                count_aggregation[metric] = {
                    'state': 'invalid', 'reported_variants': len(reported),
                    'expected_variants': len(rows), 'reported_total': sum(valid),
                    'invalid_values': len(reported) - len(valid),
                }
            else:
                aggregate[metric] = sum(reported)
                count_aggregation[metric] = {
                    'state': 'complete', 'reported_variants': len(reported),
                    'expected_variants': len(rows), 'reported_total': aggregate[metric],
                }
            continue
        if not rows or any(metric not in row for row in rows):
            continue
        values = [row[metric] for row in rows]
        invalid = [value for value in values if evaluate_metric_rules({metric: rule}, {metric: value})]
        aggregate[metric] = invalid[0] if invalid else values[0]
    if count_aggregation:
        aggregate['metric_aggregation'] = count_aggregation
    return status, aggregate


def _retry_decision(job, attempt, *, automatic):
    if attempt is not None and attempt.variant_id:
        scoped = SimpleNamespace(
            attempt_number=attempt.variant.attempts.count(),
            status=attempt.status,
            error_type=attempt.error_type,
        )
        return should_retry(job, scoped, automatic=automatic)
    return should_retry(job, attempt, automatic=automatic)


def _receipt(job, attempt, *, created=False, supported=True, message='', details=None):
    return AdapterReceipt(job.pk, attempt.pk if attempt else 0,
                          attempt.attempt_number if attempt else 0, job.status, supported,
                          dispatch_id=attempt.dispatch_id if attempt else '', message=message,
                          created_attempt=created, details=details or {})


class ExecutorAdapter:
    source_type = 'other'

    def validate(self, job):
        if job.source_type != self.source_type:
            raise AdapterError(f'Adapter {self.source_type!r} cannot handle source {job.source_type!r}')
        if not job.project_id:
            raise AdapterError('Execution job must belong to a project')

    def _resolve_dispatcher(self, dispatcher=None):
        candidate = dispatcher if dispatcher is not None else getattr(self, 'dispatcher', None)
        supports = getattr(candidate, 'supports', None)
        if callable(supports):
            try:
                if not supports(self.source_type):
                    return None
            except ValueError as exc:
                raise AdapterConflict(f'Invalid dispatcher configuration: {exc}') from exc
        return candidate

    def _has_dispatcher(self):
        return self._resolve_dispatcher() is not None

    def _schedule_retry_dispatch(self, request_id, dispatcher, worker=''):
        if dispatcher is None:
            return

        def dispatch_after_commit():
            request = ExecutionRetryRequest.objects.select_related('job').filter(pk=request_id).first()
            if request is None or request.status != 'queued' or request.scheduled_at > timezone.now():
                return
            try:
                self.submit(request.job, dispatcher=dispatcher, worker=worker, requested_by=request.job.requested_by)
                request.status, request.dispatched_at = 'dispatched', timezone.now()
                request.save(update_fields=['status', 'dispatched_at', 'updated_at'])
            except Exception as exc:
                request.status, request.error_message = 'failed', _safe_text(exc)
                request.save(update_fields=['status', 'error_message', 'updated_at'])
        transaction.on_commit(dispatch_after_commit)

    def _reserve(self, job, attempt, dispatcher):
        if attempt.dispatch_reserved_at or attempt.dispatch_id or attempt.worker_claim_id or attempt.is_terminal:
            return False
        attempt.dispatch_reserved_at = timezone.now()
        if isinstance(dispatcher, CeleryExecutionDispatcher):
            attempt.dispatch_id = f'execution-{job.pk}-{attempt.pk}'
            if attempt.variant_id:
                attempt.dispatch_id += f'-variant-{attempt.variant_id}'
        attempt.save(update_fields=['dispatch_reserved_at', 'dispatch_id', 'updated_at'])
        if attempt.status == AttemptStatus.PENDING:
            attempt.transition_to(AttemptStatus.QUEUED)
        if attempt.status == AttemptStatus.QUEUED:
            attempt.transition_to(AttemptStatus.RUNNING)
        return True

    def _dispatch_reserved(self, job, attempt, dispatcher, variant=None):
        if dispatcher is None:
            self.collect_result(job, attempt, {'status': 'unsupported', 'error_type': 'unsupported',
                                             'error_message': f'No dispatcher is registered for source type {self.source_type}'})
            return ''
        try:
            result = _call_dispatcher(dispatcher, job, attempt, variant)
            if isinstance(result, Mapping):
                if 'status' in result or 'result' in result or 'summary' in result:
                    self.collect_result(job, attempt, result)
                dispatch_id = str(result.get('dispatch_id') or '')
            else:
                dispatch_id = str(result or '')
            # Update only the dispatch field; eager workers may already have
            # finished this attempt while the broker call was returning.
            if dispatch_id:
                ExecutionAttempt.objects.filter(pk=attempt.pk).update(dispatch_id=dispatch_id)
                if variant is not None:
                    type(variant).objects.filter(pk=variant.pk).update(dispatch_id=dispatch_id)
                attempt.dispatch_id = dispatch_id
            return dispatch_id
        except Exception as exc:
            self.collect_result(job, attempt, {'status': 'failed', 'error_type': 'infrastructure', 'error_message': _safe_text(exc)})
            return ''

    def _submit_dataset(self, job, *, dispatcher=None, worker=''):
        with transaction.atomic():
            locked = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            if locked.is_terminal:
                raise AdapterConflict('A terminal execution requires an explicit retry')
            if locked.status == ExecutionStatus.CANCEL_REQUESTED:
                return _receipt(locked, locked.attempts.order_by('-attempt_number').first(), message='execution cancellation requested')
            if not locked.dataset_snapshot or not locked.variants.exists():
                locked.initialize_dataset_variants()
            variants = list(locked.variants.select_for_update().order_by('ordinal'))
            if not variants:
                raise AdapterConflict('Dataset must produce at least one variant')
            reserved, attempts = [], []
            for variant in variants:
                attempt = variant.attempts.order_by('-attempt_number').first()
                if attempt is None:
                    attempt, _ = locked.create_attempt(variant=variant, worker=worker)
                attempts.append(attempt)
                if variant.status in _TERMINAL:
                    continue
                if self._reserve(locked, attempt, dispatcher):
                    variant.status, variant.started_at = ExecutionStatus.RUNNING, timezone.now()
                    variant.save(update_fields=['status', 'started_at', 'updated_at'])
                    reserved.append((variant, attempt))
            if locked.status == ExecutionStatus.PENDING:
                locked.transition_to(ExecutionStatus.QUEUED)
            if locked.status == ExecutionStatus.QUEUED:
                locked.transition_to(ExecutionStatus.RUNNING)
        ids = [self._dispatch_reserved(locked, a, dispatcher, v) for v, a in reserved]
        locked.refresh_from_db()
        attempts[0].refresh_from_db()
        return _receipt(locked, attempts[0], created=bool(reserved), supported=dispatcher is not None,
                        details={'variant_count': len(variants), 'variants': [v.variant_key for v in variants], 'dispatch_ids': ids})

    def submit(self, job, *, dispatcher: Optional[Callable] = None, worker='',
               quarantine_override=False, override_reason='', requested_by=None):
        self.validate(job)
        governance = check_dispatch_governance(job, override=quarantine_override,
                                              override_reason=override_reason, actor=requested_by or job.requested_by)
        if not governance.get('allowed'):
            raise AdapterConflict(f"Dispatch denied: {governance.get('reason')}")
        if job.dataset_id:
            return self._submit_dataset(job, dispatcher=dispatcher, worker=worker)
        with transaction.atomic():
            locked = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            if locked.is_terminal:
                raise AdapterConflict('A terminal execution requires an explicit retry')
            attempt = locked.attempts.order_by('-attempt_number').first()
            created = attempt is None
            if attempt is None:
                attempt, _ = locked.create_attempt(worker=worker)
            if locked.status == ExecutionStatus.CANCEL_REQUESTED or not self._reserve(locked, attempt, dispatcher):
                return _receipt(locked, attempt, message='dispatch already reserved')
            if locked.status == ExecutionStatus.PENDING:
                locked.transition_to(ExecutionStatus.QUEUED)
            if locked.status == ExecutionStatus.QUEUED:
                locked.worker = worker or locked.worker
                locked.save(update_fields=['worker', 'updated_at'])
                locked.transition_to(ExecutionStatus.RUNNING)
        self._dispatch_reserved(locked, attempt, dispatcher)
        locked.refresh_from_db()
        attempt.refresh_from_db()
        return _receipt(locked, attempt, created=created, supported=dispatcher is not None,
                        message=attempt.error_message,
                        details={'automatic_retry': locked.retry_requests.filter(from_attempt=attempt).exists()})

    def cancel(self, job, *, reason='', canceller=None):
        self.validate(job)
        with transaction.atomic():
            locked = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            if locked.status in _TERMINAL_SUCCESS:
                raise AdapterConflict('A successful execution cannot be cancelled')
            locked.request_cancel(_safe_text(reason))
            attempts = list(locked.attempts.filter(status__in=[AttemptStatus.PENDING, AttemptStatus.QUEUED, AttemptStatus.RUNNING]))
            for attempt in attempts:
                attempt.transition_to(AttemptStatus.CANCEL_REQUESTED)
        if canceller:
            for attempt in attempts:
                canceller(locked, attempt)
        return _receipt(locked, attempts[-1] if attempts else None, message=_safe_text(reason))

    def retry(self, job, *, reason='retry requested', requested_by=None, automatic=False):
        self.validate(job)
        if self._resolve_dispatcher() is None:
            raise AdapterConflict(f'No dispatcher is registered for source type {self.source_type}; retry cannot be queued')
        with transaction.atomic():
            locked = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            if locked.status not in _TERMINAL_FAILURE:
                raise AdapterConflict('Only failed, blocked, cancelled or unsupported jobs can retry')
            previous = locked.attempts.order_by('-attempt_number').first()
            is_matrix = bool(locked.dataset_id or locked.dataset_snapshot or locked.variants.exists())
            retry_entries = []
            if is_matrix:
                failed_variants = list(
                    locked.variants.select_for_update()
                    .filter(status__in=_TERMINAL_FAILURE)
                    .order_by('ordinal')
                )
                if not failed_variants:
                    raise AdapterConflict('Dataset has no failed variants to retry')
                decisions = []
                for variant in failed_variants:
                    old_attempt = variant.latest_attempt
                    variant_decision = _retry_decision(locked, old_attempt, automatic=automatic)
                    decisions.append((variant, old_attempt, variant_decision))
                retry_entries = [entry for entry in decisions if entry[1] is not None and entry[2].allowed]
                if not retry_entries:
                    reason_code = decisions[0][2].reason if decisions else 'NO_RETRYABLE_VARIANTS'
                    raise AdapterConflict(f'Retry denied: {reason_code}')
                decision = retry_entries[0][2]
                previous = retry_entries[0][1]
            else:
                decision = _retry_decision(locked, previous, automatic=automatic)
                if not decision.allowed:
                    raise AdapterConflict(f'Retry denied: {decision.reason}')
            policy = resolve_retry_policy(locked)
            clear_final_for_retry(locked)
            locked.transition_to(ExecutionStatus.QUEUED, reason=_safe_text(reason))
            retry_requests = []
            attempts = []
            if is_matrix:
                for variant, old_attempt, variant_decision in retry_entries:
                    mark_retry_requested(old_attempt, _safe_text(reason), requested_by, policy=policy,
                                         decision=variant_decision.reason, automatic=automatic)
                    attempt, _ = locked.create_attempt(variant=variant)
                    attempt.transition_to(AttemptStatus.QUEUED)
                    variant.status, variant.finished_at = ExecutionStatus.QUEUED, None
                    variant.save(update_fields=['status', 'finished_at', 'updated_at'])
                    attempts.append(attempt)
                    retry_requests.append(create_retry_request(
                        locked, old_attempt, attempt, policy, _safe_text(reason),
                        automatic=automatic, requested_by=requested_by,
                    ))
                attempt = attempts[0]
            else:
                if previous:
                    mark_retry_requested(previous, _safe_text(reason), requested_by, policy=policy,
                                         decision=decision.reason, automatic=automatic)
                attempt, _ = locked.create_attempt()
                attempt.transition_to(AttemptStatus.QUEUED)
                attempts.append(attempt)
                retry_requests.append(create_retry_request(
                    locked, previous or attempt, attempt, policy, _safe_text(reason),
                    automatic=automatic, requested_by=requested_by,
                ))
        dispatcher = self._resolve_dispatcher()
        for request in retry_requests:
            self._schedule_retry_dispatch(request.pk, dispatcher)
        request_ids = [request.pk for request in retry_requests]
        return _receipt(locked, attempt, created=True, message='retry queued',
                        details={'retry_reason': _safe_text(reason), 'policy': decision.as_dict(),
                                 'retry_request_id': request_ids[0], 'retry_request_ids': request_ids})

    def collect_variant_result(self, job, variant, payload, *, attempt=None):
        if not isinstance(payload, Mapping):
            raise AdapterError('Execution result must be an object')
        target = _normalise_job_status(payload.get('status', ExecutionStatus.SUCCEEDED))
        if target not in _TERMINAL:
            raise AdapterError(f'Unknown terminal execution result status: {target}')
        with transaction.atomic():
            locked = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            variant = locked.variants.select_for_update().get(pk=variant.pk)
            current = variant.attempts.order_by('-attempt_number').first()
            if current is None:
                raise AdapterError('Variant has no execution attempt')
            if attempt is not None and (attempt.job_id != locked.pk or attempt.variant_id != variant.pk):
                raise AdapterError('Attempt does not belong to the variant')
            attempt = ExecutionAttempt.objects.select_for_update().get(pk=(attempt or current).pk)
            if locked.final_attempt_id or locked.is_terminal or attempt.pk != current.pk or attempt.is_terminal or variant.status in _TERMINAL:
                return _receipt(locked, attempt, message='late variant result retained as history', details={'ignored': True})
            if locked.status == ExecutionStatus.CANCEL_REQUESTED or attempt.status == AttemptStatus.CANCEL_REQUESTED:
                target = ExecutionStatus.CANCELLED
            result = payload.get('result') or payload.get('summary') or {}
            if not isinstance(result, Mapping):
                raise AdapterError('Execution result must be an object')
            result = redact_result_payload(dict(result))
            error_type = _error_type(payload.get('error_type')) if target in _TERMINAL_FAILURE else ''
            error = _safe_text(payload.get('error_message'))
            variant.status, variant.result = target, result
            variant.error_type, variant.error_message, variant.finished_at = error_type, error, timezone.now()
            variant.save(update_fields=['status', 'result', 'error_type', 'error_message', 'finished_at', 'updated_at'])
            attempt.result, attempt.error_type, attempt.error_message = result, error_type, error
            attempt.save(update_fields=['result', 'error_type', 'error_message', 'updated_at'])
            attempt.transition_to(target)
            aggregate_status, aggregate = _variant_aggregate(locked)
            locked.result = redact_result_payload(aggregate)
            locked.save(update_fields=['result', 'updated_at'])
            if aggregate_status in _TERMINAL:
                gate = apply_quality_gate(locked, aggregate, stage='result', attempt=attempt)
                if aggregate_status in _TERMINAL_SUCCESS and gate.get('configured') and not gate.get('passed'):
                    aggregate_status = ExecutionStatus.BLOCKED
                    locked.error_type, locked.error_message = ErrorType.CONFIGURATION, 'Quality gate failed'
                    locked.save(update_fields=['error_type', 'error_message', 'updated_at'])
                if locked.status == ExecutionStatus.PENDING:
                    locked.transition_to(ExecutionStatus.QUEUED)
                if locked.status == ExecutionStatus.QUEUED:
                    locked.transition_to(ExecutionStatus.RUNNING)
                retry_requests = []
                retry_dispatcher = self._resolve_dispatcher()
                if retry_dispatcher is not None and locked.status != ExecutionStatus.CANCEL_REQUESTED:
                    for failed_variant in locked.variants.filter(status__in=_TERMINAL_FAILURE).order_by('ordinal'):
                        previous = failed_variant.latest_attempt
                        decision = _retry_decision(locked, previous, automatic=True)
                        if not decision.allowed:
                            continue
                        policy = resolve_retry_policy(locked)
                        reason = 'automatic variant retry: ' + decision.reason
                        mark_retry_requested(previous, reason, policy=policy, decision=decision.reason)
                        next_attempt, _ = locked.create_attempt(variant=failed_variant)
                        next_attempt.transition_to(AttemptStatus.QUEUED)
                        failed_variant.status, failed_variant.finished_at = ExecutionStatus.QUEUED, None
                        failed_variant.save(update_fields=['status', 'finished_at', 'updated_at'])
                        retry_requests.append(create_retry_request(locked, previous, next_attempt, policy, reason).pk)
                if retry_requests:
                    locked.result = redact_result_payload(_variant_aggregate(locked)[1])
                    locked.save(update_fields=['result', 'updated_at'])
                    for request_id in retry_requests:
                        self._schedule_retry_dispatch(request_id, retry_dispatcher)
                else:
                    locked.transition_to(aggregate_status)
                    sync_quality_result(locked, attempt, aggregate_status, aggregate)
                    preserve_final_result(locked, attempt, aggregate_status, aggregate, gate)
        return _receipt(locked, attempt, message='variant result collected', details={'variant_key': variant.variant_key, 'aggregate': aggregate})

    def collect_result(self, job, attempt, payload):
        if not isinstance(payload, Mapping):
            raise AdapterError('Execution result must be an object')
        if attempt.variant_id:
            return self.collect_variant_result(job, attempt.variant, payload, attempt=attempt)
        target = _normalise_job_status(payload.get('status', ExecutionStatus.SUCCEEDED))
        if target not in _TERMINAL:
            raise AdapterError(f'Unknown terminal execution result status: {target}')
        result = payload.get('result') or payload.get('summary') or {}
        if not isinstance(result, Mapping):
            raise AdapterError('Execution result must be an object')
        steps, artifacts = payload.get('steps') or [], payload.get('artifacts') or []
        if not isinstance(steps, list) or not isinstance(artifacts, list):
            raise AdapterError('Steps and artifacts must be lists')
        request_id, retried, retry_blocked = None, False, False
        with transaction.atomic():
            locked = ExecutionJob.objects.select_for_update().get(pk=job.pk)
            attempt = ExecutionAttempt.objects.select_for_update().get(pk=attempt.pk, job=locked)
            current = locked.attempts.filter(variant_id=None).order_by('-attempt_number').first()
            if locked.final_attempt_id or locked.is_terminal or attempt.is_terminal or current.pk != attempt.pk:
                return _receipt(locked, attempt, message='late result retained as history', details={'ignored': True, 'final_attempt_id': locked.final_attempt_id})
            if locked.status == ExecutionStatus.CANCEL_REQUESTED or attempt.status == AttemptStatus.CANCEL_REQUESTED:
                target = ExecutionStatus.CANCELLED
            attempt.result = redact_result_payload(dict(result))
            attempt.error_type = _error_type(payload.get('error_type')) if target in _TERMINAL_FAILURE else ''
            attempt.error_message = _safe_text(payload.get('error_message'))
            attempt.save(update_fields=['result', 'error_type', 'error_message', 'updated_at'])
            for item in steps:
                if not isinstance(item, Mapping):
                    raise AdapterError('Step must be an object')
                ResultEventIngestor._record_step(attempt, redact_result_payload(dict(item)))
            for item in artifacts:
                if not isinstance(item, Mapping):
                    raise AdapterError('Artifact must be an object')
                normalized = dict(item)
                normalized['artifact_idempotency_key'] = normalized.get('idempotency_key', '')
                ResultEventIngestor._record_artifact(locked, attempt, redact_result_payload(normalized))
            gate = apply_quality_gate(locked, attempt.result, stage='result', attempt=attempt)
            if target in _TERMINAL_SUCCESS and gate.get('configured') and not gate.get('passed'):
                target = ExecutionStatus.BLOCKED
                attempt.error_type, attempt.error_message = ErrorType.CONFIGURATION, 'Quality gate failed'
                attempt.save(update_fields=['error_type', 'error_message', 'updated_at'])
            locked.result, locked.error_type, locked.error_message = attempt.result, attempt.error_type, attempt.error_message
            locked.save(update_fields=['result', 'error_type', 'error_message', 'updated_at'])
            if locked.status == ExecutionStatus.PENDING:
                locked.transition_to(ExecutionStatus.QUEUED)
            if locked.status == ExecutionStatus.QUEUED:
                locked.transition_to(ExecutionStatus.RUNNING)
            attempt.transition_to(target)
            locked.transition_to(target)
            sync_quality_result(locked, attempt, target, attempt.result)
            decision = should_retry(locked, attempt, automatic=True)
            dispatcher = self._resolve_dispatcher()
            if target in _TERMINAL_FAILURE and decision.allowed and dispatcher is not None:
                policy = resolve_retry_policy(locked)
                clear_final_for_retry(locked)
                mark_retry_requested(attempt, 'automatic retry: ' + decision.reason, policy=policy, decision=decision.reason)
                locked.transition_to(ExecutionStatus.QUEUED, reason='automatic retry')
                next_attempt, _ = locked.create_attempt()
                next_attempt.transition_to(AttemptStatus.QUEUED)
                request = create_retry_request(locked, attempt, next_attempt, policy, 'automatic retry: ' + decision.reason)
                request_id, retried = request.pk, True
            else:
                if target in _TERMINAL_FAILURE and decision.allowed:
                    retry_blocked = True
                    locked.error_type = ErrorType.CONFIGURATION
                    locked.error_message = f'Automatic retry unavailable: no dispatcher is registered for source type {self.source_type}'
                    locked.save(update_fields=['error_type', 'error_message', 'updated_at'])
                preserve_final_result(locked, attempt, target, attempt.result, gate)
        if request_id:
            self._schedule_retry_dispatch(request_id, dispatcher)
        return _receipt(locked, attempt, message='result collected', details={'steps': len(steps), 'artifacts': len(artifacts), 'quality_gate': gate, 'automatic_retry': retried, 'automatic_retry_blocked': retry_blocked})


class SourceExecutorAdapter(ExecutorAdapter):
    def __init__(self, source_type, dispatcher=None, canceller=None):
        self.source_type, self.dispatcher, self.canceller = source_type, dispatcher, canceller

    def submit(self, job, *, dispatcher=None, worker='', quarantine_override=False, override_reason='', requested_by=None):
        resolved = self._resolve_dispatcher(dispatcher)
        if resolved is not None and not job.source_id:
            raise AdapterConflict(f'Unified execution source_id is required for source type {self.source_type}')
        return super().submit(job, dispatcher=resolved, worker=worker, quarantine_override=quarantine_override, override_reason=override_reason, requested_by=requested_by)

    def cancel(self, job, *, reason='', canceller=None):
        return super().cancel(job, reason=reason, canceller=canceller or self.canceller)


class ApiExecutorAdapter(SourceExecutorAdapter):
    def __init__(self, dispatcher=None, canceller=None):
        super().__init__('api', dispatcher, canceller)


class UiExecutorAdapter(SourceExecutorAdapter):
    def __init__(self, dispatcher=None, canceller=None):
        super().__init__('ui', dispatcher, canceller)


class AppExecutorAdapter(SourceExecutorAdapter):
    def __init__(self, dispatcher=None, canceller=None):
        super().__init__('app', dispatcher, canceller)


class PerformanceExecutorAdapter(SourceExecutorAdapter):
    def __init__(self, dispatcher=None, canceller=None):
        super().__init__('performance', dispatcher, canceller)


class ExecutorAdapterRegistry:
    def __init__(self, adapters=None):
        self._adapters = {}
        for adapter in adapters or ():
            self.register(adapter)

    def register(self, adapter):
        self._adapters[adapter.source_type] = adapter
        return adapter

    def get(self, source_type):
        try:
            return self._adapters[source_type]
        except KeyError as exc:
            raise UnsupportedExecutorError(f'No adapter for source type {source_type!r}') from exc

    def supported_sources(self):
        return tuple(sorted(self._adapters))


DEFAULT_EXECUTOR_REGISTRY = ExecutorAdapterRegistry([
    ApiExecutorAdapter(CeleryExecutionDispatcher()), UiExecutorAdapter(CeleryExecutionDispatcher()),
    AppExecutorAdapter(CeleryExecutionDispatcher()), PerformanceExecutorAdapter(CeleryExecutionDispatcher()),
])


def get_executor_adapter(source_type, registry=None):
    return (registry or DEFAULT_EXECUTOR_REGISTRY).get(source_type)


def register_executor_dispatcher(source_type, dispatcher):
    adapter = DEFAULT_EXECUTOR_REGISTRY.get(source_type)
    adapter.dispatcher = dispatcher
    return adapter


def unregister_executor_dispatcher(source_type):
    return register_executor_dispatcher(source_type, None)


__all__ = ['AdapterConflict', 'AdapterError', 'AdapterReceipt', 'ApiExecutorAdapter',
           'AppExecutorAdapter', 'CeleryExecutionDispatcher', 'DEFAULT_EXECUTOR_REGISTRY',
           'ExecutorAdapter', 'ExecutorAdapterRegistry', 'PerformanceExecutorAdapter',
           'SourceExecutorAdapter', 'UiExecutorAdapter', 'UnsupportedExecutorError',
           'register_executor_dispatcher', 'unregister_executor_dispatcher', '_call_dispatcher', '_variant_aggregate']
