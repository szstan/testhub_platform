"""Capability-aware APP device leases built on the legacy lock fields."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Mapping

from django.db import transaction
from django.utils import timezone

from .constants import DeviceStatus
from .models import AppDevice


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


def _reclaim_expired(device: AppDevice, now=None):
    now = now or timezone.now()
    if device.lease_expires_at and device.lease_expires_at <= now:
        device.locked_by = None
        device.locked_at = None
        device.lease_token_hash = ''
        device.lease_acquired_at = None
        device.lease_expires_at = None
        device.last_heartbeat_at = None
        device.status = DeviceStatus.AVAILABLE if device.health_status != 'unhealthy' else DeviceStatus.OFFLINE
        return True
    if device.locked_at and device.is_lock_expired():
        device.locked_by = None
        device.locked_at = None
        device.lease_token_hash = ''
        device.lease_acquired_at = None
        device.lease_expires_at = None
        device.last_heartbeat_at = None
        device.status = DeviceStatus.AVAILABLE
        return True
    return False


def acquire_device(user, *, capabilities=None, lease_seconds=None):
    lease_seconds = max(int(lease_seconds or 3600), 30)
    now = timezone.now()
    candidates = AppDevice.objects.select_for_update().filter(
        maintenance_mode=False,
        status__in=[DeviceStatus.AVAILABLE, DeviceStatus.ONLINE, DeviceStatus.LOCKED],
    ).order_by('last_seen_at', 'updated_at')
    with transaction.atomic():
        for device in candidates:
            if _reclaim_expired(device, now):
                device.save(update_fields=[
                    'locked_by', 'locked_at', 'lease_token_hash', 'lease_acquired_at',
                    'lease_expires_at', 'last_heartbeat_at', 'status', 'updated_at',
                ])
            if device.status == DeviceStatus.LOCKED or not _matches_capabilities(device, capabilities):
                continue
            raw_token = secrets.token_urlsafe(32)
            expires = now + timedelta(seconds=min(lease_seconds, device.max_allocation_time))
            device.locked_by = user
            device.locked_at = now
            device.lease_acquired_at = now
            device.lease_expires_at = expires
            device.last_heartbeat_at = now
            device.lease_token_hash = _hash_token(raw_token)
            device.status = DeviceStatus.LOCKED
            device.save(update_fields=[
                'locked_by', 'locked_at', 'lease_acquired_at', 'lease_expires_at',
                'last_heartbeat_at', 'lease_token_hash', 'status', 'updated_at',
            ])
            return device, raw_token
    raise DeviceLeaseError('没有满足能力条件的可用设备')


def _check_owner(device, user, token):
    if device.locked_by_id != getattr(user, 'id', None):
        raise DeviceLeaseError('无权操作该设备租约')
    if device.lease_token_hash and not hmac.compare_digest(device.lease_token_hash, _hash_token(token or '')):
        raise DeviceLeaseError('设备租约令牌无效')


def validate_device_lease(device_id, user, token):
    """Validate an active lease before a worker mutates or executes a device."""
    device = AppDevice.objects.select_for_update().get(pk=device_id)
    _check_owner(device, user, token)
    if device.lease_expires_at and device.lease_expires_at <= timezone.now():
        raise DeviceLeaseError('设备租约已过期')
    return device


def heartbeat_device(device_id, user, token, *, lease_seconds=None):
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().get(pk=device_id)
        _check_owner(device, user, token)
        now = timezone.now()
        seconds = max(int(lease_seconds or device.max_allocation_time), 30)
        device.last_heartbeat_at = now
        device.lease_expires_at = now + timedelta(seconds=min(seconds, device.max_allocation_time))
        device.last_seen_at = now
        device.save(update_fields=['last_heartbeat_at', 'lease_expires_at', 'last_seen_at', 'updated_at'])
        return device


def release_device(device_id, user, token, *, force=False):
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().get(pk=device_id)
        if not force:
            _check_owner(device, user, token)
        device.locked_by = None
        device.locked_at = None
        device.lease_token_hash = ''
        device.lease_acquired_at = None
        device.lease_expires_at = None
        device.last_heartbeat_at = None
        device.status = DeviceStatus.AVAILABLE if device.health_status != 'unhealthy' else DeviceStatus.OFFLINE
        device.save(update_fields=[
            'locked_by', 'locked_at', 'lease_token_hash', 'lease_acquired_at',
            'lease_expires_at', 'last_heartbeat_at', 'status', 'updated_at',
        ])
        return device


def reap_expired_device_leases():
    now = timezone.now()
    reclaimed = 0
    with transaction.atomic():
        devices = AppDevice.objects.select_for_update().filter(
            lease_expires_at__isnull=False,
            lease_expires_at__lte=now,
        )
        for device in devices:
            if _reclaim_expired(device, now):
                device.save(update_fields=[
                    'locked_by', 'locked_at', 'lease_token_hash', 'lease_acquired_at',
                    'lease_expires_at', 'last_heartbeat_at', 'status', 'updated_at',
                ])
                reclaimed += 1
    return reclaimed
