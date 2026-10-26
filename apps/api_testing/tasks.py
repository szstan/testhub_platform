"""Durable Celery worker for API scheduled tasks."""

from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.core.scheduler import ScheduleClaimLost, clear_claim, load_claimed_task

logger = logging.getLogger(__name__)


def _load_task(task_id, scheduler_lease_token=''):
    from .models import ScheduledTask

    if scheduler_lease_token:
        return load_claimed_task(
            ScheduledTask,
            task_id,
            scheduler_lease_token,
            require_active=True,
        )
    return ScheduledTask.objects.select_related(
        'test_suite', 'api_request', 'environment', 'created_by'
    ).get(pk=task_id)


def _finish_task(task_id, *, scheduler_lease_token, success, result=None, error_message=''):
    from .models import ScheduledTask

    with transaction.atomic():
        task = ScheduledTask.objects.select_for_update().get(pk=task_id)
        if scheduler_lease_token:
            # Re-check the fencing token while holding the update lock. A
            # late worker must never overwrite a reclaimed schedule.
            task = load_claimed_task(
                ScheduledTask,
                task_id,
                scheduler_lease_token,
                require_active=True,
            )
        elif task.scheduler_lease_token:
            raise ScheduleClaimLost('scheduled task is owned by another worker')

        task.total_runs += 1
        if success:
            task.successful_runs += 1
        else:
            task.failed_runs += 1
        task.last_run_time = timezone.now()
        task.last_result = result or {}
        task.error_message = str(error_message or '')[:4000]
        task.next_run_time = task.calculate_next_run()
        fields = [
            'total_runs', 'successful_runs', 'failed_runs', 'last_run_time',
            'last_result', 'error_message', 'next_run_time', 'updated_at',
        ]
        if scheduler_lease_token:
            clear_claim(task, token=scheduler_lease_token)
            fields.extend(['scheduler_lease_token', 'scheduler_lease_expires_at'])
        task.save(update_fields=fields)
        return task


@shared_task(bind=True, ignore_result=False)
def execute_scheduled_task(self, task_id, execution_log_id, scheduler_lease_token=''):
    """Execute one API scheduled task outside the web/scheduler process."""

    from .models import TaskExecutionLog
    from .utils import execute_api_request, execute_test_suite

    log = TaskExecutionLog.objects.select_related('task').get(pk=execution_log_id)
    task = None
    try:
        task = _load_task(task_id, scheduler_lease_token)
        log.status = 'RUNNING'
        log.start_time = timezone.now()
        log.save(update_fields=['status', 'start_time'])

        if task.task_type == 'TEST_SUITE':
            if not task.test_suite:
                raise ValueError('scheduled task has no test suite')
            result = execute_test_suite(task.test_suite, task.environment, task.created_by)
        elif task.task_type == 'API_REQUEST':
            if not task.api_request:
                raise ValueError('scheduled task has no API request')
            result = execute_api_request(task.api_request, task.environment, task.created_by)
        else:
            raise ValueError(f'unknown scheduled task type: {task.task_type}')

        success = bool(result and result.get('success', False))
        log.status = 'COMPLETED' if success else 'FAILED'
        log.end_time = timezone.now()
        log.result = result or {}
        if not success:
            log.error_message = str((result or {}).get('error') or 'scheduled task returned failure')
        log.save(update_fields=['status', 'end_time', 'result', 'error_message'])
        _finish_task(
            task_id,
            scheduler_lease_token=scheduler_lease_token,
            success=success,
            result=result,
            error_message=log.error_message,
        )
        from .views import ScheduledTaskViewSet
        ScheduledTaskViewSet()._send_notification(task, log, success=success)
        return {'task_id': task_id, 'execution_log_id': log.pk, 'success': success}
    except ScheduleClaimLost:
        logger.warning('API scheduled task claim lost: task_id=%s', task_id)
        if log.status == 'PENDING':
            log.status = 'CANCELLED'
            log.end_time = timezone.now()
            log.error_message = 'scheduler lease lost before execution'
            log.save(update_fields=['status', 'end_time', 'error_message'])
        return {'task_id': task_id, 'execution_log_id': log.pk, 'success': False, 'claim_lost': True}
    except Exception as exc:  # noqa: BLE001 - durable log and task state are authoritative
        logger.exception('API scheduled task failed: task_id=%s', task_id)
        log.status = 'FAILED'
        log.end_time = timezone.now()
        log.error_message = str(exc)[:4000]
        log.save(update_fields=['status', 'end_time', 'error_message'])
        try:
            _finish_task(
                task_id,
                scheduler_lease_token=scheduler_lease_token,
                success=False,
                result={'success': False, 'error': str(exc)[:4000]},
                error_message=str(exc),
            )
        except ScheduleClaimLost:
            logger.warning('API scheduled task claim lost while recording failure: task_id=%s', task_id)
        if task is not None:
            try:
                from .views import ScheduledTaskViewSet
                ScheduledTaskViewSet()._send_notification(task, log, success=False)
            except Exception:  # noqa: BLE001 - notification cannot hide execution failure
                logger.exception('API scheduled task failure notification failed: task_id=%s', task_id)
        return {'task_id': task_id, 'execution_log_id': log.pk, 'success': False}


__all__ = ['execute_scheduled_task']
