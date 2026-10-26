"""Durable Celery worker for UI scheduled tasks."""

from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.core.scheduler import ScheduleClaimLost, clear_claim, load_claimed_task

logger = logging.getLogger(__name__)


def _load_task(task_id, scheduler_lease_token=''):
    from .models import UiScheduledTask

    if scheduler_lease_token:
        return load_claimed_task(
            UiScheduledTask,
            task_id,
            scheduler_lease_token,
            require_active=True,
        )
    return UiScheduledTask.objects.select_related(
        'project', 'test_suite', 'created_by'
    ).get(pk=task_id)


def _finish_task(task_id, *, scheduler_lease_token, success, result, error_message=''):
    from .models import UiScheduledTask

    with transaction.atomic():
        task = UiScheduledTask.objects.select_for_update().get(pk=task_id)
        if scheduler_lease_token:
            task = load_claimed_task(
                UiScheduledTask,
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


def _run_suite(task):
    from .test_executor import TestExecutor

    suite = task.test_suite
    if suite is None:
        raise ValueError('scheduled task has no test suite')
    test_case_count = suite.suite_test_cases.count()
    if test_case_count == 0:
        raise ValueError('scheduled task test suite has no test cases')
    suite.execution_status = 'running'
    suite.save(update_fields=['execution_status', 'updated_at'])
    executor = TestExecutor(
        test_suite=suite,
        engine=task.engine,
        browser=task.browser,
        headless=task.headless,
        executed_by=task.created_by,
    )
    executor.run()
    suite.refresh_from_db()
    success = suite.execution_status == 'passed'
    return success, {
        'status': 'success' if success else 'failed',
        'message': '测试套件执行成功' if success else '测试套件执行失败',
        'test_case_count': test_case_count,
        'passed_count': suite.passed_count,
        'failed_count': suite.failed_count,
    }


def _run_cases(task):
    from .models import TestCase, TestSuite
    from .test_executor import TestExecutor

    test_cases = list(TestCase.objects.filter(id__in=task.test_cases).order_by('id'))
    if not test_cases:
        raise ValueError('scheduled task has no test cases')
    results = []
    passed = 0
    failed = 0
    for test_case in test_cases:
        temp_suite = TestSuite.objects.create(
            project=task.project,
            name=f'[scheduled:{task.pk}] {test_case.name}',
        )
        try:
            temp_suite.test_cases.add(test_case)
            temp_suite.execution_status = 'running'
            temp_suite.save(update_fields=['execution_status', 'updated_at'])
            TestExecutor(
                test_suite=temp_suite,
                engine=task.engine,
                browser=task.browser,
                headless=task.headless,
                executed_by=task.created_by,
            ).run()
            temp_suite.refresh_from_db()
            ok = temp_suite.execution_status == 'passed'
            if ok:
                passed += 1
            else:
                failed += 1
            results.append({'case_id': test_case.pk, 'case_name': test_case.name, 'status': 'success' if ok else 'failed'})
        except Exception as exc:  # noqa: BLE001 - retain per-case failure and continue
            failed += 1
            results.append({'case_id': test_case.pk, 'case_name': test_case.name, 'status': 'failed', 'error': str(exc)[:1000]})
        finally:
            temp_suite.delete()
    success = failed == 0
    return success, {
        'status': 'success' if success else 'partial_success',
        'test_case_count': len(test_cases),
        'success_count': passed,
        'failed_count': failed,
        'results': results,
    }


@shared_task(bind=True, ignore_result=False)
def execute_ui_scheduled_task(self, task_id, scheduler_lease_token=''):
    """Execute one UI scheduled task in a Celery worker."""

    task = None
    try:
        task = _load_task(task_id, scheduler_lease_token)
        if task.task_type == 'TEST_SUITE':
            success, result = _run_suite(task)
        elif task.task_type == 'TEST_CASE':
            success, result = _run_cases(task)
        else:
            raise ValueError(f'unknown scheduled task type: {task.task_type}')
        _finish_task(
            task_id,
            scheduler_lease_token=scheduler_lease_token,
            success=success,
            result=result,
            error_message='' if success else result.get('message', 'scheduled task failed'),
        )
        from .views import UiScheduledTaskViewSet
        UiScheduledTaskViewSet()._send_task_notification(task, success=success)
        return {'task_id': task_id, 'success': success, 'result': result}
    except ScheduleClaimLost:
        logger.warning('UI scheduled task claim lost: task_id=%s', task_id)
        return {'task_id': task_id, 'success': False, 'claim_lost': True}
    except Exception as exc:  # noqa: BLE001 - update durable schedule state before returning
        logger.exception('UI scheduled task failed: task_id=%s', task_id)
        try:
            _finish_task(
                task_id,
                scheduler_lease_token=scheduler_lease_token,
                success=False,
                result={'status': 'failed', 'message': str(exc)[:4000]},
                error_message=str(exc),
            )
        except ScheduleClaimLost:
            logger.warning('UI scheduled task claim lost while recording failure: task_id=%s', task_id)
        if task is not None:
            try:
                from .views import UiScheduledTaskViewSet
                UiScheduledTaskViewSet()._send_task_notification(task, success=False)
            except Exception:  # noqa: BLE001 - notification cannot hide execution failure
                logger.exception('UI scheduled task failure notification failed: task_id=%s', task_id)
        return {'task_id': task_id, 'success': False}


__all__ = ['execute_ui_scheduled_task']
