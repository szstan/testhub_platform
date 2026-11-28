"""Claim-fenced worker for durable UI suite submissions."""

from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.utils import timezone

from apps.core.scheduler import ScheduleClaimLost

from .execution_summary import INTERNAL_SUITE_EXECUTION_ERROR
from .models import TestExecution, TestSuite

logger = logging.getLogger(__name__)


def _claim_execution(execution_id, claim_token):
    with transaction.atomic():
        execution = TestExecution.objects.select_for_update().filter(pk=execution_id).first()
        if (
            execution is None
            or str(execution.dispatch_claim) != str(claim_token)
            or execution.status != 'PENDING'
            or execution.cancel_requested_at is not None
        ):
            return None
        execution.status = 'RUNNING'
        execution.started_at = timezone.now()
        execution.failure_stage = ''
        execution._ui_dispatch_claim = str(claim_token)
        execution.save(update_fields=['status', 'started_at', 'failure_stage'])
        if execution.test_suite_id:
            TestSuite.objects.filter(pk=execution.test_suite_id).update(
                execution_status='running',
                updated_at=timezone.now(),
            )
        return execution


def _finish_suite(execution_id, claim_token, *, status, message='', failure_stage=''):
    with transaction.atomic():
        execution = TestExecution.objects.select_for_update().filter(pk=execution_id).first()
        if (
            execution is None
            or str(execution.dispatch_claim) != str(claim_token)
            or execution.status != 'RUNNING'
            or execution.cancel_requested_at is not None
        ):
            return False
        execution.status = status
        execution.finished_at = timezone.now()
        execution.error_message = message
        execution.failure_stage = failure_stage
        execution._ui_dispatch_claim = str(claim_token)
        if message and not execution.result_data:
            execution.result_data = {'test_cases': [], 'summary': {'message': message}}
        execution.save()
        if execution.test_suite_id:
            TestSuite.objects.filter(pk=execution.test_suite_id).update(execution_status='failed')
        return True


def _cancel_check(execution_id, claim_token):
    row = TestExecution.objects.filter(pk=execution_id).values(
        'dispatch_claim', 'status', 'cancel_requested_at',
    ).first()
    if (
        row is None
        or str(row['dispatch_claim']) != str(claim_token)
        or row['status'] != 'RUNNING'
        or row['cancel_requested_at'] is not None
    ):
        raise ScheduleClaimLost('suite execution has been cancelled or lost its claim')


def _check_environment(engine, browser):
    if engine == 'playwright':
        from .playwright_engine import PlaywrightTestEngine

        return PlaywrightTestEngine.check_execution_environment_sync(browser)
    from .selenium_engine import SeleniumTestEngine

    return SeleniumTestEngine.check_execution_environment(browser)


def execute_suite_execution(execution_id, claim_token):
    execution = _claim_execution(execution_id, claim_token)
    if execution is None:
        return False

    snapshot = execution.request_snapshot or {}
    if not isinstance(snapshot.get('config'), dict) or not isinstance(snapshot.get('cases'), list):
        _finish_suite(
            execution_id,
            claim_token,
            status='FAILED',
            message='执行快照不完整，无法安全启动套件。',
            failure_stage='preflight',
        )
        return False

    options = snapshot['config']
    try:
        ready, message = _check_environment(options['engine'], options['browser'])
        if not ready:
            _finish_suite(
                execution_id,
                claim_token,
                status='FAILED',
                message=str(message or '浏览器执行环境不可用。')[:4000],
                failure_stage='preflight',
            )
            return False

        suite = TestSuite.objects.select_related('project').get(pk=execution.test_suite_id)
        from .test_executor import TestExecutor

        executor = TestExecutor(
            test_suite=suite,
            engine=options['engine'],
            browser=options['browser'],
            headless=options['headless'],
            executed_by=execution.executed_by,
            execution=execution,
            cancel_check=lambda: _cancel_check(execution_id, claim_token),
            case_snapshots=snapshot['cases'],
            base_url=snapshot.get('base_url'),
        )
        executor.run()
    except ScheduleClaimLost:
        logger.info('UI suite worker stopped after cancellation execution_id=%s', execution_id)
        return False
    except Exception:
        logger.exception('UI workbench suite worker failed execution_id=%s', execution_id)
        _finish_suite(
            execution_id,
            claim_token,
            status='FAILED',
            message=INTERNAL_SUITE_EXECUTION_ERROR,
            failure_stage='worker',
        )
        return False
    return True
