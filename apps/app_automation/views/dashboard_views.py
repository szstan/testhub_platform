# -*- coding: utf-8 -*-
"""APP自动化仪表盘视图"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from django.db.models import Count, Q
from datetime import timedelta
import logging

from ..models import (
    AppDevice,
    AppElement,
    AppTestCase,
    AppTestExecution,
)
from ..access import scope_executions, scope_test_cases
from ..serializers import AppTestExecutionSerializer

logger = logging.getLogger(__name__)


class AppDashboardViewSet(viewsets.ViewSet):
    """APP自动化测试Dashboard"""
    permission_classes = [IsAuthenticated]
    
    @action(detail=False, methods=['get'])
    def statistics(self, request):
        """获取统计数据"""
        try:
            user = request.user
            # Devices are not directly assigned to projects. Only count devices
            # that appear in executions the current tenant can access.
            accessible_executions = scope_executions(
                AppTestExecution.objects.all(), user
            )
            accessible_devices = AppDevice.objects.filter(
                Q(executions__in=accessible_executions) | Q(locked_by=user)
            ).distinct()

            # 设备统计
            total_devices = accessible_devices.count()
            online_devices = accessible_devices.filter(status='online').count()
            locked_devices = accessible_devices.filter(status='locked').count()
            
            # 测试用例统计
            total_cases = scope_test_cases(AppTestCase.objects.all(), user).count()
            
            # 执行统计（最近30天）
            thirty_days_ago = timezone.now() - timedelta(days=30)
            
            executions = accessible_executions.filter(
                created_at__gte=thirty_days_ago
            )
            
            total_executions = executions.count()
            success_executions = executions.filter(status='success').count()
            failed_executions = executions.filter(status='failed').count()
            
            pass_rate = round((success_executions / total_executions * 100) if total_executions > 0 else 0, 2)
            
            # 最近执行记录
            recent_executions = accessible_executions.order_by('-created_at')[:10]
            recent_executions_data = AppTestExecutionSerializer(
                recent_executions,
                many=True,
                context={
                    'request': request,
                    # The dashboard only needs an availability indicator. Do
                    # not mint bearer tokens into an aggregate response.
                    'include_report_access_token': False,
                },
            ).data
            
            return Response({
                'success': True,
                'data': {
                    'devices': {
                        'total': total_devices,
                        'online': online_devices,
                        'locked': locked_devices,
                        'available': total_devices - locked_devices
                    },
                    'test_cases': {
                        'total': total_cases
                    },
                    'executions': {
                        'total': total_executions,
                        'success': success_executions,
                        'failed': failed_executions,
                        'pass_rate': pass_rate
                    },
                    'recent_executions': recent_executions_data
                }
            })
        except Exception as e:
            logger.error(f"获取统计数据失败: {str(e)}")
            return Response({
                'success': False,
                'message': f'获取统计数据失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
