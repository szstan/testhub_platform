"""Cross-process scheduler claims with a bounded lease and fencing token."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone


class ScheduleClaimLost(RuntimeError):
    """Raised when a worker tries to update a stale or reclaimed schedule."""


@dataclass(frozen=True)
class ScheduleClaim:
    """The task snapshot and fencing token returned to a dispatcher."""

    task: object
    token: str
    expires_at: object


def claim_due_task(
    model,
    task_id,
    *,
    due_field: str,
    lease_seconds: int = 300,
    status_field: str = "status",
    active_status: str = "ACTIVE",
    token_field: str = "scheduler_lease_token",
    expires_field: str = "scheduler_lease_expires_at",
    now=None,
):
    """Atomically claim one due task.

    The row lock makes competing scheduler processes mutually exclusive. A
    worker must carry the returned token to fence updates after a lease is
    reclaimed. Returning ``None`` means the task was not due or another worker
    still owns a live lease.
    """

    now = now or timezone.now()
    lease_seconds = max(int(lease_seconds), 1)
    with transaction.atomic():
        task = model.objects.select_for_update().filter(pk=task_id).first()
        if task is None or getattr(task, status_field) != active_status:
            return None
        due_at = getattr(task, due_field)
        if due_at is None or due_at > now:
            return None
        current_token = getattr(task, token_field, "")
        current_expiry = getattr(task, expires_field, None)
        if current_token and current_expiry and current_expiry > now:
            return None

        token = uuid4().hex
        expires_at = now + timedelta(seconds=lease_seconds)
        setattr(task, token_field, token)
        setattr(task, expires_field, expires_at)
        task.save(update_fields=[token_field, expires_field, "updated_at"])
        return ScheduleClaim(task=task, token=token, expires_at=expires_at)


def load_claimed_task(
    model,
    task_id,
    token: str,
    *,
    token_field: str = "scheduler_lease_token",
    expires_field: str = "scheduler_lease_expires_at",
    require_active: bool = False,
):
    """Load a claimed task under a row lock and enforce its fencing token."""

    if not token:
        raise ScheduleClaimLost("scheduler lease token is required")
    with transaction.atomic():
        task = model.objects.select_for_update().filter(pk=task_id).first()
        if task is None:
            raise ScheduleClaimLost("scheduled task no longer exists")
        if require_active and getattr(task, "status", None) != "ACTIVE":
            raise ScheduleClaimLost("scheduled task is no longer active")
        if getattr(task, token_field, "") != token:
            raise ScheduleClaimLost("scheduler lease token was superseded")
        expires_at = getattr(task, expires_field, None)
        if expires_at is None or expires_at <= timezone.now():
            raise ScheduleClaimLost("scheduler lease expired")
        return task


def clear_claim(
    task,
    *,
    token: str,
    token_field: str = "scheduler_lease_token",
    expires_field: str = "scheduler_lease_expires_at",
):
    """Clear a claim only when the caller still owns its fencing token."""

    if not token or getattr(task, token_field, "") != token:
        raise ScheduleClaimLost("scheduler lease token was superseded")
    setattr(task, token_field, "")
    setattr(task, expires_field, None)
    return task


__all__ = [
    "ScheduleClaim",
    "ScheduleClaimLost",
    "claim_due_task",
    "clear_claim",
    "load_claimed_task",
]
