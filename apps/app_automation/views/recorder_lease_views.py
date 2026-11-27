"""Short-lived, token-fenced leases for explicitly enabled APP recording."""

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..access import device_action_allowed
from ..device_pool import DeviceLeaseError, acquire_specific_device, release_device, validate_device_lease
from ..execution_admission import DeviceExecutionBusy, assert_device_execution_idle
from ..execution_dispatch import execution_user_matches
from ..models import AppDevice
from ..serializers import AppDeviceSerializer


RECORDER_LEASE_SECONDS = 90


def _identity_valid(request):
    expected = request.data.get('expected_user_id')
    return expected is not None and execution_user_matches(request.user, expected)


class RecorderLeaseView(APIView):
    """Acquire/release one short recorder lease without affecting other sessions."""

    permission_classes = (IsAuthenticated,)

    def post(self, request, device_id):
        if not _identity_valid(request):
            return Response({'detail': '登录账号已变化，请重新打开录制器'}, status=status.HTTP_403_FORBIDDEN)
        try:
            with transaction.atomic():
                device = AppDevice.objects.select_for_update().get(pk=device_id)
                if not device_action_allowed(request.user, device):
                    return Response({'detail': '设备不可用或无权使用'}, status=status.HTTP_403_FORBIDDEN)
                device, token = acquire_specific_device(
                    device.pk, request.user, lease_seconds=RECORDER_LEASE_SECONDS,
                    cap_lease_to_grant=True,
                )
        except AppDevice.DoesNotExist:
            return Response({'detail': '设备不存在'}, status=status.HTTP_404_NOT_FOUND)
        except (DeviceLeaseError, DeviceExecutionBusy):
            return Response({'detail': '设备不可用或已有执行任务'}, status=status.HTTP_409_CONFLICT)
        granted_lease_seconds = int(
            (device.lease_expires_at - device.lease_acquired_at).total_seconds()
        )
        return Response({
            'device': AppDeviceSerializer(device).data,
            'lease_token': token,
            'lease_seconds': granted_lease_seconds,
        }, status=status.HTTP_201_CREATED)

    def delete(self, request, device_id):
        if not _identity_valid(request):
            return Response({'detail': '登录账号已变化，请重新打开录制器'}, status=status.HTTP_403_FORBIDDEN)
        token = request.data.get('lease_token')
        if not isinstance(token, str) or not token or len(token) > 1024:
            return Response({'detail': '设备租约无效'}, status=status.HTTP_403_FORBIDDEN)
        try:
            with transaction.atomic():
                device = AppDevice.objects.select_for_update().get(pk=device_id)
                validate_device_lease(device.pk, request.user, token)
                assert_device_execution_idle(device.pk)
                device = release_device(device.pk, request.user, token, force=False)
        except AppDevice.DoesNotExist:
            return Response({'detail': '设备不存在'}, status=status.HTTP_404_NOT_FOUND)
        except DeviceExecutionBusy:
            return Response({'detail': '设备正在执行任务，不能释放'}, status=status.HTTP_409_CONFLICT)
        except DeviceLeaseError:
            return Response({'detail': '设备租约无效、已过期或不属于当前用户'}, status=status.HTTP_403_FORBIDDEN)
        return Response({'device': AppDeviceSerializer(device).data})
