"""Shared durable submission primitives for the UI workbench."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict

from django.db import IntegrityError, transaction
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


def dispatch_execution(kind, execution):
    from .tasks import execute_workbench_case, execute_workbench_suite

    task = execute_workbench_case if kind == 'case' else execute_workbench_suite
    task.apply_async(args=(execution.pk, str(execution.dispatch_claim)))
