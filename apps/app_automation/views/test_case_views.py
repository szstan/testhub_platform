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
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.http import Http404
from rest_framework.exceptions import PermissionDenied
from apps.quality_intelligence.services.redaction import redact_text
import logging
import re

from ..models import AppDataSet, AppPackage, AppProject, AppTestCase, AppDevice, AppTestExecution
from ..access import accessible_projects, is_app_admin, project_accessible, scope_test_cases
from ..device_pool import DeviceLeaseError
from ..execution_admission import DeviceExecutionBusy
from ..execution_dispatch import (
    enqueue_app_task,
    execution_user_matches,
    new_execution_dispatch_id,
    prepare_execution_device,
)
from ..serializers import AppPackageSerializer, AppTestCaseSerializer, AppTestExecutionSerializer, AppDeviceSerializer
from ..scene_execution import (
    SCENE_CASE_CHANGED_MESSAGE,
    ScenePreparationError,
    check_appium_prerequisites,
    validate_diagnostic_selection,
)

logger = logging.getLogger(__name__)


def _diagnostic_selection_error_response(exc):
    if str(exc) == SCENE_CASE_CHANGED_MESSAGE:
        return Response(
            {'success': False, 'message': SCENE_CASE_CHANGED_MESSAGE},
            status=status.HTTP_409_CONFLICT,
        )
    return Response(
        {'success': False, 'message': '步骤编号无效或超出用例范围'},
        status=status.HTTP_400_BAD_REQUEST,
    )


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

    def get_queryset(self):
        user = self.request.user
        queryset = super().get_queryset()
        if is_app_admin(user):
            return queryset
        projects = accessible_projects(user)
        project_members = AppProject.objects.filter(
            pk__in=projects.values('pk')
        ).values('members__pk')
        return queryset.filter(
            Q(created_by=user)
            | Q(created_by__isnull=True)
            | Q(created_by_id__in=projects.values('owner_id'))
            | Q(created_by_id__in=project_members)
        ).distinct()

    def _require_package_owner(self, package):
        if not is_app_admin(self.request.user) and package.created_by_id != self.request.user.pk:
            raise PermissionDenied('仅应用包创建者或管理员可以修改')

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        self._require_package_owner(serializer.instance)
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        package = self.get_object()
        self._require_package_owner(package)
        try:
            self.perform_destroy(package)
        except ProtectedError:
            return Response(
                {'detail': '应用包仍被测试用例或定时任务引用，不能删除'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


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

    @action(detail=True, methods=['get'], url_path='appium-prerequisites')
    def appium_prerequisites(self, request, pk=None):
        """Check saved Appium case configuration without accessing a device."""
        test_case = self.get_object()
        report = check_appium_prerequisites(
            test_case,
            user=request.user,
            engine='appium',
        )
        return Response({'success': True, 'data': report})

    def _get_locked_diagnostic_case(self, request, case_id):
        test_case = AppTestCase.objects.select_for_update().filter(pk=case_id).first()
        if test_case is None:
            raise Http404
        self.check_object_permissions(request, test_case)
        if not self.filter_queryset(self.get_queryset()).filter(pk=test_case.pk).exists():
            raise Http404
        return test_case

    @action(detail=True, methods=['post'], url_path='run-step')
    def run_step(self, request, pk=None):
        """Queue one saved root step for an authenticated diagnostic run."""
        test_case = self.get_object()
        expected_user_id = request.data.get('expected_user_id')
        if (
            expected_user_id is None
            or isinstance(expected_user_id, bool)
            or not isinstance(expected_user_id, (int, str))
            or not execution_user_matches(request.user, expected_user_id)
        ):
            return Response({'success': False, 'message': '登录用户已变化，请重新操作'},
                            status=status.HTTP_403_FORBIDDEN)

        step_index = request.data.get('step_index')
        expected_case_updated_at = request.data.get('expected_case_updated_at')
        case_id = test_case.pk
        try:
            # Reject already-stale editor revisions before entering device admission.
            validate_diagnostic_selection(
                test_case,
                step_index,
                expected_case_updated_at,
            )
        except ScenePreparationError as exc:
            return _diagnostic_selection_error_response(exc)

        device_id = request.data.get('device_id')
        auto_lock = request.data.get('auto_lock', False)
        lease_token = request.data.get('lease_token') or ''
        if not isinstance(device_id, str) or not device_id:
            return Response({'success': False, 'message': '请选择执行设备'},
                            status=status.HTTP_400_BAD_REQUEST)
        if type(auto_lock) is not bool:
            return Response({'success': False, 'message': 'auto_lock 必须为布尔值'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(lease_token, str):
            return Response({'success': False, 'message': '设备租约无效'},
                            status=status.HTTP_400_BAD_REQUEST)

        execution = None
        acquired_lease = False
        try:
            with transaction.atomic():
                # Execution endpoints take the device lock before their case FK lock.
                device = AppDevice.objects.select_for_update().get(device_id=device_id)
                test_case = self._get_locked_diagnostic_case(request, case_id)
                validate_diagnostic_selection(
                    test_case,
                    step_index,
                    expected_case_updated_at,
                )
                package_name = test_case.app_package.package_name if test_case.app_package_id else ''
                device, lease_generation, acquired_lease = prepare_execution_device(
                    device,
                    request.user,
                    lease_token=lease_token,
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
        except ScenePreparationError as exc:
            return _diagnostic_selection_error_response(exc)
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
                    'step_index': step_index,
                    'expected_case_updated_at': expected_case_updated_at,
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
            logger.error('单步诊断派发失败: %s', redact_text(str(exc), max_length=500))
            return Response({'success': False, 'message': message},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        execution.refresh_from_db()
        device.refresh_from_db()
        return Response({
            'success': True,
            'message': '单步诊断已提交',
            'execution': AppTestExecutionSerializer(
                execution,
                context=self.get_serializer_context(),
            ).data,
            'device': AppDeviceSerializer(device).data,
        })

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

    @action(detail=True, methods=['post'], url_path='execute-dataset')
    def execute_dataset(self, request, pk=None):
        """Queue selected immutable dataset rows as one sequential device batch."""
        test_case = self.get_object()
        if not execution_user_matches(request.user, request.data.get('expected_user_id')):
            return Response({'success': False, 'message': '登录用户已变化，请重新操作'},
                            status=status.HTTP_403_FORBIDDEN)
        if test_case.project_id is None or not project_accessible(request.user, test_case.project):
            return Response({'success': False, 'message': '无权访问用例所属项目'},
                            status=status.HTTP_403_FORBIDDEN)
        dataset_id = request.data.get('dataset_id')
        dataset_text = str(dataset_id)
        if (type(dataset_id) not in (int, str) or not dataset_text.isascii()
                or not dataset_text.isdigit() or len(dataset_text) > 18):
            return Response({'success': False, 'message': '请选择有效的数据集'},
                            status=status.HTTP_400_BAD_REQUEST)
        dataset = AppDataSet.objects.filter(pk=dataset_id, project_id=test_case.project_id).first()
        if dataset is None:
            return Response({'success': False, 'message': '数据集不存在或不属于用例项目'},
                            status=status.HTTP_404_NOT_FOUND)
        if not isinstance(dataset.rows, list) or not dataset.rows:
            return Response({'success': False, 'message': '数据集没有可执行的数据行'},
                            status=status.HTTP_400_BAD_REQUEST)
        mode = request.data.get('mode', 'single')
        if mode == 'all':
            if len(dataset.rows) > 1000:
                return Response({'success': False, 'message': '一次最多顺序执行 1000 行，请选择单行或拆分数据集'},
                                status=status.HTTP_400_BAD_REQUEST)
            indices = list(range(len(dataset.rows)))
        elif mode == 'single':
            row_index = request.data.get('row_index', 0)
            if type(row_index) is not int or not 0 <= row_index < len(dataset.rows):
                return Response({'success': False, 'message': '数据集行号无效'},
                                status=status.HTTP_400_BAD_REQUEST)
            indices = [row_index]
        else:
            return Response({'success': False, 'message': '请选择单行或全部数据'},
                            status=status.HTTP_400_BAD_REQUEST)
        device_id = request.data.get('device_id')
        package_name = request.data.get('package_name') or ''
        auto_lock = request.data.get('auto_lock', False)
        if not device_id or not isinstance(auto_lock, bool):
            return Response({'success': False, 'message': '请选择设备并提供有效的锁定方式'},
                            status=status.HTTP_400_BAD_REQUEST)
        if package_name and not re.fullmatch(
            r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', str(package_name)
        ):
            return Response({'success': False, 'message': '非法 Android package name'},
                            status=status.HTTP_400_BAD_REQUEST)
        if test_case.app_package and package_name and package_name != test_case.app_package.package_name:
            return Response({'success': False, 'message': '执行包名必须与测试用例一致'},
                            status=status.HTTP_400_BAD_REQUEST)
        executions = []
        acquired_lease = False
        try:
            with transaction.atomic():
                device = AppDevice.objects.select_for_update().get(device_id=device_id)
                device, generation, acquired_lease = prepare_execution_device(
                    device, request.user, lease_token=request.data.get('lease_token') or '',
                    auto_lock=auto_lock,
                )
                dispatch_id = new_execution_dispatch_id()
                executions = [AppTestExecution.objects.create(
                    test_case=test_case, device=device, user=request.user, status='pending',
                    task_id=dispatch_id, device_lease_generation=generation,
                    release_device_lease_on_stop=acquired_lease,
                    dataset=dataset, dataset_row_index=index,
                ) for index in indices]
        except AppDevice.DoesNotExist:
            return Response({'success': False, 'message': '设备不存在'}, status=status.HTTP_404_NOT_FOUND)
        except DeviceExecutionBusy:
            return Response({'success': False, 'message': '设备已有待执行或正在执行的任务'},
                            status=status.HTTP_409_CONFLICT)
        except (PermissionError, DeviceLeaseError):
            return Response({'success': False, 'message': '无权使用设备或设备租约无效'},
                            status=status.HTTP_403_FORBIDDEN)
        from ..tasks import execute_app_batch_task, settle_undispatched_app_executions
        execution_ids = [item.pk for item in executions]
        try:
            enqueue_app_task(
                execute_app_batch_task, dispatch_id=dispatch_id, args=(dispatch_id,),
                kwargs={'package_name': package_name or None, 'lease_generation': generation,
                        'release_device_lease': acquired_lease},
            )
        except Exception:
            settle_undispatched_app_executions(
                execution_ids, '无法提交执行任务，请检查任务队列', device_id=device.pk,
                lease_owner=request.user if acquired_lease else None, lease_generation=generation,
                release_device_lease=acquired_lease,
            )
            logger.error('数据集执行派发失败')
            return Response({'success': False, 'message': '无法提交执行任务，请检查任务队列'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({'success': True, 'message': f'已提交 {len(execution_ids)} 行数据顺序执行',
                         'data': {'execution_ids': execution_ids, 'row_count': len(execution_ids)}})

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
