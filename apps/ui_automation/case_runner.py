"""Claim-fenced worker adapter for the canonical case execution view."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone

from .execution_summary import INTERNAL_CASE_EXECUTION_ERROR
from .models import TestCaseExecution

logger = logging.getLogger(__name__)


class CaseExecutionCancelled(Exception):
    pass


def claim_case_execution(execution_id, claim_token):
    with transaction.atomic():
        execution = TestCaseExecution.objects.select_for_update().filter(pk=execution_id).first()
        if (
            execution is None
            or str(execution.dispatch_claim) != str(claim_token)
            or execution.status != 'pending'
            or execution.cancel_requested_at is not None
        ):
            return None
        execution.status = 'running'
        execution.started_at = timezone.now()
        execution.failure_stage = ''
        execution._ui_dispatch_claim = str(claim_token)
        execution.save(update_fields=['status', 'started_at', 'failure_stage'])
        return execution


def persist_case_result(execution_id, claim_token, *, status, fields=None, failure_stage=''):
    fields = dict(fields or {})
    with transaction.atomic():
        execution = TestCaseExecution.objects.select_for_update().filter(pk=execution_id).first()
        if (
            execution is None
            or str(execution.dispatch_claim) != str(claim_token)
            or execution.status != 'running'
            or execution.cancel_requested_at is not None
        ):
            return False
        execution.status = status
        execution.failure_stage = failure_stage
        execution._ui_dispatch_claim = str(claim_token)
        for key, value in fields.items():
            setattr(execution, key, value)
        if not getattr(execution, 'finished_at', None):
            execution.finished_at = timezone.now()
        execution.save()
        return True


def mark_case_dispatch_failed(execution_id, claim_token, message):
    return persist_case_result(
        execution_id,
        claim_token,
        status='error',
        fields={'error_message': message, 'execution_logs': message, 'finished_at': timezone.now()},
        failure_stage='dispatch',
    )


def _raise_if_case_cancelled(execution_id, claim_token):
    row = TestCaseExecution.objects.filter(pk=execution_id).values(
        'dispatch_claim', 'status', 'cancel_requested_at',
    ).first()
    if (
        row is None
        or str(row['dispatch_claim']) != str(claim_token)
        or row['status'] != 'running'
        or row['cancel_requested_at'] is not None
    ):
        raise CaseExecutionCancelled('用例执行已请求停止')


def execute_case_execution(execution_id, claim_token):
    execution = claim_case_execution(execution_id, claim_token)
    if execution is None:
        return False

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
        )
        return False

    from .views import TestCaseViewSet

    request = SimpleNamespace(
        data=dict(snapshot['config']),
        user=execution.created_by,
        _ui_async_worker=True,
        _ui_execution=execution,
        _ui_dispatch_claim=str(claim_token),
        _ui_step_snapshot=snapshot['steps'],
        _ui_test_case=execution.test_case,
        _ui_base_url=snapshot.get('base_url') or execution.project.base_url,
        _ui_cancel_check=lambda: _raise_if_case_cancelled(execution_id, claim_token),
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
            # A normal DRF rejection is not an exception. Finalize only while
            # this claim is still running; engine/cancellation outcomes win.
            persist_case_result(
                execution_id, claim_token, status='error',
                fields={'error_message': message, 'execution_logs': message, 'finished_at': timezone.now()},
                failure_stage=failure_stage,
            )
            return False
        stored_status = TestCaseExecution.objects.filter(pk=execution_id).values_list('status', flat=True).first()
        if stored_status in {'pending', 'running'}:
            persist_case_result(
                execution_id, claim_token, status='error',
                fields={'error_message': INTERNAL_CASE_EXECUTION_ERROR,
                        'execution_logs': INTERNAL_CASE_EXECUTION_ERROR, 'finished_at': timezone.now()},
                failure_stage='worker',
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
        )
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
        )
        return False
