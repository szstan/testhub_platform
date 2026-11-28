"""Heartbeat leases for UI workbench worker invocations."""

from __future__ import annotations

import logging
from threading import Event, Thread
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from apps.core.scheduler import ScheduleClaimLost

logger = logging.getLogger(__name__)
DEFAULT_WORKER_LEASE_SECONDS = 90


class WorkbenchWorkerLeaseLost(ScheduleClaimLost):
    """Raised when this worker invocation no longer owns its execution lease."""


class WorkbenchClaimRecoveryUnavailable(RuntimeError):
    """Raised when a rolled-back claim cannot be made eligible for safe redelivery."""


def recover_claim_after_error(
    model, execution_id, claim_token, worker_nonce, pending_status, owner_matches, original_error,
):
    """Resolve commit ambiguity or release only a still-unclaimed pending delivery."""
    try:
        current = model.objects.filter(pk=execution_id).first()
    except Exception as error:
        raise WorkbenchClaimRecoveryUnavailable(
            'Unable to inspect the UI workbench claim after a database error.'
        ) from error

    if owner_matches(current, claim_token, worker_nonce):
        return current

    if (
        current is None
        or str(current.dispatch_claim) != str(claim_token)
        or current.status != pending_status
        or current.worker_nonce is not None
        or current.cancel_requested_at is not None
    ):
        return None

    try:
        released = model.objects.filter(
            pk=execution_id,
            dispatch_claim=claim_token,
            status=pending_status,
            worker_nonce__isnull=True,
            cancel_requested_at__isnull=True,
        ).update(
            dispatch_enqueued_at=None,
            dispatch_delivery_token=None,
            dispatch_delivery_expires_at=None,
        )
    except Exception as error:
        raise WorkbenchClaimRecoveryUnavailable(
            'Unable to release the UI workbench dispatch after a rolled-back claim.'
        ) from error

    if released:
        return None

    try:
        current = model.objects.filter(pk=execution_id).first()
    except Exception as error:
        raise WorkbenchClaimRecoveryUnavailable(
            'Unable to confirm ownership after a failed UI workbench claim release.'
        ) from error
    if (
        current is not None
        and str(current.dispatch_claim) == str(claim_token)
        and current.status == pending_status
        and current.worker_nonce is None
        and current.cancel_requested_at is None
    ):
        raise WorkbenchClaimRecoveryUnavailable(
            'The pending UI workbench dispatch remains unavailable for redelivery.'
        ) from original_error
    return None


def worker_lease_seconds():
    try:
        value = int(getattr(settings, 'UI_WORKBENCH_WORKER_LEASE_SECONDS', DEFAULT_WORKER_LEASE_SECONDS))
    except (TypeError, ValueError):
        value = DEFAULT_WORKER_LEASE_SECONDS
    return max(value, 3)


class ExecutionWorkerLeaseHeartbeat:
    def __init__(self, model, execution_id, claim_token, worker_nonce, *, lease_seconds=None):
        self.model = model
        self.execution_id = execution_id
        self.claim_token = str(claim_token)
        self.worker_nonce = str(worker_nonce)
        self.lease_seconds = max(int(lease_seconds or worker_lease_seconds()), 3)
        self.interval = max(self.lease_seconds / 3, 1)
        self._stop = Event()
        self._lost = Event()
        self._thread = None

    def start(self):
        self._thread = Thread(
            target=self._run,
            name=f'ui-workbench-heartbeat-{self.execution_id}',
            daemon=True,
        )
        self._thread.start()
        return self

    def _heartbeat(self):
        now = timezone.now()
        updated = self.model.objects.filter(
            pk=self.execution_id,
            dispatch_claim=self.claim_token,
            worker_nonce=self.worker_nonce,
            status__in=('running', 'RUNNING'),
            cancel_requested_at__isnull=True,
            worker_lease_expires_at__gt=now,
        ).update(
            worker_heartbeat_at=now,
            worker_lease_expires_at=now + timedelta(seconds=self.lease_seconds),
        )
        if updated != 1:
            self._lost.set()
            return False
        return True

    def _run(self):
        try:
            while not self._stop.wait(self.interval):
                try:
                    if not self._heartbeat():
                        return
                except Exception:  # noqa: BLE001 - a missed heartbeat cannot retain ownership
                    logger.exception('UI workbench heartbeat failed execution_id=%s', self.execution_id)
                    self._lost.set()
                    return
        finally:
            close_old_connections()

    def raise_if_lost(self):
        if self._lost.is_set():
            raise WorkbenchWorkerLeaseLost('UI workbench worker lease was lost')

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)


__all__ = [
    'ExecutionWorkerLeaseHeartbeat',
    'WorkbenchClaimRecoveryUnavailable',
    'WorkbenchWorkerLeaseLost',
    'recover_claim_after_error',
    'worker_lease_seconds',
]
