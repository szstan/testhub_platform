"""Source-specific workers using per-delivery database leases and result events."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from itertools import islice
from typing import Mapping

from celery import shared_task

from .models import ExecutionAttempt, ExecutionJob
from .result_events import ResultEventIngestor, redact_result_payload
from .source_contract import (
    SourceBindingError,
    SourceReference,
    resolve_native_source,
    resolve_source_binding,
)
from .worker_leases import ExecutionClaimLost, ExecutionLeaseHeartbeat, claim_attempt

logger = logging.getLogger(__name__)


@contextmanager
def _job_attempt(job_id, attempt_id, variant_id=None):
    job = ExecutionJob.objects.select_related('requested_by').filter(pk=job_id).first()
    attempt = ExecutionAttempt.objects.select_related('variant').filter(pk=attempt_id, job_id=job_id).first()
    if job is None or attempt is None:
        raise SourceBindingError('unified job/attempt was not found')
    if attempt.variant_id != variant_id:
        raise SourceBindingError('worker variant does not match its attempt')
    yield job, attempt


def _event(job, event_type, payload, key, claim_id):
    return ResultEventIngestor.ingest(job, event_type, payload, idempotency_key=key, worker_claim_id=claim_id)


def _claim_attempt(job_id, attempt_id, task_id):
    return claim_attempt(job_id, attempt_id, task_id)


def _start(job, attempt, task_id=''):
    claim_id = _claim_attempt(job.pk, attempt.pk, task_id)
    if not claim_id:
        return ''
    _event(job, 'run_started', {'attempt': attempt.attempt_number}, f'worker:{attempt.pk}:{claim_id}:started', claim_id)
    return claim_id


def _finish(job, attempt, *, claim_id, status, result=None, error_type='', error_message=''):
    return _event(job, 'run_finished', {
        'attempt': attempt.attempt_number, 'status': status, 'result': result or {},
        'error_type': error_type, 'error_message': redact_result_payload(str(error_message or ''))[:4000],
    }, f'worker:{attempt.pk}:{claim_id}:finished', claim_id)


def _variant_payload(attempt, source_type):
    payload = dict(attempt.variant.payload or {}) if attempt.variant_id else {}
    def has_redacted(value):
        if isinstance(value, str):
            return '[REDACTED]' in value or '[TRUNCATED]' in value
        if isinstance(value, Mapping):
            return any(has_redacted(v) for v in value.values())
        if isinstance(value, list):
            return any(has_redacted(v) for v in value)
        return False
    if has_redacted(payload):
        raise SourceBindingError('Redacted dataset values require an explicit runtime secret resolver')
    if source_type == 'api':
        if any(not isinstance(v, (str, int, float, bool, type(None))) for v in payload.values()):
            raise SourceBindingError('API dataset variables must be scalar values')
    elif source_type == 'ui':
        if set(payload) - {'engine', 'browser', 'headless'}:
            raise SourceBindingError('UI dataset supports only engine, browser and headless parameters')
        if 'engine' in payload and payload['engine'] not in {'playwright', 'selenium'}:
            raise SourceBindingError('Unsupported UI dataset engine')
        if 'browser' in payload and payload['browser'] not in {'chrome', 'firefox', 'safari', 'edge'}:
            raise SourceBindingError('Unsupported UI dataset browser')
        if 'headless' in payload and not isinstance(payload['headless'], bool):
            raise SourceBindingError('UI dataset headless must be a boolean')
    elif payload:
        raise SourceBindingError(f'{source_type} runner does not support dataset parameters')
    return payload


def _api_source(reference: SourceReference, binding, actor):
    return resolve_native_source(reference, binding.source_project_id, actor)


def _api_execute(source, reference, actor, payload=None, cancel_check=None):
    from apps.api_testing.utils import execute_api_request, execute_test_suite
    if cancel_check:
        cancel_check()
    kwargs = {'cancel_check': cancel_check, 'variable_overrides': payload or {}}
    if reference.kind == 'suite':
        result = execute_test_suite(source, source.environment, actor, **kwargs)
        success = bool(result and result.get('success')) and not int(result.get('failed_count') or 0)
    else:
        result = execute_api_request(source, None, actor, **kwargs)
        success = bool(result and result.get('success'))
    return success, result or {}


_UI_EVIDENCE_MAX_ITEMS = 500
_UI_EVIDENCE_MAX_DEPTH = 8
_UI_EVIDENCE_TRUNCATED = '[TRUNCATED]'
_UI_EVIDENCE_REFERENCE_FIELDS = (
    'sha256', 'size_bytes', 'mime_type', 'availability', 'source_uri', 'uri',
    'provenance', 'metadata',
)


def _ui_evidence_items(value, *, evidence_reference=False):
    truncated = len(value) > _UI_EVIDENCE_MAX_ITEMS
    if evidence_reference:
        default_count = int('uri' not in value and 'source_uri' not in value)
        default_count += int('metadata' not in value and 'provenance' not in value)
        truncated = len(value) + default_count > _UI_EVIDENCE_MAX_ITEMS
        priority = [key for key in _UI_EVIDENCE_REFERENCE_FIELDS if key in value]
        budget = _UI_EVIDENCE_MAX_ITEMS - len(priority) - int(truncated) - default_count
        items = [(key, value[key]) for key in priority]
        if budget > 0:
            items.extend(islice(
                ((key, child) for key, child in value.items() if key not in priority),
                budget,
            ))
        return items, truncated

    limit = _UI_EVIDENCE_MAX_ITEMS - int(truncated)
    return list(islice(value.items(), limit)), truncated


def _ui_evidence_truncation_key(value):
    key = '_truncated'
    while key in value:
        key = '_' + key
    return key


def _normalize_ui_evidence(value, depth=0):
    if depth > _UI_EVIDENCE_MAX_DEPTH:
        return _UI_EVIDENCE_TRUNCATED
    if isinstance(value, (list, tuple)):
        truncated = len(value) > _UI_EVIDENCE_MAX_ITEMS
        limit = _UI_EVIDENCE_MAX_ITEMS - int(truncated)
        result = [_normalize_ui_evidence(item, depth + 1) for item in islice(value, limit)]
        if truncated:
            result.append(_UI_EVIDENCE_TRUNCATED)
        return result
    if not isinstance(value, Mapping):
        return value

    items, truncated = _ui_evidence_items(value)
    result = {}
    for key, child in items:
        if key == 'url' and isinstance(child, str) and child.startswith('data:'):
            continue
        if key == 'evidence_reference' and isinstance(child, Mapping):
            result[key] = _normalize_ui_evidence_reference(child, depth + 1)
        else:
            result[key] = _normalize_ui_evidence(child, depth + 1)
    if truncated:
        result[_ui_evidence_truncation_key(result)] = _UI_EVIDENCE_TRUNCATED
    return result


def _normalize_ui_evidence_reference(reference, depth):
    if depth > _UI_EVIDENCE_MAX_DEPTH:
        return _UI_EVIDENCE_TRUNCATED
    items, truncated = _ui_evidence_items(reference, evidence_reference=True)
    result = {}
    for key, child in items:
        if key == 'url' and isinstance(child, str) and child.startswith('data:'):
            continue
        output_key = {'uri': 'source_uri', 'metadata': 'provenance'}.get(key, key)
        result[output_key] = _normalize_ui_evidence(child, depth + 1)
    if 'source_uri' not in result:
        result['source_uri'] = ''
    if 'provenance' not in result:
        result['provenance'] = {}
    if truncated:
        result[_ui_evidence_truncation_key(result)] = _UI_EVIDENCE_TRUNCATED
    return result


def normalize_ui_evidence(value):
    """Bound and normalize runner evidence while retaining byte-reference provenance."""
    return _normalize_ui_evidence(value)


def _ui_source(reference, binding, actor):
    return resolve_native_source(reference, binding.source_project_id, actor)


def _ui_execute(source, reference, binding, actor, metadata, cancel_check=None):
    from apps.ui_automation.models import TestSuite
    from apps.ui_automation.test_executor import TestExecutor
    if cancel_check:
        cancel_check()
    engine, browser = str(metadata.get('engine') or 'playwright'), str(metadata.get('browser') or 'chrome')
    suite = source
    if reference.kind == 'case':
        suite = TestSuite.objects.create(project=source.project, name=f'[unified:{source.pk}] {source.name}')
        suite.test_cases.add(source)
    suite.execution_status = 'running'
    suite.save(update_fields=['execution_status', 'updated_at'])
    if cancel_check:
        cancel_check()
    runner = TestExecutor(test_suite=suite, engine=engine, browser=browser,
                          headless=bool(metadata.get('headless', False)), executed_by=actor,
                          cancel_check=cancel_check)
    runner.run()
    suite.refresh_from_db()
    success = suite.execution_status == 'passed'
    return success, normalize_ui_evidence({
        'status': 'passed' if success else 'failed', 'test_case_count': suite.suite_test_cases.count(),
        'passed_count': suite.passed_count, 'failed_count': suite.failed_count,
        'engine': engine, 'browser': browser, 'test_cases': runner.results,
    })


def _app_source(reference, binding, actor):
    return resolve_native_source(reference, binding.source_project_id, actor)


def _app_execute(source, reference, binding, actor, cancel_check=None):
    from apps.app_automation.device_pool import acquire_device, release_device
    from apps.app_automation.models import AppTestExecution
    from apps.app_automation.tasks import execute_app_suite_task, execute_app_test_task
    metadata = dict(binding.metadata or {})
    if metadata.get('approved') is not True:
        raise SourceBindingError('APP unified execution requires an approved source binding')
    capabilities = metadata.get('capabilities') or {}
    if not isinstance(capabilities, dict):
        raise SourceBindingError('APP binding capabilities must be an object')
    if cancel_check:
        cancel_check()
    device, lease_token = acquire_device(actor, capabilities=capabilities, lease_seconds=int(metadata.get('lease_seconds') or 3600))
    try:
        if reference.kind == 'case':
            if cancel_check:
                cancel_check()
            execution = AppTestExecution.objects.create(test_case=source, device=device, user=actor,
                                                       status='pending', device_lease_generation=device.lease_generation)
            if cancel_check:
                cancel_check()
            execute_app_test_task.run(execution.pk, package_name=source.app_package.package_name if source.app_package else '',
                                      lease_generation=device.lease_generation, release_device_lease=False,
                                      cancel_check=cancel_check)
            execution.refresh_from_db()
            return execution.result == 'passed', {'status': execution.status, 'result': execution.result,
                                                   'execution_id': execution.pk, 'device_id': device.pk}
        cases = list(source.suite_cases.select_related('test_case__app_package'))
        if not cases:
            raise SourceBindingError('APP suite has no test cases')
        packages = {item.test_case.app_package.package_name for item in cases if item.test_case.app_package}
        if len(packages) > 1:
            raise SourceBindingError('APP suite contains multiple app packages')
        executions = [AppTestExecution.objects.create(test_case=item.test_case, test_suite=source, device=device,
                                                     user=actor, status='pending', device_lease_generation=device.lease_generation) for item in cases]
        ids = [item.pk for item in executions]
        if cancel_check:
            cancel_check()
        execute_app_suite_task.run(suite_id=source.pk, execution_ids=ids, package_name=next(iter(packages), ''),
                                   lease_generation=device.lease_generation, release_device_lease=False,
                                   cancel_check=cancel_check)
        source.refresh_from_db()
        return source.execution_result == 'passed', {'status': source.execution_status, 'result': source.execution_result,
                                                      'execution_ids': ids, 'device_id': device.pk}
    finally:
        release_device(device.pk, actor, lease_token)


def _performance_execute(job, attempt, reference, binding, actor, claim_id, cancel_check):
    from apps.perf_testing.services import executor
    if reference.kind != 'scenario' or (binding.metadata or {}).get('approved') is not True:
        raise SourceBindingError('Performance execution requires an approved scenario binding')
    scenario = resolve_native_source(reference, binding.source_project_id, actor)
    if cancel_check:
        cancel_check()
    execution, check = executor.start_execution(scenario, user=actor, trigger_type='API', script_ref={
        'unified_execution': {'job_id': job.pk, 'attempt_id': attempt.pk, 'worker_claim_id': claim_id},
    })
    if execution is None:
        raise SourceBindingError('Performance preflight failed: ' + '; '.join((check or {}).get('errors') or []))
    try:
        job.refresh_from_db()
        if not job.final_attempt_id:
            cancel_check()
    except Exception:
        executor.stop_execution(execution, graceful=False)
        raise
    return {'job_id': job.pk, 'attempt_id': attempt.pk, 'status': 'started', 'performance_execution_id': execution.pk}


def _execute(task, source_type, job_id, attempt_id, variant_id):
    with _job_attempt(job_id, attempt_id, variant_id) as (job, attempt):
        claim_id = _start(job, attempt, getattr(getattr(task, 'request', None), 'id', ''))
        if not claim_id:
            return {'job_id': job_id, 'attempt_id': attempt_id, 'status': 'ignored'}
        try:
            with ExecutionLeaseHeartbeat(job.pk, attempt.pk, claim_id) as heartbeat:
                reference, binding = resolve_source_binding(job)
                if reference.source_type != source_type:
                    raise SourceBindingError('Worker received another source type')
                payload = _variant_payload(attempt, source_type)
                actor = job.requested_by or binding.created_by
                if actor is None:
                    raise SourceBindingError('Unified execution requires an execution user')
                def check_execution_access():
                    heartbeat.raise_if_lost()
                    resolve_native_source(reference, binding.source_project_id, actor)

                check_execution_access()
                if source_type == 'api':
                    source = _api_source(reference, binding, actor)
                    check_execution_access()
                    success, result = _api_execute(source, reference, actor, payload, check_execution_access)
                elif source_type == 'ui':
                    metadata = dict(job.environment_snapshot or {})
                    metadata.update(payload)
                    source = _ui_source(reference, binding, actor)
                    check_execution_access()
                    success, result = _ui_execute(source, reference, binding, actor, metadata, check_execution_access)
                elif source_type == 'app':
                    source = _app_source(reference, binding, actor)
                    check_execution_access()
                    success, result = _app_execute(source, reference, binding, actor, check_execution_access)
                else:
                    return _performance_execute(job, attempt, reference, binding, actor, claim_id, check_execution_access)
                heartbeat.raise_if_lost()
                status = 'passed' if success else 'failed'
                _finish(job, attempt, claim_id=claim_id, status=status, result=result)
                return {'job_id': job_id, 'attempt_id': attempt_id, 'status': status}
        except Exception as exc:
            job.refresh_from_db()
            status = 'cancelled' if job.status == 'cancel_requested' else 'failed'
            safe_error = redact_result_payload(str(exc))[:4000]
            logger.warning('unified execution failed: job=%s attempt=%s type=%s', job_id, attempt_id, type(exc).__name__)
            try:
                _finish(job, attempt, claim_id=claim_id, status=status,
                        error_type='configuration' if isinstance(exc, SourceBindingError) else 'infrastructure', error_message=safe_error)
            except Exception:
                logger.exception('unified failure result could not be recorded: job=%s attempt=%s', job_id, attempt_id)
            return {'job_id': job_id, 'attempt_id': attempt_id, 'status': status, 'error': safe_error}


@shared_task(bind=True, ignore_result=False)
def execute_api_source(self, job_id, attempt_id, execution_job_id=None, source_type='api', source_id='', variant_id=None):
    return _execute(self, 'api', job_id, attempt_id, variant_id)


@shared_task(bind=True, ignore_result=False)
def execute_ui_source(self, job_id, attempt_id, execution_job_id=None, source_type='ui', source_id='', variant_id=None):
    return _execute(self, 'ui', job_id, attempt_id, variant_id)


@shared_task(bind=True, ignore_result=False)
def execute_app_source(self, job_id, attempt_id, execution_job_id=None, source_type='app', source_id='', variant_id=None):
    return _execute(self, 'app', job_id, attempt_id, variant_id)


@shared_task(bind=True, ignore_result=False)
def execute_performance_source(self, job_id, attempt_id, execution_job_id=None, source_type='performance', source_id='', variant_id=None):
    return _execute(self, 'performance', job_id, attempt_id, variant_id)


__all__ = ['execute_api_source', 'execute_ui_source', 'execute_app_source', 'execute_performance_source']
