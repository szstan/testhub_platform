"""Source-specific Celery workers for project-bound unified execution jobs."""

from __future__ import annotations

import logging
from contextlib import contextmanager

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import ExecutionAttempt, ExecutionJob
from .result_events import ResultEventIngestor
from .source_contract import SourceBindingError, SourceReference, resolve_source_binding
from .state_machine import ExecutionStatus

logger = logging.getLogger(__name__)


@contextmanager
def _job_attempt(job_id, attempt_id):
    job = ExecutionJob.objects.select_related('requested_by').filter(pk=job_id).first()
    if job is None:
        raise SourceBindingError(f'unified execution job {job_id!r} was not found')
    attempt = ExecutionAttempt.objects.filter(pk=attempt_id, job_id=job.pk).first()
    if attempt is None:
        raise SourceBindingError(f'unified execution attempt {attempt_id!r} does not belong to job {job.pk}')
    yield job, attempt


def _binding_project_id(binding, source) -> str:
    expected = str(binding.source_project_id or '').strip()
    actual = str(getattr(source, 'project_id', '') or '').strip()
    if not expected or not actual or expected != actual:
        raise SourceBindingError(
            f'source project binding mismatch for {binding.source_type}:{binding.source_id}'
        )
    return actual


def _event(job, event_type, payload, key):
    return ResultEventIngestor.ingest(job, event_type, payload, idempotency_key=key)


def _claim_attempt(job_id, attempt_id, task_id):
    claim_id = str(task_id or '').strip() or f'worker:{attempt_id}'
    with transaction.atomic():
        locked_job = ExecutionJob.objects.select_for_update().get(pk=job_id)
        locked_attempt = ExecutionAttempt.objects.select_for_update().get(pk=attempt_id, job_id=job_id)
        if locked_job.final_attempt_id or locked_attempt.is_terminal:
            return False
        if locked_attempt.worker_claim_id:
            return False
        if locked_attempt.dispatch_id and task_id and locked_attempt.dispatch_id != str(task_id):
            raise SourceBindingError('worker task id does not match the fenced dispatch id')
        locked_attempt.worker_claim_id = claim_id
        locked_attempt.worker_claimed_at = timezone.now()
        locked_attempt.save(update_fields=['worker_claim_id', 'worker_claimed_at', 'updated_at'])
        return True


def _start(job, attempt, task_id=''):
    if job.status == ExecutionStatus.CANCEL_REQUESTED:
        _event(
            job,
            'run_finished',
            {'attempt': attempt.attempt_number, 'status': 'cancelled', 'error_type': 'cancelled'},
            f'worker:{attempt.pk}:cancelled',
        )
        return False
    if not _claim_attempt(job.pk, attempt.pk, task_id):
        return False
    _event(job, 'run_started', {'attempt': attempt.attempt_number}, f'worker:{attempt.pk}:started')
    return True


def _finish(job, attempt, *, status, result=None, error_type='', error_message=''):
    _event(
        job,
        'run_finished',
        {
            'attempt': attempt.attempt_number,
            'status': status,
            'result': result or {},
            'error_type': error_type,
            'error_message': str(error_message or '')[:4000],
        },
        f'worker:{attempt.pk}:finished',
    )


def _api_source(reference: SourceReference, binding):
    from apps.api_testing.models import ApiRequest, TestSuite

    model = TestSuite if reference.kind == 'suite' else ApiRequest if reference.kind == 'request' else None
    if model is None:
        raise SourceBindingError(f'unsupported API source kind: {reference.kind}')
    if reference.kind == 'suite':
        source = TestSuite.objects.select_related('project', 'environment').filter(pk=reference.object_id).first()
    else:
        source = ApiRequest.objects.select_related('collection').filter(pk=reference.object_id).first()
    if source is None:
        raise SourceBindingError(f'API source {reference.canonical!r} was not found')
    source_project_id = source.project_id if reference.kind == 'suite' else getattr(source.collection, 'project_id', None)
    if str(binding.source_project_id or '') != str(source_project_id or ''):
        raise SourceBindingError(f'API source project binding mismatch for {reference.canonical!r}')
    return source


def _api_execute(source, reference, actor):
    from apps.api_testing.utils import execute_api_request, execute_test_suite

    if reference.kind == 'suite':
        result = execute_test_suite(source, source.environment, actor)
        success = bool(result and result.get('success')) and not int(result.get('failed_count') or 0)
    else:
        result = execute_api_request(source, None, actor)
        success = bool(result and result.get('success'))
    return success, result or {}


@shared_task(bind=True, ignore_result=False)
def execute_api_source(self, job_id, attempt_id, execution_job_id=None, source_type='api', source_id='', variant_id=None):
    """Run one explicitly bound API source and publish unified result events."""

    del execution_job_id, source_type, source_id, variant_id
    with _job_attempt(job_id, attempt_id) as (job, attempt):
        try:
            reference, binding = resolve_source_binding(job)
            if reference.source_type != 'api':
                raise SourceBindingError('API worker received a non-API source')
            source = _api_source(reference, binding)
            actor = job.requested_by or binding.created_by
            if actor is None:
                raise SourceBindingError('unified execution requires a source execution user')
            if not _start(job, attempt, getattr(getattr(self, 'request', None), 'id', '')):
                return {'job_id': job.pk, 'attempt_id': attempt.pk, 'status': 'cancelled'}
            success, result = _api_execute(source, reference, actor)
            _finish(job, attempt, status='passed' if success else 'failed', result=result)
            return {'job_id': job.pk, 'attempt_id': attempt.pk, 'status': 'passed' if success else 'failed'}
        except Exception as exc:  # noqa: BLE001 - close the durable attempt on every worker error
            logger.exception('unified API execution failed: job=%s attempt=%s', job_id, attempt_id)
            try:
                _finish(job, attempt, status='failed', error_type='configuration' if isinstance(exc, SourceBindingError) else 'infrastructure', error_message=str(exc))
            except Exception:  # noqa: BLE001 - preserve original worker failure
                logger.exception('unified API failure result could not be recorded: job=%s attempt=%s', job_id, attempt_id)
            return {'job_id': job_id, 'attempt_id': attempt_id, 'status': 'failed', 'error': str(exc)[:4000]}


def _ui_source(reference: SourceReference, binding):
    from apps.ui_automation.models import TestCase, TestSuite

    model = TestSuite if reference.kind == 'suite' else TestCase if reference.kind == 'case' else None
    if model is None:
        raise SourceBindingError(f'unsupported UI source kind: {reference.kind}')
    source = model.objects.select_related('project').filter(pk=reference.object_id).first()
    if source is None:
        raise SourceBindingError(f'UI source {reference.canonical!r} was not found')
    _binding_project_id(binding, source)
    return source


def _ui_execute(source, reference, binding, actor, metadata):
    from apps.ui_automation.models import TestSuite
    from apps.ui_automation.test_executor import TestExecutor

    engine = str(metadata.get('engine') or 'playwright')
    browser = str(metadata.get('browser') or 'chrome')
    headless = bool(metadata.get('headless', False))
    suite = source
    if reference.kind == 'case':
        # Preserve this temporary suite because TestExecutor creates audit rows
        # with a cascading FK to the suite.
        suite = TestSuite.objects.create(
            project=source.project,
            name=f'[unified:{source.pk}] {source.name}',
        )
        suite.test_cases.add(source)
    suite.execution_status = 'running'
    suite.save(update_fields=['execution_status', 'updated_at'])
    TestExecutor(
        test_suite=suite,
        engine=engine,
        browser=browser,
        headless=headless,
        executed_by=actor,
    ).run()
    suite.refresh_from_db()
    success = suite.execution_status == 'passed'
    return success, {
        'status': 'passed' if success else 'failed',
        'test_case_count': suite.suite_test_cases.count(),
        'passed_count': suite.passed_count,
        'failed_count': suite.failed_count,
        'engine': engine,
        'browser': browser,
    }


@shared_task(bind=True, ignore_result=False)
def execute_ui_source(self, job_id, attempt_id, execution_job_id=None, source_type='ui', source_id='', variant_id=None):
    """Run one explicitly bound UI suite/case and publish unified result events."""

    del execution_job_id, source_type, source_id, variant_id
    with _job_attempt(job_id, attempt_id) as (job, attempt):
        try:
            reference, binding = resolve_source_binding(job)
            if reference.source_type != 'ui':
                raise SourceBindingError('UI worker received a non-UI source')
            source = _ui_source(reference, binding)
            actor = job.requested_by or binding.created_by
            if actor is None:
                raise SourceBindingError('unified execution requires a source execution user')
            if not _start(job, attempt, getattr(getattr(self, 'request', None), 'id', '')):
                return {'job_id': job.pk, 'attempt_id': attempt.pk, 'status': 'cancelled'}
            success, result = _ui_execute(source, reference, binding, actor, job.environment_snapshot or {})
            _finish(job, attempt, status='passed' if success else 'failed', result=result)
            return {'job_id': job.pk, 'attempt_id': attempt.pk, 'status': 'passed' if success else 'failed'}
        except Exception as exc:  # noqa: BLE001 - close the durable attempt on every worker error
            logger.exception('unified UI execution failed: job=%s attempt=%s', job_id, attempt_id)
            try:
                _finish(job, attempt, status='failed', error_type='configuration' if isinstance(exc, SourceBindingError) else 'infrastructure', error_message=str(exc))
            except Exception:  # noqa: BLE001
                logger.exception('unified UI failure result could not be recorded: job=%s attempt=%s', job_id, attempt_id)
            return {'job_id': job_id, 'attempt_id': attempt_id, 'status': 'failed', 'error': str(exc)[:4000]}


def _app_source(reference: SourceReference, binding):
    from apps.app_automation.models import AppTestCase, AppTestSuite

    if reference.kind == 'case':
        source = AppTestCase.objects.select_related('project', 'app_package').filter(pk=reference.object_id).first()
    elif reference.kind == 'suite':
        source = AppTestSuite.objects.select_related('project').prefetch_related(
            'suite_cases__test_case__app_package'
        ).filter(pk=reference.object_id).first()
    else:
        raise SourceBindingError(f'unsupported APP source kind: {reference.kind}')
    if source is None:
        raise SourceBindingError(f'APP source {reference.canonical!r} was not found')
    _binding_project_id(binding, source)
    return source


def _app_execute(source, reference, binding, actor):
    from apps.app_automation.device_pool import acquire_device, release_device
    from apps.app_automation.models import AppTestExecution
    from apps.app_automation.tasks import execute_app_suite_task, execute_app_test_task

    metadata = dict(binding.metadata or {})
    if metadata.get('approved') is not True:
        raise SourceBindingError('APP unified execution requires an approved source binding')
    capabilities = metadata.get('capabilities') or {}
    if not isinstance(capabilities, dict):
        raise SourceBindingError('APP binding capabilities must be an object')
    device, lease_token = acquire_device(
        actor,
        capabilities=capabilities,
        lease_seconds=int(metadata.get('lease_seconds') or 3600),
    )
    execution_ids = []
    try:
        package_name = ''
        if reference.kind == 'case':
            package_name = source.app_package.package_name if source.app_package else ''
            execution = AppTestExecution.objects.create(
                test_case=source,
                device=device,
                user=actor,
                status='pending',
            )
            execution_ids = [execution.pk]
            execute_app_test_task.run(execution.pk, package_name=package_name)
            execution.refresh_from_db()
            success = execution.result == 'passed'
            result = {
                'status': execution.status,
                'result': execution.result,
                'execution_id': execution.pk,
                'device_id': device.pk,
            }
        else:
            cases = list(source.suite_cases.select_related('test_case__app_package').all())
            if not cases:
                raise SourceBindingError('APP suite has no test cases')
            package_names = {
                item.test_case.app_package.package_name
                for item in cases
                if item.test_case.app_package
            }
            if len(package_names) > 1:
                raise SourceBindingError('APP suite contains multiple app packages')
            package_name = next(iter(package_names), '')
            executions = [AppTestExecution.objects.create(
                test_case=item.test_case,
                test_suite=source,
                device=device,
                user=actor,
                status='pending',
            ) for item in cases]
            execution_ids = [item.pk for item in executions]
            execute_app_suite_task.run(
                suite_id=source.pk,
                execution_ids=execution_ids,
                package_name=package_name,
            )
            source.refresh_from_db()
            success = source.execution_result == 'passed'
            result = {
                'status': source.execution_status,
                'result': source.execution_result,
                'execution_ids': execution_ids,
                'device_id': device.pk,
            }
        return success, result
    finally:
        release_device(device.pk, actor, lease_token)


@shared_task(bind=True, ignore_result=False)
def execute_app_source(self, job_id, attempt_id, execution_job_id=None, source_type='app', source_id='', variant_id=None):
    """Run an approved, capability-bound APP source and publish unified events."""

    del execution_job_id, source_type, source_id, variant_id
    with _job_attempt(job_id, attempt_id) as (job, attempt):
        try:
            reference, binding = resolve_source_binding(job)
            if reference.source_type != 'app':
                raise SourceBindingError('APP worker received a non-APP source')
            source = _app_source(reference, binding)
            actor = job.requested_by or binding.created_by
            if actor is None:
                raise SourceBindingError('unified APP execution requires an execution user')
            if not _start(job, attempt, getattr(getattr(self, 'request', None), 'id', '')):
                return {'job_id': job.pk, 'attempt_id': attempt.pk, 'status': 'cancelled'}
            success, result = _app_execute(source, reference, binding, actor)
            _finish(job, attempt, status='passed' if success else 'failed', result=result)
            return {'job_id': job.pk, 'attempt_id': attempt.pk, 'status': 'passed' if success else 'failed'}
        except Exception as exc:  # noqa: BLE001 - release is handled by _app_execute
            logger.exception('unified APP execution failed: job=%s attempt=%s', job_id, attempt_id)
            try:
                _finish(job, attempt, status='failed', error_type='configuration' if isinstance(exc, SourceBindingError) else 'infrastructure', error_message=str(exc))
            except Exception:  # noqa: BLE001
                logger.exception('unified APP failure result could not be recorded: job=%s attempt=%s', job_id, attempt_id)
            return {'job_id': job_id, 'attempt_id': attempt_id, 'status': 'failed', 'error': str(exc)[:4000]}


@shared_task(bind=True, ignore_result=False)
def execute_performance_source(self, job_id, attempt_id, execution_job_id=None, source_type='performance', source_id='', variant_id=None):
    """Start a bound performance scenario; the child publishes final completion."""

    del execution_job_id, source_type, source_id, variant_id
    with _job_attempt(job_id, attempt_id) as (job, attempt):
        try:
            from apps.perf_testing.models import PerfScenario
            from apps.perf_testing.services import executor

            reference, binding = resolve_source_binding(job)
            if reference.source_type != 'performance' or reference.kind != 'scenario':
                raise SourceBindingError('performance worker received an invalid source')
            if (binding.metadata or {}).get('approved') is not True:
                raise SourceBindingError('performance unified execution requires an approved source binding')
            scenario = PerfScenario.objects.select_related('project').filter(pk=reference.object_id).first()
            if scenario is None:
                raise SourceBindingError(f'performance source {reference.canonical!r} was not found')
            _binding_project_id(binding, scenario)
            actor = job.requested_by or binding.created_by
            if actor is None:
                raise SourceBindingError('unified performance execution requires an execution user')
            if not _start(job, attempt, getattr(getattr(self, 'request', None), 'id', '')):
                return {'job_id': job.pk, 'attempt_id': attempt.pk, 'status': 'cancelled'}
            execution, check = executor.start_execution(
                scenario,
                user=actor,
                trigger_type='API',
                script_ref={
                    'unified_execution': {
                        'job_id': job.pk,
                        'attempt_id': attempt.pk,
                    },
                },
            )
            if execution is None:
                raise SourceBindingError(
                    'performance preflight failed: ' + '; '.join((check or {}).get('errors') or [])
                )
            return {
                'job_id': job.pk,
                'attempt_id': attempt.pk,
                'status': 'started',
                'performance_execution_id': execution.pk,
            }
        except Exception as exc:  # noqa: BLE001 - explicit configuration/infrastructure result
            logger.warning('unified performance execution unavailable: job=%s attempt=%s error=%s', job_id, attempt_id, exc)
            try:
                _finish(job, attempt, status='failed', error_type='configuration', error_message=str(exc))
            except Exception:  # noqa: BLE001
                logger.exception('unified performance failure result could not be recorded: job=%s attempt=%s', job_id, attempt_id)
            return {'job_id': job_id, 'attempt_id': attempt_id, 'status': 'failed', 'error': str(exc)[:4000]}


__all__ = ['execute_api_source', 'execute_ui_source', 'execute_app_source', 'execute_performance_source']
