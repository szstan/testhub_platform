# -*- coding: utf-8 -*-
"""
APP自动化测试 Celery 任务
"""
from celery import shared_task
from django.utils import timezone
from django.db import transaction
import logging
import os
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from apps.core.cancellation import combine_cancel_checks
from apps.core.http_transport import guarded_post
from apps.core.scheduler import ScheduleLeaseHeartbeat, load_claimed_task


logger = logging.getLogger(__name__)

_DIAGNOSTIC_CASE_CHANGED_MESSAGE = '测试用例已变更，请重新提交单步诊断'
_SAFE_UNDISPATCHED_MESSAGES = {
    '无法提交执行任务，请检查任务队列',
    '设备已有其他待执行或正在执行的任务',
    _DIAGNOSTIC_CASE_CHANGED_MESSAGE,
}
_SAFE_PROGRESS_MESSAGES = {
    '任务开始执行',
    '正在准备测试环境',
    '正在生成测试报告',
    '执行完成',
    '任务已停止',
    '用例不存在',
    '测试结果不可用',
    '测试结果格式无效',
    '测试进程异常退出，无法确认执行结果',
    '测试进程无法确认退出，设备占用已保留；请检查执行进程',
    '设备租约已失效，请重新获取租约后重试',
    'APP worker运行失败，请检查依赖、ADB和Allure配置后重试',
    'APP执行失败，请检查设备状态和工作进程配置后重试',
    _DIAGNOSTIC_CASE_CHANGED_MESSAGE,
    'step complete',
}


def _safe_app_error_message(error):
    if type(error) is str and error in _SAFE_UNDISPATCHED_MESSAGES:
        return error

    from .device_pool import DeviceLeaseError
    from .executors.test_executor import AppTestProcessTerminationError, AppTestWorkerError
    from .scene_execution import ScenePreparationError, SCENE_CASE_CHANGED_MESSAGE

    if (
        isinstance(error, ScenePreparationError)
        and error.args == (SCENE_CASE_CHANGED_MESSAGE,)
    ):
        return SCENE_CASE_CHANGED_MESSAGE
    if isinstance(error, AppTestProcessTerminationError):
        return '测试进程无法确认退出，设备占用已保留；请检查执行进程'
    if isinstance(error, DeviceLeaseError):
        return '设备租约已失效，请重新获取租约后重试'
    if isinstance(error, AppTestWorkerError):
        return 'APP worker运行失败，请检查依赖、ADB和Allure配置后重试'
    return 'APP执行失败，请检查设备状态和工作进程配置后重试'


def _safe_progress_message(message):
    if type(message) is not str:
        return None if message is None else '执行进度已更新'
    if message in _SAFE_PROGRESS_MESSAGES:
        return message

    if message.startswith('步骤 ') and ': ' in message:
        progress, detail = message.split(': ', 1)
        counts = progress.removeprefix('步骤 ').split('/', 1)
        outcome = detail.rsplit(' - ', 1)
        if (
            len(counts) == 2
            and all(part.isdecimal() for part in counts)
            and len(outcome) == 2
            and outcome[1] in {'通过', '失败'}
        ):
            return f'{progress} - {outcome[1]}'

    if message.startswith('开始执行 (') and message.endswith(')'):
        counts = message[len('开始执行 ('):-1].split('/', 1)
        if len(counts) == 2 and all(part.isdecimal() for part in counts):
            return message

    return '执行进度已更新'


def classify_app_test_result(report_result):
    """Do not claim a verified pass when the process or its test evidence disagrees."""
    if not isinstance(report_result, dict):
        return 'error', None, '测试结果不可用'
    stats = report_result.get('test_results') or {}
    try:
        total = int(stats.get('total', 0) or 0)
        passed = int(stats.get('passed', 0) or 0)
        failed = int(stats.get('failed', 0) or 0)
        skipped = int(stats.get('skipped', 0) or 0)
        unverified = int(stats.get('unverified', 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return 'error', None, '测试结果格式无效'
    if min(total, passed, failed, skipped, unverified) < 0:
        return 'error', None, '测试结果格式无效'
    if failed:
        return 'completed', 'failed', ''
    exit_code = report_result.get('exit_code')
    if (
        report_result.get('success') is False
        and type(exit_code) is int and exit_code == 0
        and report_result.get('error') == 'Allure report generation failed after pytest completed'
    ):
        # This is a fixed, server-owned stage message. Never echo arbitrary
        # worker exception text, which may include paths or sensitive inputs.
        return 'error', None, '测试进程已结束，但 Allure 报告生成失败，请检查报告配置'
    if report_result.get('success') is False or (exit_code is not None and exit_code != 0):
        return 'error', None, '测试进程异常退出，无法确认执行结果'
    if not total:
        return 'error', None, '未获得有效测试结果'
    if unverified:
        return 'completed', 'unverified', ''
    if skipped >= total:
        return 'completed', 'skipped', ''
    if skipped or passed < total:
        return 'completed', 'unverified', ''
    return 'completed', 'passed', ''


def worker_io_completed_normally(report_result):
    """Recognize only explicit worker completion or pre-spawn evidence."""
    if not isinstance(report_result, dict):
        return False
    exit_code = report_result.get('exit_code')
    if (
        type(exit_code) is int
        and exit_code == 0
        and report_result.get('pytest_process_started') is True
        and report_result.get('pytest_process_completed') is True
    ):
        return True
    return (
        exit_code is None
        and report_result.get('device_io_admitted') is True
        and report_result.get('pytest_process_started') is False
        and report_result.get('pytest_process_completed') is False
    )


def settle_undispatched_app_executions(
    execution_ids,
    error,
    *,
    suite_id=None,
    device_id=None,
    lease_owner=None,
    lease_generation=None,
    release_device_lease=False,
):
    """Fail an unclaimed publish and release only the lease it acquired."""
    from .models import AppDevice, AppTestExecution
    from .execution_admission import release_device_if_idle, settle_app_suite_if_idle

    execution_ids = list(dict.fromkeys(execution_ids or []))
    now = timezone.now()
    message = _safe_app_error_message(error)
    settled_count = 0
    with transaction.atomic():
        if device_id is None and execution_ids:
            device_id = AppTestExecution.objects.filter(pk__in=execution_ids).values_list(
                'device_id', flat=True
            ).first()
        if device_id is not None:
            AppDevice.objects.select_for_update().filter(pk=device_id).first()

        executions = list(
            AppTestExecution.objects.select_for_update().filter(pk__in=execution_ids)
        )
        may_be_running = any(
            execution.status == 'running'
            or (execution.started_at is not None and execution.status in {'pending', 'stopped'})
            for execution in executions
        )
        if not may_be_running:
            settled_count = AppTestExecution.objects.filter(
                pk__in=execution_ids,
                status='pending',
            ).update(
                status='error',
                result=None,
                error_message=message,
                finished_at=now,
                updated_at=now,
            )
            if suite_id and settled_count:
                suite_executions = AppTestExecution.objects.filter(
                    test_suite_id=suite_id, pk__in=execution_ids,
                )
                settle_app_suite_if_idle(
                    suite_id,
                    execution_ids=execution_ids,
                    status='error',
                    result=None,
                    passed_count=suite_executions.filter(status='completed', result='passed').count(),
                    failed_count=suite_executions.filter(status='error').count() or settled_count,
                    last_run_at=now,
                )
            if release_device_lease and device_id is not None and lease_owner is not None:
                release_device_if_idle(device_id, lease_owner, lease_generation)

    return settled_count


def _record_scheduled_result(task_id, success, result, scheduler_lease_token=''):
    """Persist APP schedule counters and release a scheduler lease when owned."""
    from .models import AppScheduledTask
    from apps.core.scheduler import clear_claim, load_claimed_task

    with transaction.atomic():
        if scheduler_lease_token:
            task = load_claimed_task(
                AppScheduledTask,
                task_id,
                scheduler_lease_token,
                require_active=True,
            )
            task.total_runs += 1
            task.last_run_time = timezone.now()
            task.next_run_time = task.calculate_next_run()
            clear_claim(task, token=scheduler_lease_token)
            fields = [
                'total_runs', 'last_run_time', 'next_run_time',
                'scheduler_lease_token', 'scheduler_lease_expires_at', 'updated_at',
            ]
        else:
            task = AppScheduledTask.objects.select_for_update().get(pk=task_id)
            fields = []
        if success:
            task.successful_runs += 1
            fields.append('successful_runs')
        else:
            task.failed_runs += 1
            fields.append('failed_runs')
        task.last_result = result or {}
        fields.extend(['last_result', 'updated_at'])
        task.save(update_fields=list(dict.fromkeys(fields)))
    return task


def _assert_scheduled_task_scope(*, scheduled_task_id, project_id, device_id,
                                 app_package_id=None, test_suite_id=None,
                                 test_case_id=None):
    """Revalidate scheduler foreign keys at the worker boundary."""
    if not scheduled_task_id:
        return
    from .models import AppScheduledTask
    task = AppScheduledTask.objects.select_related(
        'project', 'device', 'app_package', 'test_suite', 'test_case'
    ).filter(pk=scheduled_task_id).first()
    if task is None or task.project_id != project_id or task.device_id != device_id:
        raise RuntimeError('定时任务资源已变更或不属于同一项目')
    if app_package_id is not None and task.app_package_id != app_package_id:
        raise RuntimeError('定时任务应用包已变更')
    if test_suite_id is not None and task.test_suite_id != test_suite_id:
        raise RuntimeError('定时任务测试套件已变更')
    if test_case_id is not None and task.test_case_id != test_case_id:
        raise RuntimeError('定时任务测试用例已变更')


def send_scheduled_task_notification(task_id, success):
    """发送定时任务执行通知（Webhook + 邮件）"""
    try:
        from .models import AppScheduledTask, AppNotificationLog

        task = AppScheduledTask.objects.get(id=task_id)

        if success and not task.notify_on_success:
            return
        if not success and not task.notify_on_failure:
            return
        if not task.notification_type:
            return

        status_text = '成功' if success else '失败'
        last_result = task.last_result or {}
        result_message = last_result.get('message', '')
        local_run_time = timezone.localtime(task.last_run_time).strftime('%Y-%m-%d %H:%M:%S') if task.last_run_time else '未知'
        device_name = task.device.name or '未命名设备' if task.device else '未指定'

        detail_content = (
            f"任务名称: {task.name}\n\n"
            f"执行状态: {status_text}\n\n"
            f"执行时间: {local_run_time}\n\n"
            f"任务类型: {task.get_task_type_display()}\n\n"
            f"执行设备: {device_name}"
        )
        if result_message:
            detail_content += f"\n\n执行结果: {result_message}"

        # Webhook 通知
        if task.notification_type in ['webhook', 'both']:
            _send_app_webhook_notification(task, detail_content, status_text)

        # 邮件通知
        if task.notification_type in ['email', 'both']:
            _send_app_email_notification(task, detail_content, status_text)

    except Exception as e:
        logger.error(
            "APP scheduled task notification failed (error_type=%s)", type(e).__name__
        )


def _send_app_webhook_notification(task, detail_content, status_text):
    """发送 Webhook 通知"""
    import requests
    import json
    from django.conf import settings
    from apps.core.network import UnsafeOutboundURL, validate_outbound_url
    from apps.quality_intelligence.services.redaction import redact_payload
    from .models import AppNotificationLog

    try:
        from apps.core.models import UnifiedNotificationConfig
        configs = UnifiedNotificationConfig.objects.filter(
            config_type__in=['webhook_wechat', 'webhook_feishu', 'webhook_dingtalk'],
            is_active=True
        )
    except Exception as e:
        logger.error("Notification configuration lookup failed (error_type=%s)", type(e).__name__)
        return

    all_bots = []
    for config in configs:
        for bot in config.get_webhook_bots():
            if bot.get('enabled', True):
                all_bots.append(bot)

    if not all_bots:
        return

    for bot in all_bots:
        webhook_url = bot.get('webhook_url')
        if not webhook_url:
            continue

        try:
            webhook_url = validate_outbound_url(
                webhook_url,
                allow_private=bool(getattr(settings, 'MONITOR_ALLOW_PRIVATE_TARGETS', False)),
            )
        except UnsafeOutboundURL:
            logger.warning('拒绝向不安全 Webhook 地址发送 APP 通知')
            continue

        bot_type = bot.get('type', 'unknown')
        success = status_text == '成功'

        if bot_type == 'wechat':
            message_data = {"msgtype": "markdown", "markdown": {"content": f"**APP自动化定时任务执行{status_text}**\n\n{detail_content}"}}
        elif bot_type == 'feishu':
            message_data = {"msg_type": "interactive", "card": {"elements": [{"tag": "div", "text": {"content": f"**APP自动化定时任务执行{status_text}**\n\n{detail_content}", "tag": "lark_md"}}], "header": {"title": {"content": f"APP自动化定时任务执行{status_text}", "tag": "plain_text"}, "template": "green" if success else "red"}}}
        elif bot_type == 'dingtalk':
            message_data = {"msgtype": "markdown", "markdown": {"title": f"APP自动化定时任务执行{status_text}", "text": f"**APP自动化定时任务执行{status_text}**\n\n{detail_content}"}}
            secret = bot.get('secret')
            if secret:
                import time as _time, hmac, hashlib, base64, urllib.parse
                timestamp = str(round(_time.time() * 1000))
                sign = urllib.parse.quote_plus(base64.b64encode(hmac.new(secret.encode('utf-8'), f'{timestamp}\n{secret}'.encode('utf-8'), digestmod=hashlib.sha256).digest()))
                webhook_url += f'{"&" if "?" in webhook_url else "?"}timestamp={timestamp}&sign={sign}'
        else:
            continue

        try:
            resp = guarded_post(webhook_url, json=message_data, headers={'Content-Type': 'application/json'}, timeout=10,
                                allow_private=bool(getattr(settings, 'MONITOR_ALLOW_PRIVATE_TARGETS', False)))
            log_status = 'success' if resp.status_code == 200 else 'failed'
            AppNotificationLog.objects.create(
                task=task, task_name=task.name, task_type=task.task_type,
                notification_type='task_execution', sender_name='系统Webhook通知',
                sender_email='system@notification.com',
                recipient_info=[{'name': bot.get('name', 'Unknown')}],
                webhook_bot_info={'type': bot_type, 'name': bot.get('name', 'Unknown'), 'configured': True},
                notification_content=redact_payload(json.dumps(message_data, ensure_ascii=False)),
                status=log_status,
                error_message='' if log_status == 'success' else f'HTTP {resp.status_code}',
                response_info={'status_code': resp.status_code},
                sent_at=timezone.now()
            )
        except Exception:
            logger.error('发送Webhook失败')
            AppNotificationLog.objects.create(
                task=task, task_name=task.name, task_type=task.task_type,
                notification_type='task_execution', sender_name='系统Webhook通知',
                sender_email='system@notification.com',
                recipient_info=[{'name': bot.get('name', 'Unknown')}],
                webhook_bot_info={'type': bot_type, 'name': bot.get('name', 'Unknown'), 'configured': True},
                notification_content=redact_payload(json.dumps(message_data, ensure_ascii=False)),
                status='failed', error_message='Webhook notification failed'
            )


def _send_app_email_notification(task, detail_content, status_text):
    """发送邮件通知"""
    from .models import AppNotificationLog

    recipients = task.notify_emails if isinstance(task.notify_emails, list) else []
    if not recipients:
        return

    try:
        from django.core.mail import send_mail
        from django.conf import settings

        subject = f"APP自动化定时任务执行{status_text}: {task.name}"
        from_email = settings.DEFAULT_FROM_EMAIL

        send_mail(subject=subject, message=detail_content, from_email=from_email, recipient_list=recipients, fail_silently=False)

        AppNotificationLog.objects.create(
            task=task, task_name=task.name, task_type=task.task_type,
            notification_type='task_execution', sender_name='系统邮件通知',
            sender_email=from_email,
            recipient_info=[{'email': e} for e in recipients],
            notification_content=detail_content, status='success', sent_at=timezone.now()
        )
    except Exception as e:
        logger.error("Email notification failed (error_type=%s)", type(e).__name__)
        AppNotificationLog.objects.create(
            task=task, task_name=task.name, task_type=task.task_type,
            notification_type='task_execution', sender_name='系统邮件通知',
            sender_email='',
            recipient_info=[{'email': e} for e in recipients],
            notification_content="发送失败: 邮件发送失败", status='failed', error_message='邮件发送失败'
        )


def send_execution_update(execution_id, status=None, progress=None, message=None, report_path=None, finished_at=None, result=None):
    """通过 WebSocket 发送执行状态更新，并报告通道不可用。"""
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            logger.warning(
                "APP execution WebSocket event skipped: channel layer unavailable "
                "execution_id=%s",
                execution_id,
            )
            return False
        payload = {
            "type": "execution_update",
            "execution_id": int(execution_id),
            "status": status if type(status) is str and status in {'running', 'completed', 'error', 'stopped'} else None,
            "result": result if type(result) is str and result in {'passed', 'failed', 'unverified', 'skipped'} else None,
            "progress": progress if type(progress) is int and 0 <= progress <= 100 else None,
            "message": _safe_progress_message(message),
            "report_path": bool(report_path),
            "finished_at": finished_at.isoformat() if finished_at else None,
        }
        async_to_sync(channel_layer.group_send)(
            f"app_execution_{execution_id}",
            payload
        )
        return True
    except Exception as exc:
        logger.warning(
            "APP execution WebSocket event failed: execution_id=%s error_type=%s",
            execution_id,
            type(exc).__name__,
        )
        return False


@shared_task
def execute_app_test_task(
    execution_id,
    package_name: str = None,
    scheduled_task_id: int = None,
    scheduler_lease_token: str = '',
    lease_seconds: int = 300,
    lease_generation: int = None,
    release_device_lease: bool = True,
    cancel_check=None,
    step_index: int = None,
    expected_case_updated_at: str = None,
):
    """
    异步执行APP测试任务

    Args:
        execution_id: AppTestExecution 的 ID
        package_name: 可选的应用包名
        scheduled_task_id: 可选的定时任务 ID（来自定时调度）
    """
    from django.conf import settings
    from .models import AppTestExecution, AppDevice, AppScheduledTask
    from .device_pool import (
        DeviceLeaseError,
        DeviceLeaseHeartbeat,
    )
    from .execution_admission import (
        claim_app_execution_batch,
        clear_device_action_pending,
        mark_device_action_pending,
        release_device_if_idle,
        settle_app_execution_failure,
    )
    from .executors.test_executor import AppTestExecutor, AppTestProcessTerminationError

    execution = None
    device = None
    bound_generation = lease_generation
    unified_cancel_check = cancel_check
    heartbeat = None
    device_heartbeat = None
    worker_claimed = False
    unsafe_termination = False
    diagnostic_requested = step_index is not None or expected_case_updated_at is not None
    pre_dispatch_diagnostic_failure = False
    device_io_admitted = False
    original_worker_cwd = os.getcwd()

    try:
        execution = AppTestExecution.objects.select_related('device', 'user', 'test_case').get(id=execution_id)
        test_case = execution.test_case
        if lease_generation is not None and execution.device_lease_generation != lease_generation:
            raise DeviceLeaseError('入队设备租约世代与执行记录不匹配')
        if bound_generation is None:
            bound_generation = execution.device_lease_generation

        device = execution.device
        if diagnostic_requested:
            from .scene_execution import validate_diagnostic_selection
            validate_diagnostic_selection(test_case, step_index, expected_case_updated_at)
        if not claim_app_execution_batch([execution.pk]):
            return
        worker_claimed = True
        execution.refresh_from_db()
        if diagnostic_requested:
            test_case.refresh_from_db(fields=['ui_flow', 'updated_at'])
            validate_diagnostic_selection(test_case, step_index, expected_case_updated_at)
        _assert_scheduled_task_scope(
            scheduled_task_id=scheduled_task_id,
            project_id=test_case.project_id,
            device_id=execution.device_id,
            app_package_id=test_case.app_package_id,
            test_case_id=test_case.id,
        )
        if scheduled_task_id and scheduler_lease_token:
            load_claimed_task(
                AppScheduledTask, scheduled_task_id, scheduler_lease_token, require_active=True
            )
            heartbeat = ScheduleLeaseHeartbeat(
                AppScheduledTask,
                scheduled_task_id,
                scheduler_lease_token,
                lease_seconds=lease_seconds,
            ).start()

        device_heartbeat = DeviceLeaseHeartbeat(
            device.pk,
            execution.user,
            bound_generation,
            execution_id=execution.pk,
            lease_seconds=lease_seconds,
        ).start()

        def execution_not_stopped():
            if not AppTestExecution.objects.filter(
                pk=execution.pk, status='running', stop_requested=False
            ).exists():
                raise RuntimeError('APP执行已停止或不再处于运行状态')

        cancel_checks = [device_heartbeat.raise_if_lost, execution_not_stopped]
        if unified_cancel_check is not None:
            cancel_checks.append(unified_cancel_check)
        if heartbeat is not None:
            cancel_checks.append(heartbeat.raise_if_lost)
        cancel_check = combine_cancel_checks(*cancel_checks)
        cancel_check()

        send_execution_update(execution_id, status='running', progress=0, message='任务开始执行')
        logger.info('开始执行APP测试')

        execution.progress = 10
        execution.save(update_fields=['progress'])
        send_execution_update(execution_id, status='running', progress=10, message='正在准备测试环境')

        if package_name:
            final_package_name = package_name
        else:
            final_package_name = test_case.app_package.package_name if test_case.app_package else ""

        executor = AppTestExecutor()
        diagnostic_kwargs = {}
        if step_index is not None:
            diagnostic_kwargs = {
                'step_index': step_index,
                'expected_case_updated_at': expected_case_updated_at,
            }
        def admit_device_io():
            nonlocal device_io_admitted
            mark_device_action_pending(execution.pk)
            device_io_admitted = True

        report_result = executor.run_tests(
            test_case_id=test_case.id,
            device_id=device.device_id,
            package_name=final_package_name,
            execution_id=execution_id,
            username=execution.user.username if execution.user else 'unknown',
            cancel_check=cancel_check,
            before_device_io=admit_device_io,
            **diagnostic_kwargs,
        )
        pre_dispatch_diagnostic_failure = (
            diagnostic_requested
            and not device_io_admitted
            and isinstance(report_result, dict)
            and report_result.get('success') is False
            and report_result.get('exit_code') is None
            and report_result.get('device_io_admitted') is False
            and report_result.get('pytest_process_started') is False
            and report_result.get('pytest_process_completed') is False
        )
        cancel_check()
        if worker_io_completed_normally(report_result):
            clear_device_action_pending(execution.pk)

        # 从数据库重新读取最新进度（子进程中的回调可能已经更新过）
        execution.refresh_from_db()

        if report_result.get('report_path'):
            execution.report_path = report_result['report_path']
            logger.info("APP test report generated")

        test_results = report_result.get('test_results', {})
        execution.total_steps = test_results.get('total', 0)
        execution.passed_steps = test_results.get('passed', 0)
        # parser 已将 broken 算在 failed 里，直接使用
        execution.failed_steps = test_results.get('failed', 0)
        if test_results.get('broken', 0):
            logger.info(f"检测到 broken 用例 {test_results.get('broken')} 个（已计入失败统计）。")

        execution.progress = 95
        execution.save(update_fields=['progress', 'report_path', 'total_steps', 'passed_steps', 'failed_steps'])
        send_execution_update(
            execution_id,
            status='running',
            progress=95,
            message='正在生成测试报告',
            report_path=execution.report_path
        )

        # 3. 完成测试 — 结合进程退出状态、断言与 Allure 结果。
        execution.status, execution.result, execution.error_message = classify_app_test_result(report_result)
        execution.finished_at = timezone.now()
        execution.duration = (execution.finished_at - execution.started_at).total_seconds()
        execution.progress = 100
        updated = AppTestExecution.objects.filter(
            pk=execution.pk,
            status='running',
            stop_requested=False,
        ).update(
            status=execution.status,
            result=execution.result,
            error_message=execution.error_message,
            finished_at=execution.finished_at,
            duration=execution.duration,
            progress=100,
            report_path=execution.report_path,
            total_steps=execution.total_steps,
            passed_steps=execution.passed_steps,
            failed_steps=execution.failed_steps,
            updated_at=timezone.now(),
        )
        if not updated:
            cancel_check()
            return
        execution.refresh_from_db()
        send_execution_update(
            execution_id,
            status=execution.status,
            progress=100,
            message=execution.error_message if execution.status == 'error' else '执行完成',
            report_path=execution.report_path,
            finished_at=execution.finished_at,
            result=execution.result,
        )

        logger.info('APP测试执行完成: 状态: %s, 结果: %s', execution.status, execution.result)

        # 定时任务通知和 durable schedule completion
        if scheduled_task_id:
            try:
                is_success = execution.result == 'passed'
                result = {
                    'status': execution.status,
                    'result': execution.result,
                    'message': f'APP test execution: {execution.result or execution.status}',
                }
                _record_scheduled_result(
                    scheduled_task_id,
                    is_success,
                    result,
                    scheduler_lease_token=scheduler_lease_token,
                )
                send_scheduled_task_notification(scheduled_task_id, success=is_success)
            except Exception as ne:
                logger.error(
                    "APP scheduled result update failed (error_type=%s)", type(ne).__name__
                )

    except AppTestExecution.DoesNotExist:
        logger.error("APP execution record not found")
    except Exception as e:
        pre_dispatch_diagnostic_failure = diagnostic_requested and not device_io_admitted
        safe_error = _safe_app_error_message(e)
        logger.error("APP test execution failed (error_type=%s)", type(e).__name__)
        unsafe_termination = isinstance(e, AppTestProcessTerminationError)
        if unsafe_termination:
            os.chdir(original_worker_cwd)
            if execution:
                AppTestExecution.objects.filter(pk=execution.pk, status='running').update(
                    stop_requested=True, error_message=safe_error, updated_at=timezone.now()
                )

        if execution and not unsafe_termination:
            settled_ids = settle_app_execution_failure(
                [execution.pk], safe_error, include_running=worker_claimed
            )
            if settled_ids:
                execution.refresh_from_db()
                send_execution_update(
                    execution_id,
                    status=execution.status,
                    progress=execution.progress or 0,
                    message='任务已停止' if execution.status == 'stopped' else safe_error,
                    report_path=execution.report_path,
                    finished_at=execution.finished_at,
                    result=None,
                )
                try:
                    executor = AppTestExecutor()
                    executor._generate_allure_report(execution_id=execution_id)
                except Exception:
                    pass

        if scheduled_task_id and scheduler_lease_token and not unsafe_termination:
            try:
                _record_scheduled_result(
                    scheduled_task_id,
                    False,
                    {'status': 'error', 'message': safe_error},
                    scheduler_lease_token=scheduler_lease_token,
                )
            except Exception as schedule_error:
                logger.error(
                    "APP schedule failure update failed (error_type=%s)",
                    type(schedule_error).__name__,
                )
    finally:
        if device_heartbeat is not None:
            device_heartbeat.stop()
        if heartbeat is not None:
            heartbeat.stop()
        if release_device_lease and not unsafe_termination and device and execution and execution.user:
            should_release = bool(scheduled_task_id) or (
                pre_dispatch_diagnostic_failure and execution.release_device_lease_on_stop
            ) or (
                worker_claimed and AppTestExecution.objects.filter(
                    pk=execution.pk, stop_requested=True
                ).exists()
            )
            if should_release:
                try:
                    release_device_if_idle(device.pk, execution.user, bound_generation)
                except Exception as e:
                    logger.error(
                        "APP device lease release failed (error_type=%s)",
                        type(e).__name__,
                    )


@shared_task
def execute_app_suite_task(
    suite_id,
    execution_ids,
    package_name=None,
    scheduled_task_id=None,
    scheduler_lease_token='',
    lease_seconds: int = 300,
    lease_generation: int = None,
    device_id: int = None,
    device_lease_owner_id: int = None,
    release_device_lease: bool = True,
    cancel_check=None,
    dispatch_id: str = '',
):
    """
    异步执行APP测试套件（顺序执行多个用例）

    Args:
        suite_id: AppTestSuite 的 ID
        execution_ids: AppTestExecution ID 列表（按执行顺序）
        package_name: 可选的应用包名覆盖
        scheduled_task_id: 可选的定时任务 ID
    """
    from django.contrib.auth import get_user_model
    from .models import AppTestSuite, AppTestExecution, AppDevice, AppScheduledTask
    from .device_pool import (
        DeviceLeaseError,
        DeviceLeaseHeartbeat,
    )
    from .execution_admission import (
        claim_app_execution_batch,
        clear_device_action_pending,
        mark_device_action_pending,
        release_device_if_idle,
        settle_app_execution_failure,
        settle_app_suite_if_idle,
        start_next_app_execution,
        summarize_app_suite_dispatch,
    )
    from .executors.test_executor import (
        AppTestExecutor,
        AppTestProcessTerminationError,
        AppTestWorkerError,
    )

    suite = None
    device = None
    user = None
    bound_generation = lease_generation
    unified_cancel_check = cancel_check
    passed = 0
    failed = 0
    unverified = 0
    skipped = 0
    heartbeat = None
    device_heartbeat = None
    executions = []
    worker_claimed = False
    unsafe_termination = False
    original_worker_cwd = os.getcwd()

    try:
        if device_id is not None:
            device = AppDevice.objects.filter(pk=device_id).first()
        if device_lease_owner_id is not None:
            user = get_user_model().objects.filter(pk=device_lease_owner_id).first()

        executions = list(
            AppTestExecution.objects.filter(id__in=execution_ids)
            .select_related('test_case', 'test_case__app_package', 'device', 'user')
            .order_by('id')
        )
        # Preserve dispatch order for both execution and failure settlement.
        exec_map = {e.id: e for e in executions}
        executions = [exec_map[eid] for eid in execution_ids if eid in exec_map]

        if executions:
            first_execution = executions[0]
            if device_id is not None and str(first_execution.device_id) != str(device_id):
                raise DeviceLeaseError('套件执行记录的设备与派发租约不一致')
            if device_lease_owner_id is not None and str(first_execution.user_id) != str(device_lease_owner_id):
                raise DeviceLeaseError('套件执行记录的用户与派发租约不一致')
            device = first_execution.device
            user = first_execution.user

        if not executions:
            raise RuntimeError(f'执行批次 {suite_id or dispatch_id or "unknown"} 未找到执行记录')

        generations = {item.device_lease_generation for item in executions}
        if lease_generation is not None:
            bound_generation = lease_generation
        elif len(generations) == 1:
            bound_generation = next(iter(generations))
        else:
            raise DeviceLeaseError('套件执行记录的设备租约世代不一致')
        if any(
            item.device_id != executions[0].device_id
            or item.user_id != executions[0].user_id
            or item.device_lease_generation != bound_generation
            or (dispatch_id and item.task_id != dispatch_id)
            for item in executions
        ):
            raise DeviceLeaseError('套件执行记录未绑定到同一设备租约或派发批次')

        if not claim_app_execution_batch(execution_ids, suite_id=suite_id):
            return
        worker_claimed = True
        executions = list(
            AppTestExecution.objects.filter(pk__in=execution_ids)
            .select_related('test_case', 'test_case__app_package', 'device', 'user')
        )
        exec_map = {item.pk: item for item in executions}
        executions = [exec_map[execution_id] for execution_id in execution_ids]
        device = executions[0].device
        user = executions[0].user
        suite = AppTestSuite.objects.get(pk=suite_id) if suite_id is not None else None

        _assert_scheduled_task_scope(
            scheduled_task_id=scheduled_task_id,
            project_id=suite.project_id if suite else executions[0].test_case.project_id,
            device_id=executions[0].device_id,
            app_package_id=executions[0].test_case.app_package_id if executions[0].test_case else None,
            test_suite_id=suite.id if suite else None,
        )
        if scheduled_task_id and scheduler_lease_token:
            load_claimed_task(
                AppScheduledTask, scheduled_task_id, scheduler_lease_token, require_active=True
            )
            heartbeat = ScheduleLeaseHeartbeat(
                AppScheduledTask,
                scheduled_task_id,
                scheduler_lease_token,
                lease_seconds=lease_seconds,
            ).start()

        device_heartbeat = DeviceLeaseHeartbeat(
            device.pk,
            user,
            bound_generation,
            execution_id=executions[0].pk,
            lease_seconds=lease_seconds,
        ).start()
        cancel_checks = [device_heartbeat.raise_if_lost]
        if unified_cancel_check is not None:
            cancel_checks.append(unified_cancel_check)
        if heartbeat is not None:
            cancel_checks.append(heartbeat.raise_if_lost)
        cancel_check = combine_cancel_checks(*cancel_checks)
        cancel_check()
        if suite:
            logger.info('套件执行开始: 共 %s 个用例', len(executions))
        else:
            logger.info('APP批量执行开始: 共 %s 个用例', len(executions))

        batch_ids = [item.pk for item in executions]
        for idx, execution in enumerate(executions):
            if idx:
                if not start_next_app_execution(execution.pk, batch_ids):
                    execution.refresh_from_db()
                    if execution.status in {'stopped', 'error'}:
                        raise RuntimeError('APP执行批次已停止或已结算')
                    continue
                execution.refresh_from_db()
            def execution_not_stopped():
                if not AppTestExecution.objects.filter(
                    pk=execution.pk, status='running', stop_requested=False
                ).exists():
                    raise RuntimeError('APP执行已停止或不再处于运行状态')

            case_cancel_check = combine_cancel_checks(cancel_check, execution_not_stopped)
            case_cancel_check()
            test_case = execution.test_case
            if not test_case:
                now = timezone.now()
                AppTestExecution.objects.filter(pk=execution.pk, status='running').update(
                    status='error', result=None, error_message='用例不存在',
                    finished_at=now, updated_at=now,
                )
                failed += 1
                continue

            try:
                send_execution_update(
                    execution.id, status='running', progress=0,
                    message=f'开始执行 ({idx + 1}/{len(executions)})'
                )

                # 确定包名
                if package_name:
                    final_pkg = package_name
                else:
                    final_pkg = test_case.app_package.package_name if test_case.app_package else ""

                execution.progress = 10
                execution.save(update_fields=['progress'])
                send_execution_update(
                    execution.id, status='running', progress=10,
                    message='正在准备测试环境'
                )

                executor = AppTestExecutor()
                report_result = executor.run_tests(
                    test_case_id=test_case.id,
                    device_id=device.device_id,
                    package_name=final_pkg,
                    execution_id=execution.id,
                    username=execution.user.username if execution.user else 'unknown',
                    cancel_check=case_cancel_check,
                    before_device_io=lambda: mark_device_action_pending(execution.pk),
                )
                case_cancel_check()
                if worker_io_completed_normally(report_result):
                    clear_device_action_pending(execution.pk)
                else:
                    execution.refresh_from_db(fields=['device_action_pending'])
                    if execution.device_action_pending:
                        raise AppTestWorkerError('APP worker did not complete cleanly')

                execution.refresh_from_db()

                if report_result.get('report_path'):
                    execution.report_path = report_result['report_path']

                test_results = report_result.get('test_results', {})
                execution.total_steps = test_results.get('total', 0)
                execution.passed_steps = test_results.get('passed', 0)
                execution.failed_steps = test_results.get('failed', 0)
                if test_results.get('broken', 0):
                    logger.info(f"检测到 broken 用例 {test_results.get('broken')} 个（已计入失败统计）。")

                execution.status, execution.result, execution.error_message = classify_app_test_result(report_result)
                execution.finished_at = timezone.now()
                execution.duration = (execution.finished_at - execution.started_at).total_seconds()
                execution.progress = 100
                updated = AppTestExecution.objects.filter(
                    pk=execution.pk,
                    status='running',
                    stop_requested=False,
                ).update(
                    status=execution.status,
                    result=execution.result,
                    error_message=execution.error_message,
                    finished_at=execution.finished_at,
                    duration=execution.duration,
                    progress=100,
                    report_path=execution.report_path,
                    total_steps=execution.total_steps,
                    passed_steps=execution.passed_steps,
                    failed_steps=execution.failed_steps,
                    updated_at=timezone.now(),
                )
                if not updated:
                    case_cancel_check()
                    continue
                execution.refresh_from_db()

                if execution.result == 'passed':
                    passed += 1
                elif execution.result == 'unverified':
                    unverified += 1
                elif execution.result == 'skipped':
                    skipped += 1
                else:
                    failed += 1

                send_execution_update(
                    execution.id, status=execution.status, progress=100,
                    message=execution.error_message if execution.status == 'error' else '执行完成',
                    report_path=execution.report_path,
                    finished_at=execution.finished_at,
                    result=execution.result,
                )

                logger.info(
                    'APP case execution completed: status=%s, result=%s',
                    execution.status,
                    execution.result,
                )

            except Exception as e:
                if isinstance(e, AppTestProcessTerminationError):
                    raise
                case_cancel_check()
                safe_error = _safe_app_error_message(e)
                logger.error(
                    'APP case execution failed (error_type=%s)', type(e).__name__
                )
                now = timezone.now()
                settled = AppTestExecution.objects.filter(
                    pk=execution.pk,
                    status='running',
                ).update(
                    status='error', result=None, error_message=safe_error,
                    finished_at=now, updated_at=now,
                )
                if not settled:
                    continue
                execution.refresh_from_db()
                failed += 1
                send_execution_update(
                    execution.id, status='error',
                    progress=execution.progress or 0,
                    message=safe_error,
                    finished_at=execution.finished_at,
                    result=None,
                )

        cancel_check()
        if suite:
            if failed:
                suite_result = 'failed'
            elif unverified or (passed and skipped):
                suite_result = 'unverified'
            elif passed:
                suite_result = 'passed'
            else:
                suite_result = 'skipped'
            settle_app_suite_if_idle(
                suite.pk,
                execution_ids=execution_ids,
                dispatch_id=dispatch_id,
                status='completed',
                result=suite_result,
                passed_count=passed,
                failed_count=failed,
                last_run_at=timezone.now(),
            )
            suite.refresh_from_db()
            logger.info('套件执行完成: 通过: %s, 失败: %s', passed, failed)
        else:
            logger.info('APP批量执行完成: 通过 %s, 失败 %s', passed, failed)

        # 定时任务通知和 durable schedule completion
        if scheduled_task_id:
            try:
                is_success = bool(passed) and failed == 0 and unverified == 0 and skipped == 0
                result = {
                    'status': suite.execution_status if suite else 'completed',
                    'result': suite.execution_result if suite else None,
                    'message': f'通过: {passed}, 失败: {failed}, 未验证: {unverified}, 跳过: {skipped}',
                }
                _record_scheduled_result(
                    scheduled_task_id,
                    is_success,
                    result,
                    scheduler_lease_token=scheduler_lease_token,
                )
                send_scheduled_task_notification(scheduled_task_id, success=is_success)
            except Exception as ne:
                logger.error(
                    "APP scheduled result update failed (error_type=%s)", type(ne).__name__
                )

    except Exception as e:
        safe_error = _safe_app_error_message(e)
        logger.error("APP execution batch failed (error_type=%s)", type(e).__name__)
        unsafe_termination = isinstance(e, AppTestProcessTerminationError)
        if unsafe_termination:
            os.chdir(original_worker_cwd)
            AppTestExecution.objects.filter(
                pk__in=[item.pk for item in executions], status='running'
            ).update(stop_requested=True, error_message=safe_error, updated_at=timezone.now())
        settled_ids = settle_app_execution_failure(
            [item.pk for item in executions], safe_error,
            include_running=worker_claimed and not unsafe_termination,
        )
        for settled in AppTestExecution.objects.filter(pk__in=settled_ids):
            send_execution_update(
                settled.pk,
                status=settled.status,
                progress=settled.progress or 0,
                message='任务已停止' if settled.status == 'stopped' else safe_error,
                finished_at=settled.finished_at,
                result=None,
            )
        if suite and settled_ids and not unsafe_termination:
            settled_statuses = list(
                AppTestExecution.objects.filter(pk__in=settled_ids)
                .values_list('status', flat=True)
            )
            cooperative_cancel = bool(settled_statuses) and all(
                status == 'stopped' for status in settled_statuses
            )
            suite_summary = summarize_app_suite_dispatch(
                suite.pk,
                execution_ids=[item.pk for item in executions],
                dispatch_id=dispatch_id,
            )
            settle_app_suite_if_idle(
                suite.pk,
                execution_ids=[item.pk for item in executions],
                dispatch_id=dispatch_id,
                status='completed' if cooperative_cancel else 'error',
                result=suite_summary['result'] if cooperative_cancel else None,
                passed_count=suite_summary['passed_count'],
                failed_count=suite_summary['failed_count'],
                last_run_at=timezone.now(),
            )
            suite.refresh_from_db()

        if scheduled_task_id and scheduler_lease_token and not unsafe_termination:
            try:
                _record_scheduled_result(
                    scheduled_task_id,
                    False,
                    {'status': 'error', 'message': safe_error},
                    scheduler_lease_token=scheduler_lease_token,
                )
            except Exception as schedule_error:
                logger.error(
                    'APP suite schedule failure update failed (error_type=%s)',
                    type(schedule_error).__name__,
                )
    finally:
        if device_heartbeat is not None:
            device_heartbeat.stop()
        if heartbeat is not None:
            heartbeat.stop()
        if release_device_lease and not unsafe_termination and device and user:
            should_release = bool(scheduled_task_id) or (
                worker_claimed and AppTestExecution.objects.filter(
                    pk__in=[item.pk for item in executions], stop_requested=True
                ).exists()
            )
            if should_release:
                try:
                    release_device_if_idle(device.pk, user, bound_generation)
                except Exception as e:
                    logger.error(
                        "APP device lease release failed (error_type=%s)",
                        type(e).__name__,
                    )


@shared_task
def execute_app_batch_task(
    dispatch_id,
    package_name=None,
    lease_generation=None,
    release_device_lease=False,
):
    """Run all durable rows for one batch in their persisted insertion order."""
    from .models import AppTestExecution

    execution_ids = list(
        AppTestExecution.objects.filter(task_id=dispatch_id)
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    if not execution_ids:
        return {'status': 'ignored'}
    return execute_app_suite_task.run(
        suite_id=None,
        execution_ids=execution_ids,
        package_name=package_name,
        lease_generation=lease_generation,
        release_device_lease=release_device_lease,
        dispatch_id=dispatch_id,
    )


@shared_task
def check_and_release_expired_devices():
    """
    检查并释放过期锁定的设备
    """
    from .device_pool import reap_expired_device_leases

    try:
        released_count = reap_expired_device_leases()
        logger.info(f"检查设备锁定完成，释放 {released_count} 个设备")

    except Exception as e:
        logger.error(
            "APP expired-device lease cleanup failed (error_type=%s)", type(e).__name__
        )

@shared_task
def reap_expired_device_leases_task():
    """Reclaim capability-pool leases whose heartbeat has expired."""
    from .device_pool import reap_expired_device_leases
    reclaimed = reap_expired_device_leases()
    logger.info('回收过期设备租约: %s', reclaimed)
    return reclaimed
