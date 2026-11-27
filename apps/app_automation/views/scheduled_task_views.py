# -*- coding: utf-8 -*-
"""APP自动化定时任务视图"""
import json
import logging

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.db import transaction
from apps.quality_intelligence.services.redaction import redact_text

from .test_case_views import AppPagination
from ..models import (
    AppScheduledTask, AppNotificationLog,
    AppTestSuite, AppTestCase, AppDevice, AppTestExecution,
)
from ..device_pool import (
    DeviceLeaseError,
    acquire_specific_device,
    is_device_lease_active,
    release_device_execution_lease,
    validate_device_lease,
    assert_device_execution_lease,
)
from ..execution_admission import DeviceExecutionBusy, assert_device_execution_idle
from ..execution_dispatch import enqueue_app_task, new_execution_dispatch_id
from ..access import scope_project, scheduled_task_relation_errors
from ..serializers import (
    AppScheduledTaskSerializer,
    AppNotificationLogSerializer,
)

logger = logging.getLogger(__name__)


class AppScheduledTaskViewSet(viewsets.ModelViewSet):
    """APP定时任务视图集"""
    queryset = AppScheduledTask.objects.all()
    serializer_class = AppScheduledTaskSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['task_type', 'status', 'trigger_type', 'project']
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'next_run_time', 'last_run_time']
    ordering = ['-created_at']

    def get_queryset(self):
        return scope_project(super().get_queryset(), self.request.user, 'project')

    @action(detail=True, methods=['post'])
    def pause(self, request, pk=None):
        """暂停处于激活状态的任务，保留下次调度时间。"""
        task = self.get_object()

        if task.status == 'PAUSED':
            return Response(
                {'success': False, 'message': '任务已经是暂停状态'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if task.status != 'ACTIVE':
            return Response(
                {'success': False, 'message': '只有激活状态的任务可以暂停'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        task.status = 'PAUSED'
        task.save(update_fields=['status'])
        return Response({'success': True, 'message': '任务已暂停'})

    @action(detail=True, methods=['post'])
    def resume(self, request, pk=None):
        task = self.get_object()
        task.status = 'ACTIVE'
        task.next_run_time = task.calculate_next_run()
        task.save(update_fields=['status', 'next_run_time'])
        return Response({'success': True, 'message': '任务已恢复'})

    @action(detail=True, methods=['post'])
    def run_now(self, request, pk=None):
        """立即运行任务"""
        task = self.get_object()

        relation_errors = scheduled_task_relation_errors(
            user=request.user,
            project=task.project,
            device=task.device,
            app_package=task.app_package,
            test_suite=task.test_suite,
            test_case=task.test_case,
            task_type=task.task_type,
        )
        if relation_errors:
            return Response(
                {'success': False, 'message': '任务关联资源不一致', 'errors': relation_errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        executions = []
        suite_id = task.test_suite_id if task.task_type == 'TEST_SUITE' else None
        try:
            if not task.device:
                return Response(
                    {'success': False, 'message': '该任务未配置执行设备'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if task.task_type not in {'TEST_CASE', 'TEST_SUITE'}:
                return Response(
                    {'success': False, 'message': '不支持的任务类型'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            device = task.device
            release_device_lease = False
            lease_generation = None
            if is_device_lease_active(device):
                if device.locked_by_id != request.user.id:
                    return Response(
                        {'success': False, 'message': '设备已被其他用户锁定'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                try:
                    with transaction.atomic():
                        device = validate_device_lease(
                            device.pk, request.user, request.data.get('lease_token') or ''
                        )
                except DeviceLeaseError as exc:
                    return Response({'success': False, 'message': str(exc)}, status=status.HTTP_403_FORBIDDEN)
                lease_generation = device.lease_generation
            else:
                device, _lease_token = acquire_specific_device(device.pk, request.user)
                lease_generation = device.lease_generation
                release_device_lease = True

            suite_cases = list(task.test_suite.suite_cases.select_related('test_case').all()) if task.task_type == 'TEST_SUITE' else []
            if task.task_type == 'TEST_SUITE' and not suite_cases:
                if release_device_lease:
                    release_device_execution_lease(device.pk, request.user, lease_generation)
                return Response(
                    {'success': False, 'message': '测试套件没有用例'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            package_name = task.app_package.package_name if task.app_package else ''
            dispatch_id = new_execution_dispatch_id()
            with transaction.atomic():
                device = AppDevice.objects.select_for_update().get(pk=device.pk)
                assert_device_execution_lease(device.pk, request.user, lease_generation)
                assert_device_execution_idle(device.pk)
                task.last_run_time = timezone.now()
                task.total_runs += 1
                task.next_run_time = task.calculate_next_run()
                task.save()
                if task.task_type == 'TEST_SUITE':
                    executions = [AppTestExecution.objects.create(
                        test_case=sc.test_case,
                        test_suite=task.test_suite,
                        device=device,
                        user=request.user,
                        status='pending',
                        task_id=dispatch_id,
                        device_lease_generation=lease_generation,
                        release_device_lease_on_stop=release_device_lease,
                    ) for sc in suite_cases]
                    task.test_suite.execution_status = 'running'
                    task.test_suite.save(update_fields=['execution_status'])
                else:
                    executions = [AppTestExecution.objects.create(
                        test_case=task.test_case,
                        device=device,
                        user=request.user,
                        status='pending',
                        task_id=dispatch_id,
                        device_lease_generation=lease_generation,
                        release_device_lease_on_stop=release_device_lease,
                    )]

            from ..tasks import execute_app_suite_task, execute_app_test_task, settle_undispatched_app_executions
            if task.task_type == 'TEST_SUITE':
                enqueue_app_task(
                    execute_app_suite_task,
                    dispatch_id=dispatch_id,
                    kwargs={
                        'suite_id': task.test_suite_id,
                        'execution_ids': [item.pk for item in executions],
                        'package_name': package_name,
                        'scheduled_task_id': task.pk,
                        'lease_generation': lease_generation,
                        'device_id': device.pk,
                        'device_lease_owner_id': request.user.pk,
                        'release_device_lease': release_device_lease,
                        'dispatch_id': dispatch_id,
                    },
                )
                return Response({
                    'success': True,
                    'message': f'测试套件开始执行，共 {len(executions)} 个用例',
                    'data': {'task_id': dispatch_id, 'test_case_count': len(executions)},
                })

            enqueue_app_task(
                execute_app_test_task,
                dispatch_id=dispatch_id,
                args=(executions[0].pk,),
                kwargs={
                    'package_name': package_name,
                    'scheduled_task_id': task.pk,
                    'lease_generation': lease_generation,
                    'release_device_lease': release_device_lease,
                },
            )
            return Response({
                'success': True,
                'message': '测试用例开始执行',
                'data': {'task_id': dispatch_id},
            })
        except DeviceExecutionBusy as exc:
            return Response({'success': False, 'message': str(exc)}, status=status.HTTP_409_CONFLICT)
        except Exception as e:
            failure = '无法提交执行任务，请检查任务队列'
            try:
                from ..tasks import settle_undispatched_app_executions
                settle_undispatched_app_executions(
                    [item.pk for item in executions],
                    failure,
                    suite_id=suite_id,
                    device_id=device.pk if locals().get('device') else None,
                    lease_owner=request.user if locals().get('release_device_lease') else None,
                    lease_generation=locals().get('lease_generation'),
                    release_device_lease=bool(locals().get('release_device_lease')),
                )
            except Exception:  # noqa: BLE001 - retain the broker failure response
                logger.exception('settling manually scheduled APP executions failed')
            task.failed_runs += 1
            task.error_message = failure[:4000]
            task.last_result = {'status': 'failed', 'error': failure[:4000]}
            task.save(update_fields=['failed_runs', 'error_message', 'last_result', 'updated_at'])
            logger.error('执行定时任务失败: %s', redact_text(str(e), max_length=500))
            return Response(
                {'success': False, 'message': failure},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AppNotificationLogViewSet(viewsets.ReadOnlyModelViewSet):
    """APP通知日志视图集（只读）"""
    queryset = AppNotificationLog.objects.all()
    serializer_class = AppNotificationLogSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'notification_type']
    search_fields = ['task_name', 'notification_content']
    ordering_fields = ['created_at', 'sent_at']
    ordering = ['-created_at']

    def get_queryset(self):
        return scope_project(AppNotificationLog.objects.select_related('task'), self.request.user, 'task__project').order_by('-created_at')

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        log = self.get_object()
        if log.status == 'failed':
            log.retry_count += 1
            log.is_retried = True
            log.save(update_fields=['retry_count', 'is_retried'])
            return Response({'success': True, 'message': '通知已加入重试队列'})
        return Response({'success': False, 'message': '只能重试失败的通知'},
                        status=status.HTTP_400_BAD_REQUEST)
