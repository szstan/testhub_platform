"""Project-scoped notification intents and durable outbox dispatch.

Emission only writes an immutable, deduplicated intent. The on-commit callback
and scheduler send broker messages; provider I/O belongs to notification_tasks.
"""

from __future__ import annotations

import hashlib
import logging
from functools import partial
from typing import Any, Mapping

from celery import current_app
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import ExecutionNotificationEvent, ExecutionNotificationSubscription
from .result_events import redact_result_payload

logger = logging.getLogger(__name__)

NOTIFICATION_TASK = 'apps.executions.notification_tasks.deliver_execution_notification'
MAX_DELIVERY_ATTEMPTS = 5


def _safe_event_key(event_key: str) -> str:
    raw_key = str(event_key)
    if len(raw_key) > 255 or redact_result_payload(raw_key) != raw_key:
        return 'sha256:' + hashlib.sha256(raw_key.encode('utf-8')).hexdigest()
    return raw_key


def _enqueue_notification(event_id: int) -> bool:
    try:
        # send_task deliberately bypasses Celery's eager execution: even an
        # on-commit callback must never perform synchronous HTTP/SMTP work.
        current_app.send_task(NOTIFICATION_TASK, args=[event_id], retry=False)
        return True
    except Exception as exc:  # noqa: BLE001 - committed intent remains recoverable
        error = str(redact_result_payload(str(exc)))[:4000]
        ExecutionNotificationEvent.objects.filter(
            pk=event_id, status__in=('pending', 'failed'), delivery_claim_id='',
        ).update(error_message=error)
        logger.warning('notification broker enqueue failed: event_id=%s', event_id)
        return False


def emit_project_notification(
    project,
    event_type: str,
    event_key: str,
    payload: Mapping[str, Any] | None = None,
) -> list[ExecutionNotificationEvent]:
    """Persist one intent per subscription/key; enqueue only after commit.

    Repeated emissions leave the original payload, delivery lease, retry delay,
    and terminal result intact. A broker outage does not roll back the caller's
    business transaction; the scheduler recovers the pending intent.
    """

    safe_payload = redact_result_payload(dict(payload or {}))
    stored_key = _safe_event_key(event_key)
    subscriptions = ExecutionNotificationSubscription.objects.filter(
        project=project, event_type=event_type, enabled=True, channel__enabled=True,
    )
    results = []
    for subscription in subscriptions:
        with transaction.atomic():
            event, created = ExecutionNotificationEvent.objects.get_or_create(
                subscription=subscription,
                event_key=stored_key,
                defaults={
                    'project': project,
                    'event_type': event_type,
                    'payload': safe_payload,
                    'status': 'pending',
                },
            )
            if created:
                transaction.on_commit(partial(_enqueue_notification, event.pk))
            results.append(event)
    return results


def dispatch_pending_notifications(*, limit=100) -> int:
    """Enqueue a bounded batch of due or abandoned intents without provider I/O.

    Duplicate broker messages are harmless while a delivery claim is live.
    Failed deliveries with a null next_attempt_at are terminal and stay visible.
    """

    if transaction.get_connection().in_atomic_block:
        raise RuntimeError('Notification outbox dispatch must run outside a database transaction')
    now = timezone.now()
    due = Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    eligible = (
        (Q(status='pending') & due)
        | Q(status='failed', next_attempt_at__lte=now, delivery_attempts__lt=MAX_DELIVERY_ATTEMPTS)
        | (Q(status='dispatching') & (
            Q(delivery_claim_expires_at__isnull=True) | Q(delivery_claim_expires_at__lte=now)
        ))
    )
    event_ids = list(
        ExecutionNotificationEvent.objects.filter(eligible)
        .order_by('created_at', 'pk').values_list('pk', flat=True)[:max(0, min(int(limit), 1000))]
    )
    return sum(_enqueue_notification(event_id) for event_id in event_ids)


def notify_gate_failed(project, event_key: str, payload: Mapping[str, Any]):
    return emit_project_notification(project, 'gate_failed', event_key, payload)


def notify_integration_failed(project, event_key: str, payload: Mapping[str, Any]):
    return emit_project_notification(project, 'integration_failed', event_key, payload)


def notify_failure_reopened(project, event_key: str, payload: Mapping[str, Any]):
    return emit_project_notification(project, 'failure_reopened', event_key, payload)


def notify_quarantine_expiring(project, event_key: str, payload: Mapping[str, Any]):
    return emit_project_notification(project, 'quarantine_expiring', event_key, payload)


__all__ = [
    'dispatch_pending_notifications',
    'emit_project_notification',
    'notify_gate_failed',
    'notify_integration_failed',
    'notify_failure_reopened',
    'notify_quarantine_expiring',
]
