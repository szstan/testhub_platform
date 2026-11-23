"""Shared admission helpers for APP execution endpoints."""

from uuid import uuid4

from .access import device_action_allowed
from .device_pool import (
    DeviceLeaseError,
    assert_device_execution_lease,
    validate_device_lease,
)
from .execution_admission import assert_device_execution_idle


def execution_user_matches(user, expected_user_id):
    """Fence a request initiated before an account switch without trusting client identity."""
    return expected_user_id is None or str(expected_user_id) == str(getattr(user, 'pk', None))


def new_execution_dispatch_id():
    return uuid4().hex


def prepare_execution_device(device, user, *, lease_token='', auto_lock=False):
    """Validate or acquire a tokenless manual lease under the caller's row lock."""
    if not device_action_allowed(user, device):
        raise PermissionError('无权使用其他用户锁定的设备')

    acquired = False
    if device.locked_by_id is None and auto_lock:
        device.lock(user)
        device.refresh_from_db()
        acquired = True

    if device.locked_by_id != user.pk:
        raise DeviceLeaseError('请先锁定设备后再执行测试')
    validate_device_lease(device.pk, user, lease_token)
    assert_device_execution_lease(device.pk, user, device.lease_generation)
    assert_device_execution_idle(device.pk)
    return device, device.lease_generation, acquired


def enqueue_app_task(task, *, dispatch_id, args=(), kwargs=None):
    """Publish with a pre-persisted ID so failures and cancellation are fenced."""
    return task.apply_async(
        args=tuple(args),
        kwargs=dict(kwargs or {}),
        task_id=dispatch_id,
    )
