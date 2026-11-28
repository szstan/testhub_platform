"""Shared durable submission primitives for the UI workbench."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import defaultdict
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    TestCaseExecution,
    TestExecution,
    TestCaseStep,
    TestSuiteTestCase,
    UiExecutionSubmission,
)

SUPPORTED_BROWSERS = {
    'playwright': {'chrome', 'edge', 'firefox'},
    'selenium': {'chrome', 'edge', 'firefox'},
}
WORKBENCH_DISPATCH_LEASE_SECONDS = 30
WORKBENCH_WORKER_LOST_MESSAGE = (
    'UI worker heartbeat expired; external effects are unknown. '
    'Execution was interrupted and will not be replayed.'
)
logger = logging.getLogger(__name__)


class SubmissionValidationError(ValueError):
    pass


def parse_client_request_id(value):
    if value in (None, ''):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SubmissionValidationError('client_request_id must be a UUID') from exc


def normalize_run_options(data, user, *, default_headless=True, enforce_permissions=True):
    engine = str(data.get('engine', 'playwright') or '').lower()
    browser = str(data.get('browser', 'chrome') or '').lower()
    headless = data.get('headless', default_headless)
    if engine not in SUPPORTED_BROWSERS:
        raise SubmissionValidationError('Unsupported engine')
    if browser not in SUPPORTED_BROWSERS[engine]:
        raise SubmissionValidationError('Unsupported browser for this engine')
    if not isinstance(headless, bool):
        raise SubmissionValidationError('headless must be a boolean')

    if engine == 'selenium' and enforce_permissions:
        from .access import selenium_compatibility_enabled

        if not selenium_compatibility_enabled(user):
            raise PermissionError('Selenium compatibility mode is disabled')

    return {'engine': engine, 'browser': browser, 'headless': headless}


def _element_snapshot(element):
    if element is None:
        return None
    return {
        'id': element.pk,
        'name': element.name,
        'locator_strategy': element.locator_strategy.name if element.locator_strategy else 'css',
        'locator_value': element.locator_value,
        'wait_timeout': element.wait_timeout,
        'force_action': element.force_action,
    }


def case_steps_snapshot(case, rows=None):
    steps = []
    if rows is None:
        rows = case.steps.select_related('element', 'element__locator_strategy').order_by('step_number')
    for step in rows:
        if step.element_id and step.element.project_id != case.project_id:
            raise SubmissionValidationError('用例步骤引用了其他项目的元素，无法安全创建执行快照。')
        steps.append({
            'id': step.pk,
            'step_number': step.step_number,
            'action_type': step.action_type,
            'description': step.description,
            'input_value': step.input_value,
            'wait_time': step.wait_time,
            'assert_type': step.assert_type,
            'assert_value': step.assert_value,
            'element_data': _element_snapshot(step.element),
            'element': {
                'id': step.element_id,
                'name': step.element.name,
                'locator_value': step.element.locator_value,
                'locator_strategy': step.element.locator_strategy.name if step.element.locator_strategy else 'css',
            } if step.element else None,
        })
    return steps


def case_request_snapshot(case, options):
    return {
        'version': 1,
        'config': dict(options),
        'case': {'id': case.pk, 'name': case.name, 'project_id': case.project_id},
        'base_url': case.project.base_url,
        'steps': case_steps_snapshot(case),
    }


def suite_request_snapshot(suite, options):
    case_rows = list(TestSuiteTestCase.objects.filter(test_suite=suite).select_related('test_case').order_by('order', 'pk'))
    if not case_rows:
        raise SubmissionValidationError('该测试套件未包含任何测试用例，无法执行')
    if any(relation.test_case.project_id != suite.project_id for relation in case_rows):
        raise SubmissionValidationError('测试套件包含其他项目的测试用例，无法安全创建执行快照。')
    steps_by_case = defaultdict(list)
    rows = TestCaseStep.objects.filter(
        test_case_id__in=[relation.test_case_id for relation in case_rows],
    ).select_related('element', 'element__locator_strategy').order_by('test_case_id', 'step_number', 'pk')
    for step in rows:
        steps_by_case[step.test_case_id].append(step)
    cases = []
    for relation in case_rows:
        case = relation.test_case
        cases.append({
            'id': case.pk,
            'name': case.name,
            'project_id': case.project_id,
            'steps': case_steps_snapshot(case, steps_by_case[case.pk]),
        })
    return {
        'version': 1,
        'config': dict(options),
        'suite': {'id': suite.pk, 'name': suite.name, 'project_id': suite.project_id},
        'base_url': suite.project.base_url,
        'cases': cases,
    }


def request_fingerprint(kind, resource_id, options, rerun_of=None, rerun_kind=''):
    value = {
        'kind': kind,
        'resource_id': int(resource_id),
        'options': options,
        'rerun_of': int(rerun_of) if rerun_of is not None else None,
        'rerun_kind': rerun_kind or '',
    }
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _submission_result(existing, fingerprint, kind, resource_id):
    execution = existing.case_execution if existing.kind == 'case' else existing.suite_execution
    original_id = execution.pk if execution is not None else None
    matches = (
        existing.kind == kind
        and existing.resource_id == int(resource_id)
        and existing.request_fingerprint == fingerprint
        and execution is not None
    )
    return execution, not matches, original_id


def find_existing_submission(actor, kind, resource_id, client_request_id, options, *, rerun_of=None, rerun_kind=''):
    """Compare an owned identity before inspecting mutable execution inputs."""
    if client_request_id is None:
        return None
    existing = UiExecutionSubmission.objects.filter(
        actor=actor, client_request_id=client_request_id,
    ).select_related('case_execution', 'suite_execution').first()
    if existing is None:
        return None
    fingerprint = request_fingerprint(kind, resource_id, options, rerun_of, rerun_kind)
    return _submission_result(existing, fingerprint, kind, resource_id)


def create_case_execution(case, actor, options, *, client_request_id=None, rerun_of=None, rerun_kind=''):
    fingerprint = request_fingerprint('case', case.pk, options, rerun_of.pk if rerun_of else None, rerun_kind)
    try:
        with transaction.atomic():
            if client_request_id is not None:
                existing = UiExecutionSubmission.objects.select_for_update().filter(
                    actor=actor, client_request_id=client_request_id,
                ).select_related('case_execution', 'suite_execution').first()
                if existing:
                    execution, conflict, original_id = _submission_result(
                        existing, fingerprint, 'case', case.pk,
                    )
                    return execution, True, conflict, original_id

            snapshot = case_request_snapshot(case, options)
            execution = TestCaseExecution.objects.create(
                test_case=case,
                project=case.project,
                execution_source='manual',
                status='pending',
                engine=options['engine'],
                browser=options['browser'],
                headless=options['headless'],
                created_by=actor,
                dispatch_claim=uuid.uuid4(),
                request_snapshot=snapshot,
                rerun_of=rerun_of if rerun_kind == 'case' else None,
                rerun_kind=rerun_kind,
                rerun_origin_id=rerun_of.pk if rerun_of is not None else None,
            )
            if client_request_id is not None:
                UiExecutionSubmission.objects.create(
                    actor=actor,
                    client_request_id=client_request_id,
                    kind='case',
                    resource_id=case.pk,
                    request_fingerprint=fingerprint,
                    case_execution=execution,
                )
            return execution, False, False, execution.pk
    except IntegrityError:
        if client_request_id is None:
            raise
        existing = UiExecutionSubmission.objects.filter(
            actor=actor, client_request_id=client_request_id,
        ).select_related('case_execution', 'suite_execution').first()
        if existing is None:
            raise
        execution, conflict, original_id = _submission_result(existing, fingerprint, 'case', case.pk)
        return execution, True, conflict, original_id


def create_suite_execution(suite, actor, options, *, client_request_id=None, rerun_of=None, rerun_kind=''):
    fingerprint = request_fingerprint('suite', suite.pk, options, rerun_of.pk if rerun_of else None, rerun_kind)
    try:
        with transaction.atomic():
            if client_request_id is not None:
                existing = UiExecutionSubmission.objects.select_for_update().filter(
                    actor=actor, client_request_id=client_request_id,
                ).select_related('case_execution', 'suite_execution').first()
                if existing:
                    execution, conflict, original_id = _submission_result(
                        existing, fingerprint, 'suite', suite.pk,
                    )
                    return execution, True, conflict, original_id

            snapshot = suite_request_snapshot(suite, options)
            execution = TestExecution.objects.create(
                project=suite.project,
                test_suite=suite,
                status='PENDING',
                total_cases=len(snapshot['cases']),
                executed_by=actor,
                engine=options['engine'],
                browser=options['browser'],
                headless=options['headless'],
                dispatch_claim=uuid.uuid4(),
                request_snapshot=snapshot,
                rerun_of=rerun_of,
                rerun_kind=rerun_kind,
            )
            if client_request_id is not None:
                UiExecutionSubmission.objects.create(
                    actor=actor,
                    client_request_id=client_request_id,
                    kind='suite',
                    resource_id=suite.pk,
                    request_fingerprint=fingerprint,
                    suite_execution=execution,
                )
            return execution, False, False, execution.pk
    except IntegrityError:
        if client_request_id is None:
            raise
        existing = UiExecutionSubmission.objects.filter(
            actor=actor, client_request_id=client_request_id,
        ).select_related('case_execution', 'suite_execution').first()
        if existing is None:
            raise
        execution, conflict, original_id = _submission_result(existing, fingerprint, 'suite', suite.pk)
        return execution, True, conflict, original_id


def resolve_submission(actor, kind, resource_id, client_request_id):
    try:
        request_id = parse_client_request_id(client_request_id)
    except SubmissionValidationError:
        return None
    if request_id is None:
        return None
    submission = UiExecutionSubmission.objects.filter(
        actor=actor,
        kind=kind,
        resource_id=resource_id,
        client_request_id=request_id,
    ).select_related('case_execution', 'suite_execution').first()
    if submission is None:
        return None
    return submission.case_execution if kind == 'case' else submission.suite_execution


def _status_for(kind, execution):
    return execution.status


def acceptance_payload(kind, execution, *, replayed=False, accepted=True, failure_stage=''):
    payload = {
        'accepted': bool(accepted),
        'execution_id': execution.pk,
        'status': _status_for(kind, execution),
        'kind': kind,
    }
    if replayed:
        payload['replayed'] = True
    if failure_stage:
        payload['failure_stage'] = failure_stage
        payload['message'] = execution.error_message or '执行未能派发'
    return payload


def _dispatch_model(kind):
    if kind == 'case':
        return TestCaseExecution, 'pending'
    if kind == 'suite':
        return TestExecution, 'PENDING'
    raise ValueError(f'unsupported workbench execution kind: {kind}')


def dispatch_execution(kind, execution, *, now=None):
    """Publish once per persisted delivery lease; workers still fence execution by claim."""
    from .tasks import execute_workbench_case, execute_workbench_suite

    model, pending_status = _dispatch_model(kind)
    if not execution.dispatch_claim:
        return False

    now = now or timezone.now()
    lease_token = uuid.uuid4()
    lease_expiry = now + timedelta(seconds=WORKBENCH_DISPATCH_LEASE_SECONDS)
    lease_available = (
        Q(dispatch_delivery_token__isnull=True)
        | Q(dispatch_delivery_expires_at__isnull=True)
        | Q(dispatch_delivery_expires_at__lte=now)
    )
    reserved = model.objects.filter(
        pk=execution.pk,
        dispatch_claim=execution.dispatch_claim,
        status=pending_status,
        cancel_requested_at__isnull=True,
        dispatch_enqueued_at__isnull=True,
    ).filter(lease_available).update(
        dispatch_delivery_token=lease_token,
        dispatch_delivery_expires_at=lease_expiry,
    )
    if not reserved:
        return False

    task = execute_workbench_case if kind == 'case' else execute_workbench_suite
    task.apply_async(args=(execution.pk, str(execution.dispatch_claim)), retry=False)
    model.objects.filter(
        pk=execution.pk,
        dispatch_claim=execution.dispatch_claim,
        dispatch_delivery_token=lease_token,
        dispatch_enqueued_at__isnull=True,
    ).update(
        dispatch_enqueued_at=timezone.now(),
        dispatch_delivery_token=None,
        dispatch_delivery_expires_at=None,
    )
    return True


def reconcile_stale_workbench_executions(limit=20, now=None) -> int:
    """Terminalize expired workbench workers without replaying external actions."""
    limit = max(int(limit), 0)
    if not limit:
        return 0

    query_time = now or timezone.now()
    candidates = []
    for kind, (model, running_status) in (
        ('case', (TestCaseExecution, 'running')),
        ('suite', (TestExecution, 'RUNNING')),
    ):
        rows = model.objects.filter(
            status=running_status,
            dispatch_claim__isnull=False,
            worker_nonce__isnull=False,
            worker_lease_expires_at__lte=query_time,
        ).order_by('worker_lease_expires_at', 'pk').values(
            'pk', 'dispatch_claim', 'worker_nonce', 'worker_lease_expires_at',
        )[:limit]
        candidates.extend((kind, row) for row in rows)

    candidates.sort(key=lambda item: (item[1]['worker_lease_expires_at'], item[1]['pk'], item[0]))
    recovered = 0
    for kind, candidate in candidates[:limit]:
        model, _pending_status = _dispatch_model(kind)
        running_status = 'running' if kind == 'case' else 'RUNNING'
        try:
            with transaction.atomic():
                current = model.objects.select_for_update().filter(pk=candidate['pk']).first()
                if (
                    current is None
                    or current.status != running_status
                    or current.dispatch_claim != candidate['dispatch_claim']
                    or current.worker_nonce != candidate['worker_nonce']
                    or current.worker_lease_expires_at is None
                    or current.worker_lease_expires_at > query_time
                ):
                    continue

                fields = {
                    'status': 'aborted' if kind == 'case' else 'ABORTED',
                    'cancel_requested_at': query_time,
                    'finished_at': query_time,
                    'error_message': WORKBENCH_WORKER_LOST_MESSAGE,
                    'failure_stage': 'worker_lost',
                    'worker_heartbeat_at': None,
                    'worker_lease_expires_at': None,
                }
                if kind == 'case':
                    fields['execution_logs'] = WORKBENCH_WORKER_LOST_MESSAGE
                    fields['execution_time'] = max(
                        0.0,
                        (query_time - current.started_at).total_seconds() if current.started_at else 0.0,
                    )
                else:
                    fields['duration'] = max(
                        0.0,
                        (query_time - current.started_at).total_seconds() if current.started_at else 0.0,
                    )
                    data = current.result_data if isinstance(current.result_data, dict) else {}
                    summary = data.get('summary') if isinstance(data.get('summary'), dict) else {}
                    fields['result_data'] = {
                        **data,
                        'summary': {
                            **summary,
                            'message': WORKBENCH_WORKER_LOST_MESSAGE,
                            'interrupted': True,
                            'external_effects_unknown': True,
                        },
                    }

                updated = model.objects.filter(
                    pk=current.pk,
                    dispatch_claim=candidate['dispatch_claim'],
                    worker_nonce=candidate['worker_nonce'],
                    status=running_status,
                    worker_lease_expires_at__lte=query_time,
                ).update(**fields)
                if not updated:
                    continue
                if kind == 'suite':
                    TestCaseExecution.objects.filter(
                        parent_execution_id=current.pk,
                        worker_nonce=candidate['worker_nonce'],
                        status__in=['pending', 'running'],
                    ).update(
                        status='aborted',
                        cancel_requested_at=query_time,
                        finished_at=query_time,
                        error_message=WORKBENCH_WORKER_LOST_MESSAGE,
                        execution_logs=WORKBENCH_WORKER_LOST_MESSAGE,
                        failure_stage='worker_lost',
                        worker_heartbeat_at=None,
                        worker_lease_expires_at=None,
                    )
                    if current.test_suite_id:
                        from .models import TestSuite

                        TestSuite.objects.filter(pk=current.test_suite_id).update(
                            execution_status='failed',
                            updated_at=query_time,
                        )
                recovered += 1
        except Exception:  # noqa: BLE001 - each candidate is isolated for the next poll
            logger.exception(
                'UI workbench stale worker reconciliation failed kind=%s execution_id=%s',
                kind, candidate['pk'],
            )
    return recovered


def reconcile_pending_submissions(limit=20, now=None) -> int:
    """Re-publish a bounded number of unacknowledged, pending submissions."""
    limit = max(int(limit), 0)
    if not limit:
        return 0

    query_time = now or timezone.now()
    lease_available = (
        Q(dispatch_delivery_token__isnull=True)
        | Q(dispatch_delivery_expires_at__isnull=True)
        | Q(dispatch_delivery_expires_at__lte=query_time)
    )
    candidates = []
    for kind, (model, pending_status) in (
        ('case', _dispatch_model('case')),
        ('suite', _dispatch_model('suite')),
    ):
        rows = model.objects.filter(
            status=pending_status,
            dispatch_claim__isnull=False,
            cancel_requested_at__isnull=True,
            dispatch_enqueued_at__isnull=True,
        ).filter(lease_available).order_by('created_at', 'pk')[:limit]
        candidates.extend((kind, execution) for execution in rows)

    candidates.sort(key=lambda item: (item[1].created_at, item[1].pk, item[0]))
    enqueued = 0
    for kind, execution in candidates[:limit]:
        try:
            reservation_time = now or timezone.now()
            if dispatch_execution(kind, execution, now=reservation_time):
                enqueued += 1
        except Exception:  # noqa: BLE001 - stop on broker failure; next poll can retry after lease expiry
            logger.exception(
                'UI workbench dispatch reconciliation failed kind=%s execution_id=%s',
                kind, execution.pk,
            )
            break
    return enqueued
