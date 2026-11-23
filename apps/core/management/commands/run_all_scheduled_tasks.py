"""Run durable API/UI/APP/performance dispatches and generation recovery.

The command is intentionally a poller only. Every due row is claimed with a
DB-backed lease before any external/Celery work is started; no process-local
thread is used as the delivery mechanism.
"""

from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.core.scheduler import ScheduleClaimLost, clear_claim, claim_due_task, load_claimed_task
from apps.executions.retry_outbox import consume_retry_outbox
from apps.perf_testing.services.executor import reconcile_unified_completions

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = '运行所有模块的定时任务调度器（API测试 + UI自动化 + APP自动化 + 性能测试 + 用例生成恢复）'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=int, default=60, help='检查间隔（秒），默认60秒')
        parser.add_argument('--once', action='store_true', help='只执行一次检查，不循环')
        parser.add_argument('--lease-seconds', type=int, default=300, help='单次调度租约秒数，默认300秒')

    def handle(self, *args, **options):
        interval = max(int(options['interval']), 1)
        self.lease_seconds = max(int(options['lease_seconds']), 1)
        run_once = options['once']
        self.stdout.write(self.style.SUCCESS('=' * 60))
        self.stdout.write(self.style.SUCCESS('启动统一定时任务调度器'))
        self.stdout.write(self.style.SUCCESS(f'检查间隔: {interval}秒'))
        self.stdout.write(self.style.SUCCESS('调度模块: API测试 + UI自动化 + APP自动化 + 性能测试 + 用例生成恢复'))
        self.stdout.write(self.style.SUCCESS('=' * 60))

        while True:
            close_old_connections()
            try:
                now = timezone.localtime(timezone.now())
                self.stdout.write(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] 开始检查任务...")
                device_lease_recovery_count = self.reap_expired_app_device_leases()
                retry_count = self.consume_retry_outbox()
                generation_recovery_count = self.recover_stale_generation_tasks()
                self.reconcile_execution_claims()
                self.dispatch_pending_notifications()
                self.reconcile_performance_callbacks()
                counts = (
                    self.schedule_api_tasks(),
                    self.schedule_ui_tasks(),
                    self.schedule_app_tasks(),
                    self.schedule_perf_tasks(),
                )
                total_count = sum(counts) + retry_count + generation_recovery_count + device_lease_recovery_count
                if total_count:
                    self.stdout.write(self.style.SUCCESS(
                        f'✓ 本次调度执行了 {total_count} 个任务 '
                        f'(API: {counts[0]}, UI: {counts[1]}, APP: {counts[2]}, 性能: {counts[3]}, '
                        f'device leases recovered: {device_lease_recovery_count}, '
                        f'retry: {retry_count}, generation recovery: {generation_recovery_count})'))
                else:
                    self.stdout.write('  没有需要执行的任务')
                if run_once:
                    self.stdout.write(self.style.WARNING('单次执行模式，调度器退出'))
                    break
                self.stdout.write(f'等待 {interval} 秒后进行下一次检查...')
                time.sleep(interval)
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING('\n\n调度器已停止'))
                break
            except Exception as exc:  # noqa: BLE001 - keep a long-lived poller alive
                logger.error('调度器运行出错: %s', exc, exc_info=True)
                self.stdout.write(self.style.ERROR(f'调度器运行出错: {exc}'))
                if run_once:
                    break
                time.sleep(interval)

    def reap_expired_app_device_leases(self):
        """Reclaim expired device leases on the scheduler cadence, never on UI requests."""
        try:
            from apps.app_automation.device_pool import reap_expired_device_leases

            reclaimed = reap_expired_device_leases()
            if reclaimed:
                logger.info('expired APP device leases reclaimed: %s', reclaimed)
            return reclaimed
        except Exception as exc:  # noqa: BLE001 - optional cleanup cannot stop dispatch
            logger.error('expired APP device lease reaping failed (error_type=%s)', type(exc).__name__)
            return 0

    def reconcile_execution_claims(self):
        try:
            from apps.executions import worker_leases

            reconcile = getattr(worker_leases, 'reconcile_stale_execution_claims', None)
            return reconcile(limit=100) if callable(reconcile) else 0
        except ImportError:
            return 0
        except Exception:  # noqa: BLE001 - optional reconciliation cannot stop dispatch
            logger.exception('execution claim reconciliation failed')
            return 0

    def dispatch_pending_notifications(self):
        try:
            from apps.executions import notifications

            dispatch = getattr(notifications, 'dispatch_pending_notifications', None)
            return dispatch(limit=100) if callable(dispatch) else 0
        except ImportError:
            return 0
        except Exception:  # noqa: BLE001 - optional integration cannot stop dispatch
            logger.exception('pending notification dispatch failed')
            return 0

    def reconcile_performance_callbacks(self):
        try:
            reconciled = reconcile_unified_completions(limit=100)
            if reconciled:
                logger.info('performance unified completion callbacks retried: %s', reconciled)
            return reconciled
        except Exception as exc:  # noqa: BLE001 - scheduler loop remains available
            logger.error('performance unified completion reconciliation failed: %s', exc, exc_info=True)
            return 0

    def recover_stale_generation_tasks(self):
        try:
            from apps.requirement_analysis.tasks import recover_stale_generation_tasks

            result = recover_stale_generation_tasks.run()
            requeued = int((result or {}).get('requeued', 0))
            if requeued:
                logger.info('stale requirement-generation tasks requeued: %s', requeued)
            return requeued
        except Exception as exc:  # noqa: BLE001 - recovery cannot stop other scheduling
            logger.error('requirement-generation recovery failed: %s', exc, exc_info=True)
            return 0

    def consume_retry_outbox(self):
        try:
            receipts = consume_retry_outbox(limit=100)
            dispatched = sum(receipt.status == 'dispatched' for receipt in receipts)
            failed = sum(receipt.status == 'failed' for receipt in receipts)
            if failed:
                logger.error('retry outbox failed rows: %s', failed)
            return dispatched
        except Exception as exc:  # noqa: BLE001 - poller remains alive
            logger.error('retry outbox consumption failed: %s', exc, exc_info=True)
            return 0

    def _claim(self, model, task_id, due_field='next_run_time'):
        return claim_due_task(
            model,
            task_id,
            due_field=due_field,
            lease_seconds=self.lease_seconds,
        )

    def _mark_dispatch_failure(self, model, task_id, token, *, due_field, error):
        """Record a broker/worker dispatch failure and release the lease."""
        try:
            with transaction.atomic():
                task = load_claimed_task(model, task_id, token, require_active=True)
                task.total_runs += 1
                task.failed_runs += 1
                task.last_run_time = timezone.now()
                task.error_message = str(error)[:4000]
                task.last_result = {'status': 'failed', 'error': str(error)[:4000]}
                setattr(task, due_field, task.calculate_next_run())
                clear_claim(task, token=token)
                task.save(update_fields=[
                    'total_runs', 'failed_runs', 'last_run_time', 'error_message',
                    'last_result', due_field, 'scheduler_lease_token',
                    'scheduler_lease_expires_at', 'updated_at',
                ])
        except ScheduleClaimLost:
            logger.warning('schedule claim lost while recording dispatch failure: %s/%s', model.__name__, task_id)

    def schedule_api_tasks(self):
        try:
            from apps.api_testing.models import ScheduledTask, TaskExecutionLog
            from apps.api_testing.tasks import execute_scheduled_task

            count = 0
            for task_id in ScheduledTask.objects.filter(status='ACTIVE').values_list('pk', flat=True):
                claim = self._claim(ScheduledTask, task_id)
                if claim is None:
                    continue
                task = claim.task
                execution_log = None
                try:
                    execution_log = TaskExecutionLog.objects.create(task_id=task.pk, status='PENDING')
                    execute_scheduled_task.delay(task.pk, execution_log.pk, claim.token, self.lease_seconds)
                    count += 1
                except Exception as exc:  # broker unavailable: do not leave an invisible lease
                    if execution_log is not None:
                        execution_log.status = 'FAILED'
                        execution_log.end_time = timezone.now()
                        execution_log.error_message = str(exc)[:4000]
                        execution_log.result = {'success': False, 'error': str(exc)[:4000]}
                        execution_log.save(update_fields=['status', 'end_time', 'error_message', 'result'])
                    logger.error('API task dispatch failed: %s', exc, exc_info=True)
                    self._mark_dispatch_failure(ScheduledTask, task.pk, claim.token, due_field='next_run_time', error=exc)
            return count
        except Exception as exc:  # noqa: BLE001
            logger.error('调度API任务时出错: %s', exc, exc_info=True)
            self.stdout.write(self.style.ERROR(f'[API] 调度失败: {exc}'))
            return 0

    def schedule_ui_tasks(self):
        try:
            from apps.ui_automation.models import UiScheduledTask
            from apps.ui_automation.tasks import execute_ui_scheduled_task

            count = 0
            for task_id in UiScheduledTask.objects.filter(status='ACTIVE').values_list('pk', flat=True):
                claim = self._claim(UiScheduledTask, task_id)
                if claim is None:
                    continue
                try:
                    execute_ui_scheduled_task.delay(claim.task.pk, claim.token, self.lease_seconds)
                    count += 1
                except Exception as exc:  # broker unavailable: release as a visible failure
                    logger.error('UI task dispatch failed: %s', exc, exc_info=True)
                    self._mark_dispatch_failure(UiScheduledTask, claim.task.pk, claim.token, due_field='next_run_time', error=exc)
            return count
        except Exception as exc:  # noqa: BLE001
            logger.error('调度UI任务时出错: %s', exc, exc_info=True)
            self.stdout.write(self.style.ERROR(f'[UI] 调度失败: {exc}'))
            return 0

    def schedule_app_tasks(self):
        try:
            from apps.app_automation.models import AppDevice, AppScheduledTask, AppTestExecution
            from apps.app_automation.tasks import (
                execute_app_suite_task,
                execute_app_test_task,
                settle_undispatched_app_executions,
            )
            from apps.app_automation.execution_admission import assert_device_execution_idle
            from apps.app_automation.execution_dispatch import enqueue_app_task, new_execution_dispatch_id

            count = 0
            for task_id in AppScheduledTask.objects.filter(status='ACTIVE').values_list('pk', flat=True):
                claim = self._claim(AppScheduledTask, task_id)
                if claim is None:
                    continue
                task = claim.task
                leased_device = None
                lease_generation = None
                executions = []
                suite_id = task.test_suite_id if task.task_type == 'TEST_SUITE' else None
                try:
                    if not task.device:
                        raise ValueError('scheduled APP task has no device')
                    from apps.app_automation.device_pool import acquire_specific_device
                    leased_device, _lease_token = acquire_specific_device(
                        task.device_id,
                        task.created_by,
                        lease_seconds=self.lease_seconds,
                    )
                    lease_generation = leased_device.lease_generation
                    package_name = task.app_package.package_name if task.app_package else ''
                    dispatch_id = new_execution_dispatch_id()
                    suite_cases = []
                    with transaction.atomic():
                        leased_device = AppDevice.objects.select_for_update().get(pk=leased_device.pk)
                        assert_device_execution_idle(leased_device.pk)
                        if task.task_type == 'TEST_SUITE' and task.test_suite:
                            suite_cases = list(task.test_suite.suite_cases.select_related('test_case').all())
                            if not suite_cases:
                                raise ValueError('scheduled APP suite has no test cases')
                            for sc in suite_cases:
                                executions.append(AppTestExecution.objects.create(
                                    test_case=sc.test_case,
                                    test_suite=task.test_suite,
                                    device=leased_device,
                                    user=task.created_by,
                                    status='pending',
                                    task_id=dispatch_id,
                                    device_lease_generation=lease_generation,
                                    release_device_lease_on_stop=True,
                                ))
                            task.test_suite.execution_status = 'running'
                            task.test_suite.save(update_fields=['execution_status'])
                        elif task.task_type == 'TEST_CASE' and task.test_case:
                            executions.append(AppTestExecution.objects.create(
                                test_case=task.test_case,
                                device=leased_device,
                                user=task.created_by,
                                status='pending',
                                task_id=dispatch_id,
                                device_lease_generation=lease_generation,
                                release_device_lease_on_stop=True,
                            ))
                        else:
                            raise ValueError('scheduled APP task configuration is incomplete')

                    if task.task_type == 'TEST_SUITE':
                        enqueue_app_task(
                            execute_app_suite_task,
                            dispatch_id=dispatch_id,
                            kwargs={
                                'suite_id': task.test_suite_id,
                                'execution_ids': [execution.pk for execution in executions],
                                'package_name': package_name,
                                'scheduled_task_id': task.pk,
                                'scheduler_lease_token': claim.token,
                                'lease_seconds': self.lease_seconds,
                                'lease_generation': lease_generation,
                                'device_id': leased_device.pk,
                                'device_lease_owner_id': task.created_by_id,
                                'dispatch_id': dispatch_id,
                                'release_device_lease': True,
                            },
                        )
                    else:
                        enqueue_app_task(
                            execute_app_test_task,
                            dispatch_id=dispatch_id,
                            args=(executions[0].pk,),
                            kwargs={
                                'package_name': package_name,
                                'scheduled_task_id': task.pk,
                                'scheduler_lease_token': claim.token,
                                'lease_seconds': self.lease_seconds,
                                'lease_generation': lease_generation,
                                'release_device_lease': True,
                            },
                        )
                    count += 1
                except Exception as exc:  # noqa: BLE001
                    try:
                        settle_undispatched_app_executions(
                            [item.pk for item in executions],
                            exc,
                            suite_id=suite_id,
                            device_id=leased_device.pk if leased_device else None,
                            lease_owner=task.created_by if leased_device else None,
                            lease_generation=lease_generation,
                            release_device_lease=bool(leased_device and lease_generation is not None),
                        )
                    except Exception:  # noqa: BLE001 - failure to record cannot hide the dispatch error
                        logger.exception('failed to settle undispatched APP executions')
                    logger.error('APP task dispatch failed: %s', exc, exc_info=True)
                    self._mark_dispatch_failure(AppScheduledTask, task.pk, claim.token, due_field='next_run_time', error=exc)
            return count
        except Exception as exc:  # noqa: BLE001
            logger.error('调度APP任务时出错: %s', exc, exc_info=True)
            self.stdout.write(self.style.ERROR(f'[APP] 调度失败: {exc}'))
            return 0

    def schedule_perf_tasks(self):
        try:
            from apps.perf_testing.models import PerfScheduledTask
            from apps.perf_testing.services import executor

            count = 0
            for task_id in PerfScheduledTask.objects.filter(status='ACTIVE').values_list('pk', flat=True):
                claim = self._claim(PerfScheduledTask, task_id, due_field='next_run_at')
                if claim is None:
                    continue
                task = claim.task
                execution = None
                try:
                    scenario = task.scenario
                    if scenario.has_active_execution():
                        raise RuntimeError('场景已有执行中的压测')
                    execution, check = executor.start_execution(
                        scenario,
                        user=task.created_by,
                        trigger_type='SCHEDULED',
                        scheduled_task=task,
                    )
                    if execution is None:
                        errors = (check or {}).get('errors') or []
                        raise RuntimeError('; '.join(errors) if errors else '执行前检查未通过')
                    task.mark_dispatched(scheduler_lease_token=claim.token)
                    count += 1
                except Exception as exc:  # noqa: BLE001
                    if execution is not None:
                        try:
                            executor.stop_execution(execution, graceful=False)
                        except Exception:  # noqa: BLE001 - stop any orphaned child before reporting failure
                            logger.exception('failed to stop undispatched performance child: %s', execution.pk)
                    logger.error('性能任务 dispatch failed: %s', exc, exc_info=True)
                    self._mark_perf_dispatch_failure(task.pk, claim.token, exc)
            return count
        except Exception as exc:  # noqa: BLE001
            logger.error('调度性能任务时出错: %s', exc, exc_info=True)
            self.stdout.write(self.style.ERROR(f'[性能] 调度失败: {exc}'))
            return 0

    def _mark_perf_dispatch_failure(self, task_id, token, error):
        from apps.perf_testing.models import PerfScheduledTask

        try:
            task = PerfScheduledTask.objects.get(pk=task_id)
            task.mark_dispatched(error=str(error), scheduler_lease_token=token)
        except ScheduleClaimLost:
            logger.warning('performance schedule claim lost while recording failure: %s', task_id)
        except Exception:
            logger.error('回写性能任务失败状态时出错: %s', task_id, exc_info=True)
