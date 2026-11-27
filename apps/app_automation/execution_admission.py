"""Transactional admission and worker claims for APP device executions."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import AppDevice, AppTestExecution
from .device_pool import DeviceLeaseError

ACTIVE_EXECUTION_STATUSES = ('pending', 'running')


class DeviceExecutionBusy(DeviceLeaseError):
    """The device already has queued or running executions."""


def assert_device_execution_idle(device_id):
    if AppTestExecution.objects.filter(
        device_id=device_id,
        status__in=ACTIVE_EXECUTION_STATUSES,
    ).exists():
        raise DeviceExecutionBusy('设备已有待执行或正在执行的任务')
    assert_device_action_clear(device_id)


def assert_device_action_clear(device_id):
    if (
        AppDevice.objects.filter(pk=device_id, device_action_pending=True).exists()
        or AppTestExecution.objects.filter(
            device_id=device_id,
            device_action_pending=True,
        ).exists()
    ):
        raise DeviceExecutionBusy('设备存在待安全恢复的设备动作')


def mark_device_action_pending(execution_id):
    """Durably fence a worker before it starts any device I/O."""
    from .device_pool import _assert_execution_lease, _has_pending_device_action

    snapshot = AppTestExecution.objects.filter(pk=execution_id).values(
        'device_id', 'user_id', 'device_lease_generation'
    ).first()
    if not snapshot or not snapshot['device_id'] or not snapshot['user_id']:
        raise DeviceLeaseError('执行记录未绑定有效设备租约')

    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().filter(pk=snapshot['device_id']).first()
        execution = AppTestExecution.objects.select_for_update().filter(pk=execution_id).first()
        if device is None or execution is None:
            raise DeviceLeaseError('执行记录或设备租约已失效')
        if (
            execution.device_id != device.pk
            or execution.user_id != snapshot['user_id']
            or execution.device_lease_generation != snapshot['device_lease_generation']
            or execution.status != 'running'
            or execution.device_action_pending
        ):
            raise DeviceLeaseError('执行设备租约状态不允许启动设备动作')
        _assert_execution_lease(
            device,
            execution.user,
            execution.device_lease_generation,
            execution_id=execution.pk,
        )
        if _has_pending_device_action(device.pk):
            raise DeviceLeaseError('设备存在待安全恢复的其他动作')
        execution.device_action_pending = True
        execution.save(update_fields=['device_action_pending', 'updated_at'])
    return True


def clear_device_action_pending(execution_id):
    """Clear a pending action only while the execution still owns its lease."""
    from .device_pool import _assert_execution_lease

    snapshot = AppTestExecution.objects.filter(pk=execution_id).values(
        'device_id', 'user_id', 'device_lease_generation'
    ).first()
    if not snapshot or not snapshot['device_id'] or not snapshot['user_id']:
        raise DeviceLeaseError('执行记录未绑定有效设备租约')

    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().filter(pk=snapshot['device_id']).first()
        execution = AppTestExecution.objects.select_for_update().filter(pk=execution_id).first()
        if device is None or execution is None:
            raise DeviceLeaseError('执行记录或设备租约已失效')
        if (
            execution.device_id != device.pk
            or execution.user_id != snapshot['user_id']
            or execution.device_lease_generation != snapshot['device_lease_generation']
            or execution.status != 'running'
        ):
            raise DeviceLeaseError('执行设备租约所有者或世代已变更')
        _assert_execution_lease(
            device,
            execution.user,
            execution.device_lease_generation,
            execution_id=execution.pk,
        )
        if not execution.device_action_pending:
            return False
        execution.device_action_pending = False
        execution.save(update_fields=['device_action_pending', 'updated_at'])
    return True


def release_device_if_idle(device_id, user, generation):
    """Release a matching lease only while its device has no active execution."""
    from .device_pool import release_device_execution_lease

    with transaction.atomic():
        if not AppDevice.objects.select_for_update().filter(pk=device_id).exists():
            return False
        if AppTestExecution.objects.filter(
            device_id=device_id,
            status__in=ACTIVE_EXECUTION_STATUSES,
        ).exists():
            return False
        if AppDevice.objects.filter(pk=device_id, device_action_pending=True).exists():
            return False
        if AppTestExecution.objects.filter(
            device_id=device_id,
            device_action_pending=True,
        ).exists():
            return False
        return release_device_execution_lease(device_id, user, generation)


def claim_app_execution_batch(execution_ids, *, suite_id=None):
    """Claim a durable batch while holding its device row lock, before any I/O."""
    from .device_pool import assert_device_execution_lease

    execution_ids = list(execution_ids or [])
    if not execution_ids or len(set(execution_ids)) != len(execution_ids):
        return False

    first = AppTestExecution.objects.filter(pk=execution_ids[0]).values(
        'device_id', 'user_id'
    ).first()
    if not first or not first['device_id'] or not first['user_id']:
        return False

    with transaction.atomic():
        device = AppDevice.objects.select_for_update().filter(pk=first['device_id']).first()
        if device is None:
            return False
        assert_device_action_clear(device.pk)
        rows = list(
            AppTestExecution.objects.select_for_update()
            .filter(pk__in=execution_ids)
            .order_by('pk')
        )
        by_id = {row.pk: row for row in rows}
        if len(by_id) != len(execution_ids):
            return False
        ordered = [by_id[execution_id] for execution_id in execution_ids]
        generation = ordered[0].device_lease_generation
        user_id = ordered[0].user_id
        if any(
            row.device_id != device.pk
            or row.user_id != user_id
            or row.device_lease_generation != generation
            or (suite_id is not None and row.test_suite_id != suite_id)
            for row in ordered
        ):
            raise RuntimeError('APP执行批次未绑定到同一设备租约')

        # A redelivery or a stopped/settled task must never restart its rows.
        if any(row.status != 'pending' for row in ordered):
            return False

        competing = AppTestExecution.objects.filter(
            device_id=device.pk,
            status__in=ACTIVE_EXECUTION_STATUSES,
        ).exclude(pk__in=execution_ids).exists()
        if competing:
            now = timezone.now()
            AppTestExecution.objects.filter(
                pk__in=execution_ids,
                status='pending',
            ).update(
                status='error',
                error_message='设备已有其他待执行或正在执行的任务',
                finished_at=now,
                updated_at=now,
            )
            return False

        assert_device_execution_lease(
            device.pk,
            ordered[0].user,
            generation,
            execution_id=ordered[0].pk,
        )
        now = timezone.now()
        claimed = AppTestExecution.objects.filter(
            pk=ordered[0].pk,
            status='pending',
        ).update(status='running', started_at=now, progress=0, updated_at=now)
        return claimed == 1


def start_next_app_execution(execution_id, batch_ids):
    """Atomically move one sequential batch item from pending to running."""
    from .device_pool import assert_device_execution_lease

    batch_ids = list(batch_ids)
    snapshot = AppTestExecution.objects.filter(pk=execution_id).values(
        'device_id', 'user_id', 'device_lease_generation'
    ).first()
    if not snapshot or not snapshot['device_id'] or not snapshot['user_id']:
        return False

    with transaction.atomic():
        device = AppDevice.objects.select_for_update().filter(pk=snapshot['device_id']).first()
        execution = AppTestExecution.objects.select_for_update().filter(pk=execution_id).first()
        if device is None or execution is None or execution.status != 'pending':
            return False
        assert_device_action_clear(device.pk)
        assert_device_execution_lease(
            device.pk,
            execution.user,
            execution.device_lease_generation,
            execution_id=execution.pk,
        )
        if AppTestExecution.objects.filter(
            device_id=device.pk,
            status__in=ACTIVE_EXECUTION_STATUSES,
        ).exclude(pk__in=batch_ids).exists():
            raise DeviceExecutionBusy('设备已有其他待执行或正在执行的任务')
        now = timezone.now()
        return AppTestExecution.objects.filter(
            pk=execution.pk,
            status='pending',
        ).update(status='running', started_at=now, progress=0, updated_at=now) == 1


def settle_app_execution_failure(execution_ids, error, *, include_running=False):
    """Settle only rows the caller can prove it owns, under the device mutex."""
    execution_ids = list(dict.fromkeys(execution_ids or []))
    if not execution_ids:
        return []
    device_id = AppTestExecution.objects.filter(pk__in=execution_ids).values_list(
        'device_id', flat=True
    ).first()
    now = timezone.now()
    statuses = ACTIVE_EXECUTION_STATUSES if include_running else ('pending',)
    with transaction.atomic():
        if device_id:
            AppDevice.objects.select_for_update().filter(pk=device_id).first()
        rows = list(
            AppTestExecution.objects.select_for_update().filter(
                pk__in=execution_ids,
                status__in=statuses,
            )
        )
        if not include_running:
            rows = [row for row in rows if row.started_at is None]
        ids = [row.pk for row in rows]
        stopped_ids = [row.pk for row in rows if include_running and row.stop_requested]
        error_ids = [row.pk for row in rows if row.pk not in stopped_ids]
        if stopped_ids:
            AppTestExecution.objects.filter(pk__in=stopped_ids, status='running').update(
                status='stopped',
                result=None,
                finished_at=now,
                updated_at=now,
            )
        if error_ids:
            AppTestExecution.objects.filter(pk__in=error_ids, status__in=statuses).update(
                status='error',
                result=None,
                error_message=str(error)[:4000],
                finished_at=now,
                updated_at=now,
            )
        return ids
