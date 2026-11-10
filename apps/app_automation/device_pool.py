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
    device.status = (
        DeviceStatus.AVAILABLE
        if not health_sensitive or device.health_status != 'unhealthy'
        else DeviceStatus.OFFLINE
    )


def _lease_fields():
    return [
        'locked_by', 'locked_at', 'lease_token_hash', 'lease_acquired_at',
        'lease_expires_at', 'last_heartbeat_at', 'status', 'updated_at',
    ]


def _reclaim_expired(device: AppDevice, now=None):
    now = now or timezone.now()
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


def _acquire_locked_device(device, user, *, now, lease_seconds):
    if _reclaim_expired(device, now):
        device.save(update_fields=_lease_fields())
    if _lease_is_active(device, now):
        raise DeviceLeaseError('设备已有有效租约')
    if device.locked_by_id is not None or device.lease_token_hash or device.status == DeviceStatus.LOCKED:
        raise DeviceLeaseError('设备锁状态不明确，拒绝重新分配')
    raw_token = secrets.token_urlsafe(32)
    seconds = max(int(lease_seconds or 3600), 30)
    max_seconds = max(int(device.max_allocation_time or 0), 30)
    expires = now + timedelta(seconds=min(seconds, max_seconds))
    device.lease_generation += 1
    device.locked_by = user
    device.locked_at = now
    device.lease_acquired_at = now
    device.lease_expires_at = expires
    device.last_heartbeat_at = now
    device.lease_token_hash = _hash_token(raw_token)
    device.status = DeviceStatus.LOCKED
    device.save(update_fields=[
        'locked_by', 'locked_at', 'lease_generation', 'lease_acquired_at',
        'lease_expires_at', 'last_heartbeat_at', 'lease_token_hash', 'status', 'updated_at',
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


def acquire_specific_device(device_id, user, *, lease_seconds=None):
    """Acquire one scheduled device without exposing its bearer token to Celery."""
    now = timezone.now()
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().get(pk=device_id)
        if device.maintenance_mode or device.status == DeviceStatus.OFFLINE:
            raise DeviceLeaseError('设备不可用')
        return _acquire_locked_device(device, user, now=now, lease_seconds=lease_seconds)


def _check_owner(device, user, token):
    if device.locked_by_id != getattr(user, 'id', None):
        raise DeviceLeaseError('无权操作该设备租约')
    if device.lease_token_hash and not hmac.compare_digest(device.lease_token_hash, _hash_token(token or '')):
        raise DeviceLeaseError('设备租约令牌无效')


def validate_device_lease(device_id, user, token):
    """Validate a user-presented token before enqueueing a device execution."""
    device = AppDevice.objects.select_for_update().get(pk=device_id)
    _check_owner(device, user, token)
    if not _lease_is_active(device, timezone.now()):
        raise DeviceLeaseError('设备租约已过期或无效')
    return device


def heartbeat_device(device_id, user, token, *, lease_seconds=None):
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().get(pk=device_id)
        _check_owner(device, user, token)
        now = timezone.now()
        if not _lease_is_active(device, now):
            raise DeviceLeaseError('设备租约已过期，不能续租')
        seconds = max(int(lease_seconds or device.max_allocation_time), 30)
        if device.lease_token_hash:
            device.lease_expires_at = now + timedelta(
                seconds=min(seconds, max(int(device.max_allocation_time or 0), 30))
            )
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
        seconds = max(int(lease_seconds or 300), 1)
        if device.lease_token_hash:
            device.lease_expires_at = now + timedelta(
                seconds=min(seconds, max(int(device.max_allocation_time or 0), 1))
            )
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
        _clear_lease(device)
        device.save(update_fields=_lease_fields())
        return True


def release_device(device_id, user, token, *, force=False):
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().get(pk=device_id)
        if not force:
            _check_owner(device, user, token)
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
    'DeviceLeaseError',
    'DeviceLeaseHeartbeat',
    'acquire_device',
    'acquire_specific_device',
    'assert_device_execution_lease',
    'heartbeat_device',
    'heartbeat_device_execution',
    'is_device_lease_active',
    'reap_expired_device_leases',
    'release_device',
    'release_device_execution_lease',
    'validate_device_lease',
]
