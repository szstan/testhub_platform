# -*- coding: utf-8 -*-
"""APP测试用例管理视图"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from rest_framework.filters import SearchFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction
from apps.quality_intelligence.services.redaction import redact_text
import logging
import re

from ..models import AppPackage, AppTestCase, AppDevice, AppTestExecution
from ..access import scope_test_cases
from ..device_pool import DeviceLeaseError
from ..execution_admission import DeviceExecutionBusy
from ..execution_dispatch import (
    enqueue_app_task,
    execution_user_matches,
    new_execution_dispatch_id,
    prepare_execution_device,
)
from ..serializers import AppPackageSerializer, AppTestCaseSerializer, AppTestExecutionSerializer, AppDeviceSerializer

logger = logging.getLogger(__name__)


class AppPagination(PageNumberPagination):
    """APP自动化模块通用分页"""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class AppPackageViewSet(viewsets.ModelViewSet):
    """APP应用包名管理 ViewSet"""
    queryset = AppPackage.objects.all()
    serializer_class = AppPackageSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    search_fields = ['name', 'package_name']
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class AppTestCaseViewSet(viewsets.ModelViewSet):
    """APP测试用例 ViewSet"""
    queryset = AppTestCase.objects.all()
    serializer_class = AppTestCaseSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['project', 'app_package']
    search_fields = ['name']
    
    def get_queryset(self):
        return scope_test_cases(super().get_queryset(), self.request.user)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        """执行测试用例"""
        test_case = self.get_object()
        if not execution_user_matches(request.user, request.data.get('expected_user_id')):
            return Response({'success': False, 'message': '登录用户已变化，请重新操作'},
                            status=status.HTTP_403_FORBIDDEN)
        device_id = request.data.get('device_id')
        package_name = request.data.get('package_name')
        auto_lock = request.data.get('auto_lock', False)
        if not isinstance(auto_lock, bool):
            return Response({'success': False, 'message': 'auto_lock 必须为布尔值'},
                            status=status.HTTP_400_BAD_REQUEST)

        if not device_id:
            return Response({
                'success': False,
                'message': '请选择执行设备'
            }, status=status.HTTP_400_BAD_REQUEST)
        if test_case.app_package and package_name and package_name != test_case.app_package.package_name:
            return Response({
                'success': False,
                'message': '执行包名必须与测试用例一致'
            }, status=status.HTTP_400_BAD_REQUEST)

        execution = None
        acquired_lease = False
        try:
            with transaction.atomic():
                device = AppDevice.objects.select_for_update().get(device_id=device_id)
                device, lease_generation, acquired_lease = prepare_execution_device(
                    device,
                    request.user,
                    lease_token=request.data.get('lease_token') or '',
                    auto_lock=auto_lock,
                )
                dispatch_id = new_execution_dispatch_id()
                execution = AppTestExecution.objects.create(
                    test_case=test_case,
                    device=device,
                    user=request.user,
                    status='pending',
                    task_id=dispatch_id,
                    device_lease_generation=lease_generation,
                    release_device_lease_on_stop=acquired_lease,
                )
        except AppDevice.DoesNotExist:
            return Response({'success': False, 'message': '设备不存在'},
                            status=status.HTTP_404_NOT_FOUND)
        except DeviceExecutionBusy as exc:
            return Response({'success': False, 'message': str(exc)},
                            status=status.HTTP_409_CONFLICT)
        except PermissionError as exc:
            return Response({'success': False, 'message': str(exc)},
                            status=status.HTTP_403_FORBIDDEN)
        except DeviceLeaseError as exc:
            return Response({'success': False, 'message': str(exc)},
                            status=status.HTTP_403_FORBIDDEN)

        from ..tasks import execute_app_test_task, settle_undispatched_app_executions
        try:
            enqueue_app_task(
                execute_app_test_task,
                dispatch_id=dispatch_id,
                args=(execution.pk,),
                kwargs={
                    'package_name': package_name,
                    'lease_generation': lease_generation,
                    'release_device_lease': acquired_lease,
                },
            )
        except Exception as exc:
            message = '无法提交执行任务，请检查任务队列'
            settle_undispatched_app_executions(
                [execution.pk],
                message,
                device_id=device.pk,
                lease_owner=request.user if acquired_lease else None,
                lease_generation=lease_generation,
                release_device_lease=acquired_lease,
            )
            logger.error('执行测试派发失败: %s', redact_text(str(exc), max_length=500))
            return Response({'success': False, 'message': message},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        execution.refresh_from_db()
        device.refresh_from_db()
        logger.info('测试已提交执行: execution_id=%s task_id=%s', execution.pk, dispatch_id)
        return Response({
            'success': True,
            'message': '测试已提交执行',
            'execution': AppTestExecutionSerializer(
                execution,
                context=self.get_serializer_context(),
            ).data,
            'device': AppDeviceSerializer(device).data,
        })

    @action(detail=False, methods=['post'], url_path='batch-execute')
    def batch_execute(self, request):
        """Queue one durable sequential batch of test cases."""
        if not execution_user_matches(request.user, request.data.get('expected_user_id')):
            return Response({'success': False, 'message': '登录用户已变化，请重新操作'},
                            status=status.HTTP_403_FORBIDDEN)
        case_ids = request.data.get('case_ids')
        device_id = request.data.get('device_id')
        package_name = request.data.get('package_name') or ''
        auto_lock = request.data.get('auto_lock', False)

        if not isinstance(auto_lock, bool):
            return Response({'success': False, 'message': 'auto_lock 必须为布尔值'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(case_ids, list) or not case_ids or len(case_ids) > 1000:
            return Response({'success': False, 'message': 'case_ids 必须包含 1 到 1000 个用例 ID'},
                            status=status.HTTP_400_BAD_REQUEST)
        if any(not isinstance(case_id, int) or isinstance(case_id, bool) or case_id < 1 for case_id in case_ids):
            return Response({'success': False, 'message': 'case_ids 中包含无效用例 ID'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not device_id:
            return Response({'success': False, 'message': '请选择执行设备'},
                            status=status.HTTP_400_BAD_REQUEST)

        if package_name and not re.fullmatch(
            r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', str(package_name)
        ):
            return Response({'success': False, 'message': '非法 Android package name'},
                            status=status.HTTP_400_BAD_REQUEST)

        cases = scope_test_cases(
            AppTestCase.objects.filter(pk__in=set(case_ids)).select_related('app_package'),
            request.user,
        )
        case_map = {case.pk: case for case in cases}
        if any(case_id not in case_map for case_id in case_ids):
            return Response({'success': False, 'message': '包含无权使用或不存在的测试用例'},
                            status=status.HTTP_403_FORBIDDEN)
        ordered_cases = [case_map[case_id] for case_id in case_ids]
        if package_name and any(
            case.app_package_id and case.app_package.package_name != package_name
            for case in ordered_cases
        ):
            return Response({'success': False, 'message': '执行包名必须与所有测试用例一致'},
                            status=status.HTTP_400_BAD_REQUEST)

        executions = []
        acquired_lease = False
        try:
            with transaction.atomic():
                device = AppDevice.objects.select_for_update().get(device_id=device_id)
                device, lease_generation, acquired_lease = prepare_execution_device(
                    device,
                    request.user,
                    lease_token=request.data.get('lease_token') or '',
                    auto_lock=auto_lock,
                )
                dispatch_id = new_execution_dispatch_id()
                executions = [
                    AppTestExecution.objects.create(
                        test_case=case,
                        device=device,
                        user=request.user,
                        status='pending',
                        task_id=dispatch_id,
                        device_lease_generation=lease_generation,
                        release_device_lease_on_stop=acquired_lease,
                    )
                    for case in ordered_cases
                ]
        except AppDevice.DoesNotExist:
            return Response({'success': False, 'message': '设备不存在'},
                            status=status.HTTP_404_NOT_FOUND)
        except DeviceExecutionBusy as exc:
            return Response({'success': False, 'message': str(exc)},
                            status=status.HTTP_409_CONFLICT)
        except PermissionError as exc:
            return Response({'success': False, 'message': str(exc)},
                            status=status.HTTP_403_FORBIDDEN)
        except DeviceLeaseError as exc:
            return Response({'success': False, 'message': str(exc)},
                            status=status.HTTP_403_FORBIDDEN)

        from ..tasks import execute_app_batch_task, settle_undispatched_app_executions
        execution_ids = [execution.pk for execution in executions]
        try:
            enqueue_app_task(
                execute_app_batch_task,
                dispatch_id=dispatch_id,
                args=(dispatch_id,),
                kwargs={
                    'package_name': package_name or None,
                    'lease_generation': lease_generation,
                    'release_device_lease': acquired_lease,
                },
            )
        except Exception as exc:
            message = '无法提交执行任务，请检查任务队列'
            settle_undispatched_app_executions(
                execution_ids,
                message,
                device_id=device.pk,
                lease_owner=request.user if acquired_lease else None,
                lease_generation=lease_generation,
                release_device_lease=acquired_lease,
            )
            logger.error('批量执行派发失败: %s', redact_text(str(exc), max_length=500))
            return Response({'success': False, 'message': message},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        device.refresh_from_db()
        return Response({
            'success': True,
            'message': f'已提交 {len(execution_ids)} 个用例顺序执行',
            'data': {
                'execution_ids': execution_ids,
                'test_case_count': len(execution_ids),
            },
            'device': AppDeviceSerializer(device).data,
        })
