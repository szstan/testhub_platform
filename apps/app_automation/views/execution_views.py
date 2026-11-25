# -*- coding: utf-8 -*-
"""APP测试执行管理视图"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.settings import api_settings
from rest_framework.filters import SearchFilter
from django.db import transaction
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.http import FileResponse, Http404, HttpResponse
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.views.decorators.csrf import csrf_exempt
from apps.quality_intelligence.services.redaction import redact_text
import logging
import os

from ..models import AppTestExecution
from ..access import scope_executions
from ..serializers import AppTestExecutionSerializer
from ..tasks import send_execution_update
from apps.app_automation.views.test_case_views import AppPagination

logger = logging.getLogger(__name__)


def _is_asgi_http_request(request):
    """Return whether the Django request was created by the ASGI handler."""
    django_request = getattr(request, '_request', request)
    scope = getattr(django_request, 'scope', None)
    return isinstance(scope, dict) and scope.get('type') == 'http'


class AppTestExecutionViewSet(viewsets.ModelViewSet):
    """APP测试执行记录 ViewSet"""
    queryset = AppTestExecution.objects.all()
    serializer_class = AppTestExecutionSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'head', 'options']
    filterset_fields = ['status', 'test_case', 'device']
    search_fields = ['test_case__name', 'device__name', 'device__device_id', 'user__username']
    pagination_class = AppPagination
    
    def get_queryset(self):
        queryset = scope_executions(super().get_queryset(), self.request.user)

        # 支持 test_suite__isnull 过滤，用于区分单独执行和套件执行
        suite_isnull = self.request.query_params.get('test_suite__isnull')
        if suite_isnull is not None:
            queryset = queryset.filter(test_suite__isnull=(suite_isnull.lower() in ('true', '1')))

        # 支持按项目间接过滤（通过 test_case.project）
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(test_case__project_id=project_id)

        search_value = (self.request.query_params.get('search') or '').strip()
        if not search_value:
            return queryset
        return queryset.filter(
            Q(test_case__name__icontains=search_value) |
            Q(device__name__icontains=search_value) |
            Q(device__device_id__icontains=search_value) |
            Q(user__username__icontains=search_value)
        )
    
    def create(self, request, *args, **kwargs):
        return Response({'detail': '请通过测试用例执行入口创建执行记录'},
                        status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(detail=False, methods=['get'])
    def ws_status(self, request):
        """检查 WebSocket 是否可用"""
        try:
            from channels.layers import get_channel_layer

            channel_layer = get_channel_layer()
            is_asgi = _is_asgi_http_request(request)
            return Response({
                'websocket': bool(channel_layer) and is_asgi,
            })
        except Exception:
            return Response({'websocket': False})

    @action(detail=True, methods=['post'])
    def stop(self, request, pk=None):
        """Request cooperative stop without freeing a device under active I/O."""
        execution = self.get_object()
        try:
            with transaction.atomic():
                if execution.device_id:
                    from ..models import AppDevice
                    AppDevice.objects.select_for_update().filter(pk=execution.device_id).first()
                current = AppTestExecution.objects.select_for_update().get(pk=execution.pk)
                if current.status not in ['pending', 'running']:
                    return Response({
                        'success': False,
                        'message': '只能停止待执行或执行中的任务'
                    }, status=status.HTTP_400_BAD_REQUEST)

                rows = AppTestExecution.objects.select_for_update().filter(
                    pk=current.pk,
                    status__in=['pending', 'running'],
                )
                if current.task_id:
                    rows = AppTestExecution.objects.select_for_update().filter(
                        task_id=current.task_id,
                        device_id=current.device_id,
                        user_id=current.user_id,
                        status__in=['pending', 'running'],
                    )
                active_rows = list(rows.order_by('pk'))
                active_ids = {item.pk for item in active_rows}
                authorized_ids = set(scope_executions(
                    AppTestExecution.objects.filter(pk__in=active_ids), request.user
                ).values_list('pk', flat=True))
                if authorized_ids != active_ids:
                    return Response({
                        'success': False,
                        'message': '无权停止该批次中的全部执行记录',
                    }, status=status.HTTP_403_FORBIDDEN)

                suite_execution_ids = set(active_ids)
                if current.test_suite_id:
                    suite_rows = AppTestExecution.objects.filter(
                        test_suite_id=current.test_suite_id,
                    )
                    if current.task_id:
                        suite_rows = suite_rows.filter(task_id=current.task_id)
                    else:
                        suite_rows = suite_rows.filter(pk=current.pk)
                    suite_execution_ids = set(suite_rows.values_list('pk', flat=True))
                    suite_authorized_ids = set(scope_executions(
                        AppTestExecution.objects.filter(pk__in=suite_execution_ids), request.user
                    ).values_list('pk', flat=True))
                    if suite_authorized_ids != suite_execution_ids:
                        return Response({
                            'success': False,
                            'message': '无权停止该批次中的全部执行记录',
                        }, status=status.HTTP_403_FORBIDDEN)

                running_rows = [item for item in active_rows if item.status == 'running']
                pending_rows = [item for item in active_rows if item.status == 'pending']
                now = timezone.now()

                for item in pending_rows:
                    item.status = 'stopped'
                    item.finished_at = now
                    item.save(update_fields=['status', 'finished_at', 'updated_at'])
                for item in running_rows:
                    item.stop_requested = True
                    item.save(update_fields=['stop_requested', 'updated_at'])

                if current.test_suite_id and pending_rows and not running_rows:
                    from ..execution_admission import (
                        settle_app_suite_if_idle,
                        summarize_app_suite_dispatch,
                    )
                    suite_summary = summarize_app_suite_dispatch(
                        current.test_suite_id,
                        execution_ids=suite_execution_ids,
                        dispatch_id=current.task_id,
                    )
                    settle_app_suite_if_idle(
                        current.test_suite_id,
                        execution_ids=suite_execution_ids,
                        dispatch_id=current.task_id,
                        status='completed',
                        result=suite_summary['result'],
                        passed_count=suite_summary['passed_count'],
                        failed_count=suite_summary['failed_count'],
                        last_run_at=now,
                    )

                if (
                    not running_rows
                    and current.release_device_lease_on_stop
                    and current.device_id
                    and current.user_id
                ):
                    from ..execution_admission import release_device_if_idle
                    release_device_if_idle(
                        current.device_id,
                        current.user,
                        current.device_lease_generation,
                    )

            if current.task_id:
                try:
                    from celery import current_app
                    current_app.control.revoke(current.task_id, terminate=False)
                    logger.info('Celery执行已撤销/请求协作停止: task_id=%s', current.task_id)
                except Exception as exc:
                    # The database cancellation is authoritative even if the broker is unavailable.
                    logger.warning('撤销队列任务失败: %s', redact_text(str(exc), max_length=500))

            for item in pending_rows:
                send_execution_update(
                    item.pk,
                    status='stopped',
                    progress=item.progress,
                    message='任务已停止',
                    report_path=item.report_path,
                    finished_at=item.finished_at,
                )
            for item in running_rows:
                send_execution_update(
                    item.pk,
                    status='running',
                    progress=item.progress,
                    message='正在停止当前步骤',
                    report_path=item.report_path,
                )

            return Response({
                'success': True,
                'message': '停止请求已提交' if running_rows else '任务已停止',
                'stopping': bool(running_rows),
            })
        except Exception as e:
            logger.error('停止任务失败: %s', redact_text(str(e), max_length=500))
            return Response({
                'success': False,
                'message': '停止任务失败，请稍后重试'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


REPORT_TOKEN_MAX_AGE = 300


def _request_user(request):
    """Resolve session/DRF authentication for this non-APIView endpoint."""
    forced_user = getattr(request, '_force_auth_user', None)
    if forced_user is not None:
        return forced_user

    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return user

    # URL-token sharing is anonymous by design, but a real JWT/token-auth
    # request must still be resolved before deciding whether project access is
    # allowed. The report view is a plain Django function, so DRF does not do
    # this automatically as it would for a ViewSet.
    if request.META.get('HTTP_AUTHORIZATION'):
        drf_request = Request(request)
        for authenticator_class in api_settings.DEFAULT_AUTHENTICATION_CLASSES:
            try:
                authenticator = authenticator_class()
                result = authenticator.authenticate(drf_request)
            except Exception:
                continue
            if result:
                return result[0]
    return None


def _execution_is_accessible(request, execution):
    """Return whether the authenticated user may read this execution."""
    user = _request_user(request)
    return scope_executions(
        AppTestExecution.objects.filter(pk=execution.pk), user
    ).exists()


def _valid_report_token(execution_id, access_token):
    if not access_token:
        return False
    try:
        value = TimestampSigner().unsign(access_token, max_age=REPORT_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return str(value) == str(execution_id)


@csrf_exempt
def serve_report_file(request, execution_id, access_token='', file_path=''):
    """Serve an APP report to an authorized user or explicit short-lived share.

    A valid signed token is intentionally sufficient for anonymous, short-lived
    sharing. Authenticated users still need access to the execution, so a
    token copied from another tenant cannot bypass project authorization.
    """
    try:
        execution = AppTestExecution.objects.get(id=execution_id)
    except AppTestExecution.DoesNotExist:
        raise Http404("执行记录不存在")

    token_valid = _valid_report_token(execution_id, access_token)
    user = _request_user(request)
    authenticated = bool(user and getattr(user, 'is_authenticated', False))
    authorized = _execution_is_accessible(request, execution) if authenticated else False

    # Anonymous access is an explicit share flow and requires a valid token.
    # Authenticated access is scoped by the same project/user rules as the API;
    # a supplied token must also be valid, so malformed shares are rejected.
    if authenticated:
        allowed = authorized and (not access_token or token_valid)
    else:
        allowed = token_valid
    if not allowed:
        return HttpResponse('无权访问报告或报告访问令牌无效', status=403)
    if not execution.report_path:
        raise Http404("报告路径不存在")

    if not file_path:
        file_path = 'index.html'

    report_dir = execution.report_path
    full_path = os.path.join(report_dir, file_path)

    report_dir_abs = os.path.realpath(report_dir)
    full_path_abs = os.path.realpath(full_path)
    try:
        inside_report = os.path.commonpath([report_dir_abs, full_path_abs]) == report_dir_abs
    except ValueError:
        inside_report = False
    if not inside_report:
        raise Http404("无效的文件路径")

    if not os.path.exists(full_path_abs) or not os.path.isfile(full_path_abs):
        raise Http404("文件不存在")

    return FileResponse(open(full_path_abs, 'rb'), content_type=_get_content_type(file_path))


def _get_content_type(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    content_types = {
        '.html': 'text/html',
        '.js': 'application/javascript',
        '.css': 'text/css',
        '.json': 'application/json',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.svg': 'image/svg+xml',
        '.ico': 'image/x-icon',
        '.woff': 'font/woff',
        '.woff2': 'font/woff2',
        '.ttf': 'font/ttf',
        '.eot': 'application/vnd.ms-fontobject',
        '.txt': 'text/plain',
    }
    return content_types.get(ext, 'application/octet-stream')
