# -*- coding: utf-8 -*-
"""APP设备管理视图"""
import subprocess
import base64
import ipaddress
import os
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from apps.quality_intelligence.services.redaction import redact_text
import logging

from .test_case_views import AppPagination
from ..models import AppDevice, AppTestExecution
from ..access import is_app_admin
from ..serializers import AppDeviceSerializer
from ..managers.device_manager import DeviceManager
from ..device_pool import (
    DeviceLeaseError,
    acquire_device,
    heartbeat_device,
    release_device,
    reap_expired_device_leases,
    is_device_lease_active,
)

logger = logging.getLogger(__name__)


class _PendingDeviceActionConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = '设备存在待恢复的设备动作，请先通过显式恢复操作处理'
    default_code = 'pending_device_action'


def _device_has_pending_action(device):
    return device.device_action_pending or AppTestExecution.objects.filter(
        device_id=device.pk,
        device_action_pending=True,
    ).exists()


def _expected_user_id_mismatch(request):
    return (
        'expected_user_id' in request.data
        and str(request.data.get('expected_user_id')) != str(request.user.pk)
    )


def _pending_action_blocks_update(device, changes):
    connection_identity_fields = ('device_id', 'connection_type', 'ip_address', 'port')
    return (
        any(
            field in changes and changes[field] != getattr(device, field)
            for field in connection_identity_fields
        )
        or ('status' in changes and changes['status'] != device.status)
        or changes.get('maintenance_mode') is False
    )


def _upsert_discovered_device(device_id, defaults):
    """Refresh discovery fields without overwriting a live worker lease."""
    with transaction.atomic():
        device, created = AppDevice.objects.select_for_update().get_or_create(
            device_id=device_id,
            defaults=defaults,
        )
        if created:
            return device
        for field, value in defaults.items():
            setattr(device, field, value)
        if is_device_lease_active(device):
            device.status = 'locked'
        device.save(update_fields=[*defaults.keys(), 'status', 'updated_at'])
        return device


def _remote_device_allowed(ip_address):
    try:
        address = ipaddress.ip_address(ip_address)
    except ValueError:
        return False
    cidrs = getattr(settings, 'APP_DEVICE_ALLOWED_CIDRS', []) or []
    return any(address in ipaddress.ip_network(cidr) for cidr in cidrs)


def get_adb_path() -> str:
    """Prefer an explicit UI setting, then the deployed APP_ADB_PATH."""
    configured_path = ''
    try:
        from ..models import AppTestConfig
        config = AppTestConfig.objects.first()
        configured_path = (config.adb_path or '').strip() if config else ''
    except Exception as e:
        logger.warning("获取 ADB 配置失败，使用环境配置或默认路径: %s", redact_text(str(e), max_length=300))

    if configured_path and configured_path.lower() not in {'adb', 'adb.exe'}:
        return configured_path
    return os.environ.get('APP_ADB_PATH', '').strip() or configured_path or 'adb'


def _adb_screenshot_failure(stderr):
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', errors='replace')
    detail = ' '.join(''.join(char if char.isprintable() else ' ' for char in str(stderr or '')).split())
    lowered = detail.lower()
    if 'unauthorized' in lowered:
        message = '设备未授权，请在设备上允许 USB 调试授权后重试'
    elif 'offline' in lowered:
        message = '设备已离线，请在设备管理中检查连接并重新连接'
    elif 'no devices' in lowered or ('device' in lowered and 'not found' in lowered):
        message = 'ADB 未找到该设备，请在设备管理中刷新或重新连接'
    elif 'cannot connect' in lowered or 'cannot connect to daemon' in lowered:
        message = '无法连接 ADB 服务，请检查后端运行环境及设备连接'
    else:
        message = 'ADB 截图命令失败，请检查设备连接和后端日志'
    return message, redact_text(detail, max_length=500)


class AppDeviceViewSet(viewsets.ModelViewSet):
    """APP设备管理 ViewSet"""
    queryset = AppDevice.objects.all()
    serializer_class = AppDeviceSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['status', 'connection_type']
    search_fields = ['device_id', 'name']

    def get_queryset(self):
        queryset = super().get_queryset()
        if is_app_admin(self.request.user):
            return queryset
        # Available devices form a shared pool; a device locked by another
        # user is hidden from list/retrieve and all detail actions.
        return queryset.filter(
            Q(locked_by__isnull=True) | Q(locked_by=self.request.user)
        )

    def _require_admin(self, request):
        if not is_app_admin(request.user):
            return Response({'detail': '仅管理员可以执行设备管理操作'}, status=status.HTTP_403_FORBIDDEN)
        return None

    def perform_create(self, serializer):
        if not is_app_admin(self.request.user):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('仅管理员可以创建设备')
        serializer.save()

    def perform_update(self, serializer):
        if not is_app_admin(self.request.user):
            raise PermissionDenied('仅管理员可以修改设备')

        try:
            with transaction.atomic():
                device = AppDevice.objects.select_for_update().get(pk=serializer.instance.pk)
                changes = serializer.validated_data
                if (
                    changes.get('maintenance_mode') is False
                    and _expected_user_id_mismatch(self.request)
                ):
                    raise PermissionDenied('当前账号已变化，请刷新后重试')
                if _device_has_pending_action(device) and _pending_action_blocks_update(device, changes):
                    raise _PendingDeviceActionConflict()
                serializer.instance = device
                serializer.save()
        except AppDevice.DoesNotExist:
            raise NotFound('设备不存在')

    def perform_destroy(self, instance):
        if not is_app_admin(self.request.user):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('仅管理员可以删除设备')

        try:
            with transaction.atomic():
                device = AppDevice.objects.select_for_update().get(pk=instance.pk)
                if _device_has_pending_action(device):
                    raise _PendingDeviceActionConflict()
                device.delete()
        except AppDevice.DoesNotExist:
            raise NotFound('设备不存在')

    @action(detail=False, methods=['get'])
    def discover(self, request):
        """发现ADB设备"""
        denial = self._require_admin(request)
        if denial:
            return denial
        try:
            adb_path = get_adb_path()
            logger.info(f"使用 ADB 路径: {adb_path}")
            
            manager = DeviceManager(adb_path=adb_path)
            devices_info = manager.list_devices()
            
            # 更新或创建设备记录
            db_devices = []
            for device_info in devices_info:
                # 判断连接类型和 IP 地址
                device_id = device_info['device_id']
                if ':' in device_id:
                    # 远程设备（IP:端口格式）
                    connection_type = 'remote_emulator'
                    ip_address = device_info.get('ip_address') or ''
                elif device_id.startswith('emulator-'):
                    # 本地模拟器 - 使用 localhost
                    connection_type = 'emulator'
                    ip_address = '127.0.0.1'
                else:
                    # USB 连接的真机
                    connection_type = 'usb'
                    ip_address = device_info.get('ip_address') or ''
                
                device = _upsert_discovered_device(
                    device_info['device_id'],
                    {
                        'name': device_info.get('name') or '',
                        'status': device_info.get('status') or 'offline',
                        'android_version': device_info.get('android_version') or '',
                        'ip_address': ip_address,
                        'port': device_info.get('port') or 5555,
                        'connection_type': connection_type,
                        'health_status': 'healthy' if (device_info.get('status') or '').lower() in {'online', 'available'} else 'unhealthy',
                        'last_seen_at': timezone.now(),
                        'capabilities': {
                            'platform': 'android',
                            'android_version': device_info.get('android_version') or '',
                            'connection_type': connection_type,
                        },
                    },
                )
                db_devices.append(device)
            
            # 返回序列化后的数据库对象
            return Response({
                'success': True,
                'message': f'发现 {len(db_devices)} 个设备',
                'devices': AppDeviceSerializer(db_devices, many=True).data
            })
        except Exception as e:
            logger.error(f"发现设备失败: {str(e)}")
            return Response({
                'success': False,
                'message': f'发现设备失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['post'])
    def lock(self, request, pk=None):
        """锁定设备"""
        try:
            with transaction.atomic():
                # Scope before locking so a regular user cannot lock another
                # user's lease through a direct primary-key request.
                device = self.get_queryset().select_for_update().get(pk=pk)
                if device.status == 'locked':
                    return Response({
                        'success': False,
                        'message': '设备已被锁定'
                    }, status=status.HTTP_400_BAD_REQUEST)
                try:
                    device.lock(request.user)
                except DeviceLeaseError as exc:
                    return Response({'success': False, 'message': str(exc)}, status=status.HTTP_409_CONFLICT)
        except AppDevice.DoesNotExist:
            return Response({'detail': '设备不存在或无权操作'}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            'success': True,
            'message': '设备锁定成功',
            'device': AppDeviceSerializer(device).data
        })
    
    @action(detail=True, methods=['post'])
    def unlock(self, request, pk=None):
        """释放设备"""
        device = self.get_object()
        
        if device.locked_by and device.locked_by != request.user:
            return Response({
                'success': False,
                'message': '无权释放他人锁定的设备'
            }, status=status.HTTP_403_FORBIDDEN)
        
        if not device.unlock():
            return Response({'success': False, 'message': '设备租约已变更，请刷新后重试'},
                            status=status.HTTP_409_CONFLICT)

        return Response({
            'success': True,
            'message': '设备释放成功',
            'device': AppDeviceSerializer(device).data
        })
    
    @action(detail=True, methods=['post'])
    def disconnect(self, request, pk=None):
        """断开远程设备连接"""
        device = self.get_object()
        if not is_app_admin(request.user) and device.locked_by_id != request.user.id:
            return Response({'success': False, 'message': '无权断开该设备'},
                            status=status.HTTP_403_FORBIDDEN)

        # 只有远程设备可以断开
        if device.connection_type not in ['remote', 'remote_emulator']:
            return Response({
                'success': False,
                'message': '只能断开远程设备的连接'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            adb_path = get_adb_path()
            manager = DeviceManager(adb_path=adb_path)
            success = manager.disconnect_device(f'{device.ip_address}:{device.port}')
            
            if not success:
                return Response({
                    'success': False,
                    'message': '断开设备失败'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            # 更新设备状态为离线
            device.status = 'offline'
            device.save()
            
            return Response({
                'success': True,
                'message': f'设备 {device.name or device.device_id} 已断开连接',
                'device': AppDeviceSerializer(device).data
            })
            
        except Exception as e:
            return Response({
                'success': False,
                'message': f'断开设备失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['post'])
    def connect(self, request):
        """连接远程设备"""
        denial = self._require_admin(request)
        if denial:
            return denial
        try:
            ip_address = request.data.get('ip_address')
            port = request.data.get('port', 5555)
            
            if not ip_address:
                return Response({
                    'success': False,
                    'message': '请提供设备IP地址'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            if not _remote_device_allowed(ip_address):
                return Response({
                    'success': False,
                    'message': '目标设备地址不在允许的设备网段内'
                }, status=status.HTTP_403_FORBIDDEN)

            adb_path = get_adb_path()
            manager = DeviceManager(adb_path=adb_path)
            device_info = manager.connect_device(ip_address, port)

            # 创建或更新设备记录
            device = _upsert_discovered_device(
                device_info['device_id'],
                {
                    'name': device_info.get('name') or '',
                    'status': 'online',
                    'android_version': device_info.get('android_version', ''),
                    'ip_address': ip_address,
                    'port': port,
                    'connection_type': 'remote_emulator',
                },
            )
            
            return Response({
                'success': True,
                'message': '设备连接成功',
                'device': AppDeviceSerializer(device).data
            })
        except Exception as e:
            logger.error(f"连接设备失败: {str(e)}")
            return Response({
                'success': False,
                'message': f'连接设备失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['post'])
    def acquire(self, request):
        """Acquire a capability-matched lease without changing network policy."""
        capabilities = request.data.get('capabilities') or {}
        try:
            device, lease_token = acquire_device(
                request.user,
                capabilities=capabilities,
                lease_seconds=request.data.get('lease_seconds'),
            )
        except (DeviceLeaseError, ValueError) as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({
            'device': AppDeviceSerializer(device).data,
            'lease_token': lease_token,
            'lease_expires_at': device.lease_expires_at,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def release(self, request, pk=None):
        device = self.get_object()
        try:
            device = release_device(
                device.pk,
                request.user,
                request.data.get('lease_token', ''),
                force=is_app_admin(request.user),
            )
        except DeviceLeaseError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        return Response({'device': AppDeviceSerializer(device).data})

    @action(detail=True, methods=['post'])
    def heartbeat(self, request, pk=None):
        # Resolve through the scoped queryset before touching the lease store.
        try:
            self.get_queryset().get(pk=pk)
        except AppDevice.DoesNotExist:
            return Response({'detail': '设备不存在或无权操作'}, status=status.HTTP_404_NOT_FOUND)
        try:
            device = heartbeat_device(
                pk,
                request.user,
                request.data.get('lease_token', ''),
                lease_seconds=request.data.get('lease_seconds'),
            )
        except DeviceLeaseError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        return Response({'device': AppDeviceSerializer(device).data})

    @action(detail=True, methods=['get'])
    def health(self, request, pk=None):
        device = self.get_object()
        return Response({
            'device_id': device.device_id,
            'status': device.status,
            'health_status': device.health_status,
            'maintenance_mode': device.maintenance_mode,
            'last_seen_at': device.last_seen_at,
            'last_heartbeat_at': device.last_heartbeat_at,
            'lease_expires_at': device.lease_expires_at,
        })

    @action(detail=False, methods=['post'], url_path='reap-expired')
    def reap_expired(self, request):
        denial = self._require_admin(request)
        if denial:
            return denial
        return Response({'reclaimed': reap_expired_device_leases()})

    @action(detail=True, methods=['post'], url_path='recover-pending-action')
    def recover_pending_action(self, request, pk=None):
        denial = self._require_admin(request)
        if denial:
            return denial
        if _expected_user_id_mismatch(request):
            return Response(
                {'detail': '当前账号已变化，请刷新后重试'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if request.data.get('confirm_no_active_worker') is not True:
            return Response(
                {'detail': '恢复前必须确认设备上没有活动工作进程'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                device = AppDevice.objects.select_for_update().get(pk=pk)
                executions = list(
                    AppTestExecution.objects.select_for_update()
                    .filter(device_id=device.pk)
                    .order_by('pk')
                )
                pending = [
                    execution for execution in executions
                    if execution.device_action_pending
                ]
                device_action_pending = device.device_action_pending
                if not pending and not device_action_pending:
                    return Response(
                        {'detail': '设备没有待恢复的动作标记'},
                        status=status.HTTP_409_CONFLICT,
                    )
                active = [
                    execution for execution in executions
                    if execution.status in ('pending', 'running')
                ]
                if any(
                    execution.status == 'running' and not execution.stop_requested
                    for execution in active
                ):
                    return Response(
                        {'detail': '仍有未请求停止的运行中执行，不能恢复'},
                        status=status.HTTP_409_CONFLICT,
                    )

                device.maintenance_mode = True
                device.health_status = 'unknown'
                device.device_action_pending = False
                device.save(update_fields=[
                    'maintenance_mode', 'health_status', 'device_action_pending', 'updated_at'
                ])
                now = timezone.now()
                for execution in active:
                    execution.status = 'stopped' if execution.stop_requested else 'error'
                    execution.result = None
                    execution.error_message = '管理员确认工作进程已停止；设备仍处于维护隔离状态'
                    execution.finished_at = now
                    execution.save(update_fields=[
                        'status', 'result', 'error_message', 'finished_at', 'updated_at'
                    ])
                AppTestExecution.objects.filter(
                    pk__in=[execution.pk for execution in pending]
                ).update(device_action_pending=False, updated_at=now)
        except AppDevice.DoesNotExist:
            return Response({'detail': '设备不存在'}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            'recovered_executions': len(pending),
            'recovered_device_action': device_action_pending,
            'maintenance_mode': True,
            'device_lease_preserved': True,
        })

    @action(detail=True, methods=['post'])
    def screenshot(self, request, pk=None):
        """
        获取设备实时截图
        
        功能：
        1. 使用 adb screencap 获取设备截图
        2. 转换为 Base64
        3. 返回 data URL 格式
        """
        device = self.get_object()
        if not is_app_admin(request.user) and device.locked_by_id != request.user.id:
            return Response({'code': 403, 'msg': '无权访问该设备', 'success': False},
                            status=status.HTTP_403_FORBIDDEN)
        
        if device.status == 'offline':

            return Response({
                'code': 400,
                'msg': '设备离线，无法截图',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            adb_path = get_adb_path()
            
            # 使用 adb screencap 命令截图
            result = subprocess.run(
                [adb_path, '-s', device.device_id, 'exec-out', 'screencap', '-p'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=10
            )
            
            if not result.stdout:
                logger.warning("设备 %s 截图失败: ADB 未返回截图数据", device.device_id)
                return Response({
                    'code': 500,
                    'msg': '截图失败：无返回数据',
                    'success': False
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            # 转换为 Base64
            image_base64 = base64.b64encode(result.stdout).decode('utf-8')
            
            logger.info(f"设备 {device.device_id} 截图成功")
            
            return Response({
                'code': 0,
                'msg': '截图成功',
                'success': True,
                'data': {
                    'filename': f"device_{device.id}_{int(timezone.now().timestamp())}.png",
                    'content': f"data:image/png;base64,{image_base64}",
                    'device_id': device.device_id,
                    'timestamp': int(timezone.now().timestamp())
                }
            })
            
        except subprocess.TimeoutExpired:
            logger.error(f"设备 {device.device_id} 截图超时")
            return Response({
                'code': 500,
                'msg': '截图超时，请检查设备连接',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except FileNotFoundError:
            logger.error("设备 %s 截图失败: 找不到 ADB 可执行文件", device.device_id)
            return Response({
                'code': 500,
                'msg': '找不到 ADB，请检查 APP 自动化配置或 APP_ADB_PATH',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except PermissionError:
            logger.error("设备 %s 截图失败: ADB 可执行文件无权限", device.device_id)
            return Response({
                'code': 500,
                'msg': 'ADB 无执行权限，请检查后端运行用户及文件权限',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except subprocess.CalledProcessError as e:
            message, detail = _adb_screenshot_failure(e.stderr)
            logger.error("设备 %s 截图失败: ADB 退出码 %s，错误: %s", device.device_id, e.returncode, detail or '无')
            return Response({
                'code': 500,
                'msg': message,
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            logger.error("设备 %s 截图失败: %s", device.device_id, redact_text(str(e), max_length=500))
            return Response({
                'code': 500,
                'msg': '截图失败，请检查设备连接和后端日志',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
