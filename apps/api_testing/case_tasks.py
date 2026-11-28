"""Celery task for durable asynchronous API case executions."""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.quality_intelligence.services.redaction import redact_payload


def reconcile_stale_api_case_executions(limit=100, *, stale_after_seconds=None, queryset=None):
    from .models import ApiCaseExecution

    stale_after_seconds = max(int(
        stale_after_seconds
        if stale_after_seconds is not None
        else getattr(settings, 'API_CASE_EXECUTION_STALE_AFTER_SECONDS', 300)
    ), 30)
    now = timezone.now()
    cutoff = now - timedelta(seconds=stale_after_seconds)
    stale = (
        Q(status='PENDING', created_at__lte=cutoff)
        | (
            Q(status='RUNNING')
            & (
                Q(heartbeat_at__lte=cutoff)
                | Q(heartbeat_at__isnull=True, start_time__lte=cutoff)
                | Q(heartbeat_at__isnull=True, start_time__isnull=True, created_at__lte=cutoff)
            )
        )
    )
    candidates = queryset if queryset is not None else ApiCaseExecution.objects.all()
    execution_ids = candidates.filter(stale).order_by('pk').values_list('pk', flat=True)[
        :max(1, min(int(limit), 1000))
    ]
    recovered = 0
    for execution_id in execution_ids:
        with transaction.atomic():
            execution = ApiCaseExecution.objects.select_for_update().filter(pk=execution_id).first()
            if execution is None:
                continue
            last_activity = execution.heartbeat_at or execution.start_time or execution.created_at
            is_stale = (
                execution.status == 'PENDING' and execution.created_at <= cutoff
            ) or (
                execution.status == 'RUNNING' and last_activity <= cutoff
            )
            if not is_stale:
                continue
            execution.status = 'FAILED'
            execution.end_time = now
            execution.error_message = '执行worker心跳超时，任务已终止；如需重试请创建新的执行记录。'
            execution.save(update_fields=['status', 'end_time', 'duration', 'error_message'])
            recovered += 1
    return recovered


@shared_task(bind=True, ignore_result=False)
def execute_api_case_execution(self, execution_id, variable_overrides=None, require_snapshot=False):
    from .case_execution_inputs import freeze_api_case_execution
    from .case_runner import run_api_test_case
    from .models import ApiCaseExecution

    execution = None
    frozen_inputs = None
    try:
        with transaction.atomic():
            execution = ApiCaseExecution.objects.select_for_update().select_related(
                'executed_by',
            ).filter(pk=execution_id).first()
            if execution is None:
                return {'execution_id': execution_id, 'success': False, 'error': '执行记录不存在'}
            if execution.status != 'PENDING':
                return {
                    'execution_id': execution.pk,
                    'success': execution.status == 'COMPLETED' and execution.failed_steps == 0,
                    'status': execution.status,
                    'duplicate': True,
                }
            frozen_inputs = freeze_api_case_execution(
                execution,
                variable_overrides,
                required=require_snapshot,
            )
            execution.status = 'RUNNING'
            execution.start_time = timezone.now()
            execution.heartbeat_at = execution.start_time
            execution.save(update_fields=['status', 'start_time', 'heartbeat_at'])

        def heartbeat():
            updated = ApiCaseExecution.objects.filter(
                pk=execution_id,
                status='RUNNING',
            ).update(heartbeat_at=timezone.now())
            if not updated:
                raise RuntimeError('执行已被回收或不再运行')

        if frozen_inputs is not None:
            result = run_api_test_case(
                frozen_inputs.test_case,
                frozen_inputs.environment,
                execution.executed_by,
                variable_overrides=variable_overrides,
                heartbeat=heartbeat,
                frozen_inputs=frozen_inputs,
            )
        elif execution.test_case_id is None:
            result = {'success': False, 'error': '接口测试用例已删除'}
        else:
            result = run_api_test_case(
                execution.test_case,
                execution.environment,
                execution.executed_by,
                variable_overrides=variable_overrides,
                heartbeat=heartbeat,
            )

        with transaction.atomic():
            current = ApiCaseExecution.objects.select_for_update().get(pk=execution_id)
            if current.status != 'RUNNING':
                return {
                    'execution_id': execution_id,
                    'success': False,
                    'status': current.status,
                    'superseded': True,
                }
            current.end_time = timezone.now()
            current.heartbeat_at = current.end_time
            current.total_steps = result.get('total_count', 0)
            current.passed_steps = result.get('passed_count', 0)
            current.failed_steps = result.get('failed_count', 0)
            current.skipped_steps = result.get('skipped_count', 0)
            current.results = redact_payload(result.get('results', []))
            current.error_message = redact_payload(result.get('error', ''))
            current.status = 'COMPLETED' if result.get('success') else 'FAILED'
            current.save(update_fields=[
                'end_time', 'duration', 'total_steps', 'passed_steps', 'failed_steps',
                'skipped_steps', 'results', 'error_message', 'status', 'heartbeat_at',
            ])
        return {
            'execution_id': execution_id,
            'success': result.get('success', False),
            'status': current.status,
            'passed_steps': current.passed_steps,
            'failed_steps': current.failed_steps,
        }
    except Exception as exc:  # noqa: BLE001 - durable execution state is authoritative
        if execution is not None:
            with transaction.atomic():
                current = ApiCaseExecution.objects.select_for_update().filter(pk=execution_id).first()
                if current and current.status in {'PENDING', 'RUNNING'}:
                    current.status = 'FAILED'
                    current.end_time = timezone.now()
                    current.error_message = redact_payload(str(exc))
                    current.save(update_fields=['status', 'end_time', 'duration', 'error_message'])
        return {
            'execution_id': execution_id,
            'success': False,
            'status': 'FAILED',
            'error': redact_payload(str(exc)),
        }
