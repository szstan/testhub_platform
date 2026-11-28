"""Claim-fenced worker for durable UI suite submissions."""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.scheduler import ScheduleClaimLost

from .execution_summary import INTERNAL_SUITE_EXECUTION_ERROR
from .models import TestExecution, TestSuite
from .worker_lease import (
    ExecutionWorkerLeaseHeartbeat,
    WorkbenchWorkerLeaseLost,
    recover_claim_after_error,
    worker_lease_seconds,
)

logger = logging.getLogger(__name__)
_current_suite_worker_nonce = ContextVar('ui_suite_worker_nonce', default=None)


def _suite_owner_matches(execution, claim_token, worker_nonce, *, now=None):
    now = now or timezone.now()
    return bool(
        execution is not None
        and str(execution.dispatch_claim) == str(claim_token)
        and execution.status == 'RUNNING'
        and execution.cancel_requested_at is None
        and str(execution.worker_nonce) == str(worker_nonce)
        and execution.worker_lease_expires_at is not None
        and execution.worker_lease_expires_at > now
    )


def _claim_execution(execution_id, claim_token, worker_nonce=None):
    nonce = uuid.UUID(str(worker_nonce)) if worker_nonce is not None else uuid.uuid4()
    now = timezone.now()
    try:
        with transaction.atomic():
            claimed = TestExecution.objects.filter(
                pk=execution_id,
                dispatch_claim=claim_token,
                status='PENDING',
                cancel_requested_at__isnull=True,
            ).update(
                status='RUNNING',
                started_at=now,
                failure_stage='',
                worker_nonce=nonce,
                worker_heartbeat_at=now,
                worker_lease_expires_at=now + timedelta(seconds=worker_lease_seconds()),
            )
            if not claimed:
                return None
            execution = TestExecution.objects.filter(pk=execution_id).first()
            if execution is None:
                raise RuntimeError('suite execution disappeared while claiming')
            if execution.test_suite_id:
                TestSuite.objects.filter(pk=execution.test_suite_id).update(
                    execution_status='running',
                    updated_at=now,
                )
    except Exception as error:
        execution = recover_claim_after_error(
            TestExecution,
            execution_id,
            claim_token,
            nonce,
            'PENDING',
            _suite_owner_matches,
            error,
        )
    if execution is None:
        return None
    execution._ui_dispatch_claim = str(claim_token)
    execution._ui_worker_nonce = str(nonce)
    return execution


def _finish_suite(execution_id, claim_token, *, status, message='', failure_stage='', worker_nonce=None):
    expected_nonce = worker_nonce or _current_suite_worker_nonce.get()
    with transaction.atomic():
        execution = TestExecution.objects.select_for_update().filter(pk=execution_id).first()
        now = timezone.now()
        if (
            execution is None
            or str(execution.dispatch_claim) != str(claim_token)
            or execution.status != 'RUNNING'
            or execution.cancel_requested_at is not None
            or (
                execution.worker_nonce is not None
                and (
                    str(execution.worker_nonce) != str(expected_nonce or '')
                    or execution.worker_lease_expires_at is None
                    or execution.worker_lease_expires_at <= now
                )
            )
            or (execution.worker_nonce is None and expected_nonce is not None)
        ):
            return False
        execution.status = status
        execution.finished_at = now
        execution.error_message = message
        execution.failure_stage = failure_stage
        execution._ui_dispatch_claim = str(claim_token)
        if execution.worker_nonce is not None:
            execution._ui_worker_nonce = str(expected_nonce)
            execution.worker_heartbeat_at = None
            execution.worker_lease_expires_at = None
        if message and not execution.result_data:
            execution.result_data = {'test_cases': [], 'summary': {'message': message}}
        execution.save()
        stored_status = TestExecution.objects.filter(pk=execution_id).values_list('status', flat=True).first()
        if stored_status != status:
            return False
        if execution.test_suite_id:
            TestSuite.objects.filter(pk=execution.test_suite_id).update(execution_status='failed')
        return True


def _cancel_check(execution_id, claim_token, worker_nonce=None):
    expected_nonce = worker_nonce or _current_suite_worker_nonce.get()
    row = TestExecution.objects.filter(pk=execution_id).values(
        'dispatch_claim', 'status', 'cancel_requested_at', 'worker_nonce', 'worker_lease_expires_at',
    ).first()
    if (
        row is None
        or str(row['dispatch_claim']) != str(claim_token)
        or row['status'] != 'RUNNING'
        or row['cancel_requested_at'] is not None
    ):
        raise ScheduleClaimLost('suite execution has been cancelled or lost its claim')
    if row['worker_nonce'] is not None and (
        str(row['worker_nonce']) != str(expected_nonce or '')
        or row['worker_lease_expires_at'] is None
        or row['worker_lease_expires_at'] <= timezone.now()
    ):
        raise WorkbenchWorkerLeaseLost('suite execution worker lease was lost')


def _check_environment(engine, browser):
    if engine == 'playwright':
        from .playwright_engine import PlaywrightTestEngine

        return PlaywrightTestEngine.check_execution_environment_sync(browser)
    from .selenium_engine import SeleniumTestEngine

    return SeleniumTestEngine.check_execution_environment(browser)


def execute_suite_execution(execution_id, claim_token, worker_nonce=None):
    nonce = uuid.UUID(str(worker_nonce)) if worker_nonce is not None else uuid.uuid4()
    execution = _claim_execution(execution_id, claim_token, nonce)
    if execution is None:
        return False

    heartbeat = ExecutionWorkerLeaseHeartbeat(
        TestExecution, execution_id, claim_token, nonce,
    ).start()
    context_token = _current_suite_worker_nonce.set(str(nonce))
    try:
        snapshot = execution.request_snapshot or {}
        if not isinstance(snapshot.get('config'), dict) or not isinstance(snapshot.get('cases'), list):
            _finish_suite(
                execution_id,
                claim_token,
                status='FAILED',
                message='执行快照不完整，无法安全启动套件。',
                failure_stage='preflight',
                worker_nonce=nonce,
            )
            return False

        options = snapshot['config']
        ready, message = _check_environment(options['engine'], options['browser'])
        if not ready:
            _finish_suite(
                execution_id,
                claim_token,
                status='FAILED',
                message=str(message or '浏览器执行环境不可用。')[:4000],
                failure_stage='preflight',
                worker_nonce=nonce,
            )
            return False

        suite = TestSuite.objects.select_related('project').get(pk=execution.test_suite_id)
        from .test_executor import TestExecutor

        def check_worker_ownership():
            heartbeat.raise_if_lost()
            _cancel_check(execution_id, claim_token, nonce)

        executor = TestExecutor(
            test_suite=suite,
            engine=options['engine'],
            browser=options['browser'],
            headless=options['headless'],
            executed_by=execution.executed_by,
            execution=execution,
            cancel_check=check_worker_ownership,
            case_snapshots=snapshot['cases'],
            base_url=snapshot.get('base_url'),
        )
        executor.run()
        return True
    except WorkbenchWorkerLeaseLost:
        logger.info('UI suite worker lease lost execution_id=%s', execution_id)
        return False
    except ScheduleClaimLost:
        logger.info('UI suite worker stopped after cancellation execution_id=%s', execution_id)
        return False
    except Exception:
        logger.exception('UI workbench suite worker failed execution_id=%s', execution_id)
        try:
            heartbeat.raise_if_lost()
        except WorkbenchWorkerLeaseLost:
            return False
        _finish_suite(
            execution_id,
            claim_token,
            status='FAILED',
            message=INTERNAL_SUITE_EXECUTION_ERROR,
            failure_stage='worker',
            worker_nonce=nonce,
        )
        return False
    finally:
        _current_suite_worker_nonce.reset(context_token)
        heartbeat.stop()
