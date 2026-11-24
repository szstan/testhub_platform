"""Capability-aware APP device leases with generation fencing."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from datetime import timedelta
from threading import Event, Thread
from typing import Mapping

from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from .constants import DeviceStatus
from .models import AppDevice, AppTestExecution

logger = logging.getLogger(__name__)


class DeviceLeaseError(RuntimeError):
    """Raised when a device lease cannot be acquired or mutated."""


class DeviceActionBusy(DeviceLeaseError):
    """The lease is valid, but another device action or execution is pending."""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _matches_capabilities(device: AppDevice, selector: Mapping[str, object] | None) -> bool:
    selector = selector or {}
    capabilities = dict(device.capabilities or {})
    specs = dict(device.device_specs or {})
    merged = {**specs, **capabilities}
    for key, expected in selector.items():
        actual = merged.get(key)
        if isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _has_pending_device_action(device_id):
    return (
        AppDevice.objects.filter(pk=device_id, device_action_pending=True).exists()
        or AppTestExecution.objects.filter(
            device_id=device_id,
            device_action_pending=True,
        ).exists()
    )


def _lease_is_active(device: AppDevice, now) -> bool:
    if device.locked_by_id is None:
        return False
    if device.lease_token_hash:
        return bool(device.lease_expires_at and device.lease_expires_at > now)
    if device.lease_expires_at:
        return device.lease_expires_at > now
    renewed_at = device.last_heartbeat_at or device.locked_at
    return bool(device.locked_at and renewed_at and now < renewed_at + timedelta(
        seconds=max(int(device.max_allocation_time or 0), 0)
    ))


def is_device_lease_active(device: AppDevice, now=None) -> bool:
    return _lease_is_active(device, now or timezone.now())


def _clear_lease(device: AppDevice, *, health_sensitive=True):
    device.locked_by = None
    device.locked_at = None
    device.lease_token_hash = ''
    device.lease_acquired_at = None
    device.lease_expires_at = None
    device.last_heartbeat_at = None
    device.lease_max_seconds = None
    device.status = (
        DeviceStatus.OFFLINE
        if device.status == DeviceStatus.OFFLINE
        or (health_sensitive and device.health_status == 'unhealthy')
        else DeviceStatus.AVAILABLE
    )


def _lease_fields():
    return [
        'locked_by', 'locked_at', 'lease_token_hash', 'lease_acquired_at',
        'lease_expires_at', 'last_heartbeat_at', 'lease_max_seconds', 'status', 'updated_at',
    ]


def _reclaim_expired(device: AppDevice, now=None):
    now = now or timezone.now()
    if _has_pending_device_action(device.pk):
        return False
    lease_expired = bool(device.lease_expires_at and device.lease_expires_at <= now)
    legacy_expired = bool(
        not device.lease_token_hash
        and device.lease_expires_at is None
        and device.locked_by_id is not None
        and device.locked_at
        and now >= (device.last_heartbeat_at or device.locked_at) + timedelta(
            seconds=max(int(device.max_allocation_time or 0), 0)
        )
    )
    orphaned_lock = device.status == DeviceStatus.LOCKED and device.locked_by_id is None
    if lease_expired or legacy_expired or orphaned_lock:
        _clear_lease(device)
        return True
    return False


def _acquire_locked_device(device, user, *, now, lease_seconds, cap_lease_to_grant=False):
    from .execution_admission import assert_device_execution_idle

    if _reclaim_expired(device, now):
        device.save(update_fields=_lease_fields())
    if _lease_is_active(device, now):
        raise DeviceLeaseError('设备已有有效租约')
    if device.locked_by_id is not None or device.lease_token_hash or device.status == DeviceStatus.LOCKED:
        raise DeviceLeaseError('设备锁状态不明确，拒绝重新分配')
    assert_device_execution_idle(device.pk)
    raw_token = secrets.token_urlsafe(32)
    seconds = max(int(lease_seconds or 3600), 30)
    max_seconds = max(int(device.max_allocation_time or 0), 30)
    granted_seconds = min(seconds, max_seconds)
    expires = now + timedelta(seconds=granted_seconds)
    device.lease_generation += 1
    device.locked_by = user
    device.locked_at = now
    device.lease_acquired_at = now
    device.lease_expires_at = expires
    device.last_heartbeat_at = now
    device.lease_max_seconds = granted_seconds if cap_lease_to_grant else None
    device.lease_token_hash = _hash_token(raw_token)
    device.status = DeviceStatus.LOCKED
    device.save(update_fields=[
        'locked_by', 'locked_at', 'lease_generation', 'lease_acquired_at',
        'lease_expires_at', 'last_heartbeat_at', 'lease_max_seconds', 'lease_token_hash', 'status', 'updated_at',
    ])
    return device, raw_token


def acquire_device(user, *, capabilities=None, lease_seconds=None):
    now = timezone.now()
    candidates = AppDevice.objects.select_for_update().filter(
        maintenance_mode=False,
        status__in=[DeviceStatus.AVAILABLE, DeviceStatus.ONLINE, DeviceStatus.LOCKED],
    ).order_by('last_seen_at', 'updated_at')
    with transaction.atomic():
        for device in candidates:
            if _lease_is_active(device, now):
                continue
            if not _matches_capabilities(device, capabilities):
                continue
            try:
                return _acquire_locked_device(
                    device, user, now=now, lease_seconds=lease_seconds
                )
            except DeviceLeaseError:
                continue
    raise DeviceLeaseError('没有满足能力条件的可用设备')


def acquire_specific_device(device_id, user, *, lease_seconds=None, cap_lease_to_grant=False):
    """Acquire one device, optionally capping renewals to this grant's duration."""
    now = timezone.now()
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().get(pk=device_id)
        if device.maintenance_mode or device.status == DeviceStatus.OFFLINE:
            raise DeviceLeaseError('设备不可用')
        return _acquire_locked_device(
            device, user, now=now, lease_seconds=lease_seconds,
            cap_lease_to_grant=cap_lease_to_grant,
        )


def _check_token(device, token):
    if not isinstance(token, str):
        raise DeviceLeaseError('设备租约令牌无效')
    if device.lease_token_hash and not hmac.compare_digest(
        device.lease_token_hash, _hash_token(token)
    ):
        raise DeviceLeaseError('设备租约令牌无效')


def _check_owner(device, user, token):
    if device.locked_by_id != getattr(user, 'id', None):
        raise DeviceLeaseError('无权操作该设备租约')
    _check_token(device, token)


def begin_device_management_action(
    device_id, user, token='', *, admin=False, allowed_connection_types=None,
):
    """Reserve a device for bounded management I/O without holding its row lock."""
    if not isinstance(token, str):
        raise DeviceLeaseError('设备租约令牌无效')
    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().get(pk=device_id)
        if allowed_connection_types and device.connection_type not in allowed_connection_types:
            raise DeviceActionBusy('只能断开远程设备的连接')
        if (
            device.device_action_pending
            or AppTestExecution.objects.filter(
                device_id=device.pk,
                status__in=('pending', 'running'),
            ).exists()
            or AppTestExecution.objects.filter(
                device_id=device.pk,
                device_action_pending=True,
            ).exists()
        ):
            raise DeviceActionBusy('设备存在待执行或待安全恢复的动作')

        now = timezone.now()
        temporary_lease = False
        if device.locked_by_id is None:
            if not admin:
                raise DeviceLeaseError('设备没有当前用户的有效租约')
            if (
                device.status == DeviceStatus.LOCKED
                or device.lease_token_hash
                or device.locked_at is not None
                or device.lease_acquired_at is not None
                or device.lease_expires_at is not None
                or device.lease_max_seconds is not None
            ):
                raise DeviceActionBusy('设备租约状态不明确，拒绝管理操作')
            device.lease_generation += 1
            device.locked_by = user
            device.locked_at = now
            device.last_heartbeat_at = now
            device.lease_acquired_at = None
            device.lease_expires_at = None
            device.lease_max_seconds = None
            device.lease_token_hash = ''
            device.status = DeviceStatus.LOCKED
            temporary_lease = True
            fields = [
                'locked_by', 'locked_at', 'last_heartbeat_at', 'lease_acquired_at',
                'lease_expires_at', 'lease_max_seconds', 'lease_token_hash',
                'lease_generation', 'status',
            ]
        else:
            if device.locked_by_id != getattr(user, 'id', None) and not admin:
                raise DeviceLeaseError('无权操作该设备租约')
            if not _lease_is_active(device, now):
                raise DeviceActionBusy('设备租约已过期或状态不明确，拒绝管理操作')
            if device.lease_token_hash:
                if device.locked_by_id == getattr(user, 'id', None):
                    _check_owner(device, user, token)
                else:
                    _check_token(device, token)
            fields = []

        device.device_action_pending = True
        device.device_action_epoch += 1
        device.save(update_fields=[
            *fields, 'device_action_pending', 'device_action_epoch', 'updated_at'
        ])
    return device, temporary_lease, device.device_action_epoch


def finish_device_management_action(
    device_id, owner_id, generation, action_epoch, *, status=None, release_reservation=False,
    quarantine=False,
):
    """Finish a management action only under its original action and lease fences."""
    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().filter(
            pk=device_id,
            locked_by_id=owner_id,
            lease_generation=generation,
            device_action_pending=True,
            device_action_epoch=action_epoch,
        ).first()
        if device is None:
            raise DeviceLeaseError('设备管理动作的所有者、世代或待处理标记已变更')

        if quarantine:
            device.maintenance_mode = True
            device.health_status = 'unknown'
            device.save(update_fields=['maintenance_mode', 'health_status', 'updated_at'])
            return device

        fields = ['device_action_pending', 'updated_at']
        if release_reservation:
            _clear_lease(device)
            fields.extend(_lease_fields())
        if status is not None:
            device.status = status
            fields.append('status')
        device.device_action_pending = False
        device.save(update_fields=list(dict.fromkeys(fields)))
    return device


def release_device_snapshot(device_id, user, generation, token=''):
    """Release a legacy API snapshot only if owner and generation still match."""
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().filter(
            pk=device_id,
            locked_by_id=getattr(user, 'id', None),
            lease_generation=generation,
        ).first()
        if device is None:
            return False
        _check_owner(device, user, token)
        if _has_pending_device_action(device.pk):
            raise DeviceLeaseError('设备存在待安全恢复的设备动作')
        _clear_lease(device)
        device.save(update_fields=_lease_fields())
        return True


def _renewal_seconds(device, lease_seconds, *, default_seconds, minimum):
    seconds = max(int(lease_seconds or default_seconds), minimum)
    allocation_limit = max(int(device.max_allocation_time or 0), minimum)
    seconds = min(seconds, allocation_limit)
    if device.lease_max_seconds is not None:
        seconds = min(seconds, max(int(device.lease_max_seconds), minimum))
    return seconds


def validate_device_lease(device_id, user, token):
    """Validate a user-presented token before enqueueing a device execution."""
    device = AppDevice.objects.select_for_update().get(pk=device_id)
    _check_owner(device, user, token)
    if device.maintenance_mode or device.status == DeviceStatus.OFFLINE:
        raise DeviceLeaseError('设备不可用，请检查维护状态或连接')
    if not _lease_is_active(device, timezone.now()):
        raise DeviceLeaseError('设备租约已过期或无效')
    return device


def begin_device_action(device_id, user, token, *, lease_validator=None):
    """Commit a lease-fenced device marker before a non-worker device action."""
    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().get(pk=device_id)
        validate = lease_validator or validate_device_lease
        validated = validate(device.pk, user, token)
        if (
            validated.pk != device.pk
            or validated.locked_by_id != device.locked_by_id
            or validated.lease_generation != device.lease_generation
        ):
            raise DeviceLeaseError('设备租约验证结果与当前世代不匹配')
        if AppTestExecution.objects.filter(
            device_id=device.pk,
            status__in=('pending', 'running'),
        ).exists() or _has_pending_device_action(device.pk):
            raise DeviceActionBusy('设备存在待执行或待安全恢复的动作')
        device.device_action_pending = True
        device.device_action_epoch += 1
        device.save(update_fields=[
            'device_action_pending', 'device_action_epoch', 'updated_at'
        ])
        fence = (device.device_id, device.lease_generation, device.device_action_epoch)
    return fence


def finish_device_action(device_id, user, generation, action_epoch, *, quarantine=False):
    """Clear a committed action marker under its original lease and action fences."""
    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().filter(
            pk=device_id,
            locked_by_id=getattr(user, 'id', None),
            lease_generation=generation,
            device_action_pending=True,
            device_action_epoch=action_epoch,
        ).first()
        if device is None:
            raise DeviceLeaseError('设备动作的租约所有者、世代或待处理标记已变更')
        fields = ['device_action_pending', 'updated_at']
        if quarantine:
            device.maintenance_mode = True
            device.health_status = 'unknown'
            fields.extend(['maintenance_mode', 'health_status'])
        device.device_action_pending = False
        device.save(update_fields=fields)
    return device


def heartbeat_device(device_id, user, token, *, lease_seconds=None):
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().get(pk=device_id)
        _check_owner(device, user, token)
        now = timezone.now()
        if not _lease_is_active(device, now):
            raise DeviceLeaseError('设备租约已过期，不能续租')
        seconds = _renewal_seconds(
            device, lease_seconds, default_seconds=device.max_allocation_time, minimum=30,
        )
        if device.lease_token_hash:
            device.lease_expires_at = now + timedelta(seconds=seconds)
        # Keep the original locked_at as the legacy execution binding proof.
        device.last_heartbeat_at = now
        device.last_seen_at = now
        device.save(update_fields=[
            'lease_expires_at', 'last_heartbeat_at', 'last_seen_at', 'updated_at'
        ])
        return device


def _assert_execution_lease(device, user, generation, *, execution_id=None, now=None):
    now = now or timezone.now()
    if device.locked_by_id != getattr(user, 'id', None):
        raise DeviceLeaseError('设备租约所有者已变更')
    if generation is None:
        legacy_proven = (
            device.lease_generation == 0
            and not device.lease_token_hash
            and device.lease_acquired_at is None
            and device.lease_expires_at is None
            and device.locked_at is not None
        )
        if not legacy_proven or execution_id is None:
            raise DeviceLeaseError('缺少设备租约世代，拒绝执行旧任务')
        if not AppTestExecution.objects.filter(
            pk=execution_id,
            device_id=device.pk,
            user_id=getattr(user, 'id', None),
            device_lease_generation__isnull=True,
            status__in=['pending', 'running'],
            created_at__gte=device.locked_at,
        ).exists():
            raise DeviceLeaseError('旧版设备锁无法证明属于此执行')
        active_legacy = AppTestExecution.objects.filter(
            device_id=device.pk,
            user_id=getattr(user, 'id', None),
            device_lease_generation__isnull=True,
            status__in=['pending', 'running'],
        )
        if execution_id is not None:
            active_legacy = active_legacy.exclude(pk=execution_id)
        if active_legacy.exists():
            raise DeviceLeaseError('旧版设备锁无法唯一绑定到此执行')
    elif device.lease_generation != generation:
        raise DeviceLeaseError('设备租约世代已被替换')
    if device.maintenance_mode or device.status == DeviceStatus.OFFLINE:
        raise DeviceLeaseError('设备不可用，请检查维护状态或连接')
    if not _lease_is_active(device, now):
        raise DeviceLeaseError('设备租约已过期或无效')
    return device


def assert_device_execution_lease(device_id, user, generation, *, execution_id=None):
    device = AppDevice.objects.get(pk=device_id)
    return _assert_execution_lease(
        device, user, generation, execution_id=execution_id
    )


def heartbeat_device_execution(device_id, user, generation, *, execution_id=None, lease_seconds=300):
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().get(pk=device_id)
        now = timezone.now()
        _assert_execution_lease(
            device, user, generation, execution_id=execution_id, now=now
        )
        seconds = _renewal_seconds(device, lease_seconds, default_seconds=300, minimum=1)
        if device.lease_token_hash:
            device.lease_expires_at = now + timedelta(seconds=seconds)
        # Keep the original locked_at as the legacy execution binding proof.
        device.last_heartbeat_at = now
        device.last_seen_at = now
        device.save(update_fields=[
            'lease_expires_at', 'last_heartbeat_at', 'last_seen_at', 'updated_at'
        ])
        return device


def release_device_execution_lease(device_id, user, generation):
    """Compare-and-release used by workers; never release a newer same-owner lease."""
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().filter(pk=device_id).first()
        if device is None or device.locked_by_id != getattr(user, 'id', None):
            return False
        if generation is None:
            if (
                device.lease_generation != 0
                or device.lease_token_hash
                or device.lease_acquired_at is not None
                or device.lease_expires_at is not None
            ):
                return False
        elif device.lease_generation != generation:
            return False
        if _has_pending_device_action(device.pk):
            return False
        _clear_lease(device)
        device.save(update_fields=_lease_fields())
        return True


def release_device(device_id, user, token, *, force=False, expected_generation=None):
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().get(pk=device_id)
        if expected_generation is not None and device.lease_generation != expected_generation:
            raise DeviceLeaseError('设备租约已变更，请刷新后重试')
        if force:
            _check_token(device, token)
        else:
            _check_owner(device, user, token)
        if _has_pending_device_action(device.pk):
            raise DeviceLeaseError('设备存在待安全恢复的设备动作')
        _clear_lease(device)
        device.save(update_fields=_lease_fields())
        return device


def reap_expired_device_leases():
    now = timezone.now()
    reclaimed = 0
    # Discover candidates without locking healthy owners. Tokenless legacy
    # locks need their heartbeat/allocation interval evaluated before locking.
    candidates = AppDevice.objects.filter(
        Q(lease_expires_at__isnull=False, lease_expires_at__lte=now)
        | Q(lease_token_hash='', lease_expires_at__isnull=True,
            locked_at__isnull=False, locked_by__isnull=False)
        | Q(status=DeviceStatus.LOCKED, locked_by__isnull=True)
    ).only(*_lease_fields(), 'max_allocation_time', 'health_status').order_by('pk')
    cursor_pk = 0
    while True:
        batch = list(candidates.filter(pk__gt=cursor_pk)[:200])
        if not batch:
            break
        cursor_pk = batch[-1].pk
        expired_ids = [device.pk for device in batch if _reclaim_expired(device, now)]
        if not expired_ids:
            continue
        with transaction.atomic():
            devices = AppDevice.objects.select_for_update().filter(pk__in=expired_ids)
            for device in devices:
                # A concurrent renewal/acquisition may have replaced the
                # candidate snapshot; decide again only after owning the row.
                if _reclaim_expired(device, timezone.now()):
                    device.save(update_fields=_lease_fields())
                    reclaimed += 1
    return reclaimed


class DeviceLeaseHeartbeat:
    """Renew and fence a worker's device lease without transporting its raw token."""

    def __init__(self, device_id, user, generation, *, execution_id=None, lease_seconds=300):
        self.device_id = device_id
        self.user = user
        self.generation = generation
        self.execution_id = execution_id
        self.lease_seconds = max(int(lease_seconds), 1)
        self.interval_seconds = max(min(self.lease_seconds / 3, 5), 0.5)
        self._stop = Event()
        self._lost = Event()
        self._deadline = time.monotonic() + self.lease_seconds
        self._thread = None

    def start(self):
        self.raise_if_lost()
        device = assert_device_execution_lease(
            self.device_id, self.user, self.generation, execution_id=self.execution_id
        )
        renewal_seconds = min(self.lease_seconds, max(int(device.max_allocation_time or 0), 1))
        self.interval_seconds = max(min(renewal_seconds / 3, 5), 0.1)
        self._thread = Thread(
            target=self._run,
            name=f'app-device-lease-{self.device_id}',
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=min(self.interval_seconds + 1, 10))

    def raise_if_lost(self):
        if self._lost.is_set() or time.monotonic() >= self._deadline:
            self._lost.set()
            raise DeviceLeaseError('设备租约续期失败或已失效')
        try:
            assert_device_execution_lease(
                self.device_id,
                self.user,
                self.generation,
                execution_id=self.execution_id,
            )
        except Exception:
            self._lost.set()
            raise

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            close_old_connections()
            try:
                heartbeat_device_execution(
                    self.device_id,
                    self.user,
                    self.generation,
                    execution_id=self.execution_id,
                    lease_seconds=self.lease_seconds,
                )
                self._deadline = time.monotonic() + self.lease_seconds
            except Exception:
                self._lost.set()
                logger.warning('APP device lease heartbeat lost: device_id=%s', self.device_id, exc_info=True)
                return
            finally:
                close_old_connections()


__all__ = [
    'DeviceActionBusy',
    'DeviceLeaseError',
    'DeviceLeaseHeartbeat',
    'acquire_device',
    'acquire_specific_device',
    'assert_device_execution_lease',
    'begin_device_action',
    'begin_device_management_action',
    'finish_device_action',
    'finish_device_management_action',
    'heartbeat_device',
    'heartbeat_device_execution',
    'is_device_lease_active',
    'reap_expired_device_leases',
    'release_device',
    'release_device_execution_lease',
    'release_device_snapshot',
    'validate_device_lease',
]
