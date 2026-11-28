"""Claim-fenced worker adapter for the canonical case execution view."""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from datetime import timedelta
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone

from .execution_summary import INTERNAL_CASE_EXECUTION_ERROR
from .models import TestCaseExecution
from .worker_lease import (
    ExecutionWorkerLeaseHeartbeat,
    WorkbenchWorkerLeaseLost,
    recover_claim_after_error,
    worker_lease_seconds,
)

logger = logging.getLogger(__name__)
_current_worker_heartbeat = ContextVar('ui_case_worker_heartbeat', default=None)


class CaseExecutionCancelled(Exception):
    pass


def _case_owner_matches(execution, claim_token, worker_nonce, *, now=None):
    now = now or timezone.now()
    return bool(
        execution is not None
        and str(execution.dispatch_claim) == str(claim_token)
        and execution.status == 'running'
        and execution.cancel_requested_at is None
        and str(execution.worker_nonce) == str(worker_nonce)
        and execution.worker_lease_expires_at is not None
        and execution.worker_lease_expires_at > now
    )


def claim_case_execution(execution_id, claim_token, worker_nonce=None):
    nonce = uuid.UUID(str(worker_nonce)) if worker_nonce is not None else uuid.uuid4()
    now = timezone.now()
    try:
        with transaction.atomic():
            claimed = TestCaseExecution.objects.filter(
                pk=execution_id,
                dispatch_claim=claim_token,
                status='pending',
                cancel_requested_at__isnull=True,
            ).update(
                status='running',
                started_at=now,
                failure_stage='',
                worker_nonce=nonce,
                worker_heartbeat_at=now,
                worker_lease_expires_at=now + timedelta(seconds=worker_lease_seconds()),
            )
            if not claimed:
                return None
            execution = TestCaseExecution.objects.filter(pk=execution_id).first()
    except Exception as error:
        execution = recover_claim_after_error(
            TestCaseExecution,
            execution_id,
            claim_token,
            nonce,
            'pending',
            _case_owner_matches,
            error,
        )
    if execution is not None:
        execution._ui_dispatch_claim = str(claim_token)
        execution._ui_worker_nonce = str(nonce)
    return execution


def persist_case_result(
    execution_id, claim_token, *, status, fields=None, failure_stage='', worker_nonce=None,
):
    fields = dict(fields or {})
    heartbeat = _current_worker_heartbeat.get()
    expected_nonce = worker_nonce or (heartbeat.worker_nonce if heartbeat is not None else None)
    if heartbeat is not None:
        try:
            heartbeat.raise_if_lost()
        except WorkbenchWorkerLeaseLost:
            return False
    with transaction.atomic():
        execution = TestCaseExecution.objects.select_for_update().filter(pk=execution_id).first()
        now = timezone.now()
        if (
            execution is None
            or str(execution.dispatch_claim) != str(claim_token)
            or execution.status != 'running'
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
        execution.failure_stage = failure_stage
        execution._ui_dispatch_claim = str(claim_token)
        if execution.worker_nonce is not None:
            execution._ui_worker_nonce = str(expected_nonce)
            execution.worker_heartbeat_at = None
            execution.worker_lease_expires_at = None
        for key, value in fields.items():
            setattr(execution, key, value)
        if not getattr(execution, 'finished_at', None):
            execution.finished_at = now
        execution.save()
        stored = TestCaseExecution.objects.filter(pk=execution_id).values(
            'status', 'worker_nonce',
        ).first()
        if (
            stored is None
            or stored['status'] != status
            or str(stored['worker_nonce'] or '') != str(execution.worker_nonce or '')
        ):
            return False
        return True


def mark_case_dispatch_failed(execution_id, claim_token, message):
    return persist_case_result(
        execution_id,
        claim_token,
        status='error',
        fields={'error_message': message, 'execution_logs': message, 'finished_at': timezone.now()},
        failure_stage='dispatch',
    )


def _raise_if_case_cancelled(execution_id, claim_token, worker_nonce=None):
    row = TestCaseExecution.objects.filter(pk=execution_id).values(
        'dispatch_claim', 'status', 'cancel_requested_at', 'worker_nonce', 'worker_lease_expires_at',
    ).first()
    if (
        row is None
        or str(row['dispatch_claim']) != str(claim_token)
        or row['status'] != 'running'
        or row['cancel_requested_at'] is not None
    ):
        raise CaseExecutionCancelled('用例执行已请求停止')
    if worker_nonce is not None and (
        str(row['worker_nonce']) != str(worker_nonce)
        or row['worker_lease_expires_at'] is None
        or row['worker_lease_expires_at'] <= timezone.now()
    ):
        raise WorkbenchWorkerLeaseLost('用例执行worker租约已失效')


def execute_case_execution(execution_id, claim_token, worker_nonce=None):
    nonce = uuid.UUID(str(worker_nonce)) if worker_nonce is not None else uuid.uuid4()
    execution = claim_case_execution(execution_id, claim_token, nonce)
    if execution is None:
        return False

    heartbeat = ExecutionWorkerLeaseHeartbeat(
        TestCaseExecution, execution_id, claim_token, nonce,
    ).start()
    context_token = _current_worker_heartbeat.set(heartbeat)
    try:
        snapshot = execution.request_snapshot or {}
        if not isinstance(snapshot.get('config'), dict) or not isinstance(snapshot.get('steps'), list):
            persist_case_result(
                execution.pk,
                claim_token,
                status='error',
                fields={
                    'error_message': '执行快照不完整，无法安全启动用例。',
                    'execution_logs': '执行快照不完整，无法安全启动用例。',
                    'finished_at': timezone.now(),
                },
                failure_stage='preflight',
                worker_nonce=nonce,
            )
            return False

        from .views import TestCaseViewSet

        def check_worker_ownership():
            heartbeat.raise_if_lost()
            _raise_if_case_cancelled(execution_id, claim_token, nonce)

        request = SimpleNamespace(
            data=dict(snapshot['config']),
            user=execution.created_by,
            _ui_async_worker=True,
            _ui_execution=execution,
            _ui_dispatch_claim=str(claim_token),
            _ui_worker_nonce=str(nonce),
            _ui_step_snapshot=snapshot['steps'],
            _ui_test_case=execution.test_case,
            _ui_base_url=snapshot.get('base_url') or execution.project.base_url,
            _ui_cancel_check=check_worker_ownership,
        )
        view = TestCaseViewSet()
        view.request = request
        view.action = 'run'
        view.kwargs = {'pk': str(execution.test_case_id)}
        try:
            response = view.run(request, pk=str(execution.test_case_id))
            if getattr(response, 'status_code', 200) >= 400:
                data = getattr(response, 'data', None)
                data = data if isinstance(data, dict) else {}
                message = next((data[key].strip() for key in ('message', 'error', 'detail')
                                if isinstance(data.get(key), str) and data[key].strip()),
                               INTERNAL_CASE_EXECUTION_ERROR)
                failure_stage = data.get('failure_stage')
                if failure_stage not in {'preflight', 'permission', 'dispatch'}:
                    failure_stage = 'preflight'
                persist_case_result(
                    execution_id, claim_token, status='error',
                    fields={'error_message': message, 'execution_logs': message, 'finished_at': timezone.now()},
                    failure_stage=failure_stage,
                    worker_nonce=nonce,
                )
                return False
            stored_status = TestCaseExecution.objects.filter(pk=execution_id).values_list('status', flat=True).first()
            if stored_status in {'pending', 'running'}:
                persist_case_result(
                    execution_id, claim_token, status='error',
                    fields={'error_message': INTERNAL_CASE_EXECUTION_ERROR,
                            'execution_logs': INTERNAL_CASE_EXECUTION_ERROR, 'finished_at': timezone.now()},
                    failure_stage='worker',
                    worker_nonce=nonce,
                )
                return False
            return stored_status not in {None, 'aborted'}
        except CaseExecutionCancelled:
            persist_case_result(
                execution_id,
                claim_token,
                status='aborted',
                fields={
                    'error_message': '执行已由用户停止。',
                    'execution_logs': '执行已由用户停止。',
                    'finished_at': timezone.now(),
                },
                worker_nonce=nonce,
            )
            return False
        except WorkbenchWorkerLeaseLost:
            logger.info('UI workbench case worker lease lost execution_id=%s', execution_id)
            return False
        except Exception:
            logger.exception('UI workbench case worker failed execution_id=%s', execution_id)
            persist_case_result(
                execution_id,
                claim_token,
                status='error',
                fields={
                    'error_message': INTERNAL_CASE_EXECUTION_ERROR,
                    'execution_logs': INTERNAL_CASE_EXECUTION_ERROR,
                    'finished_at': timezone.now(),
                },
                failure_stage='worker',
                worker_nonce=nonce,
            )
            return False
    finally:
        _current_worker_heartbeat.reset(context_token)
        heartbeat.stop()
