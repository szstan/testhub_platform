"""Project-scoped, idempotent execution notification delivery."""

from __future__ import annotations

import json
from typing import Any, Mapping

from django.db import transaction
from django.utils import timezone

from apps.monitor.utils.notifiers import send_via_channel

from .models import (
    ExecutionNotificationEvent,
    ExecutionNotificationSubscription,
)
from .result_events import redact_result_payload


def emit_project_notification(
    project,
    event_type: str,
    event_key: str,
    payload: Mapping[str, Any] | None = None,
) -> list[ExecutionNotificationEvent]:
    """Deliver one project event to enabled subscriptions exactly once per channel."""

    safe_payload = redact_result_payload(dict(payload or {}))
    subscriptions = ExecutionNotificationSubscription.objects.filter(
        project=project,
        event_type=event_type,
        enabled=True,
        channel__enabled=True,
    ).select_related('channel')
    results = []
    for subscription in subscriptions:
        with transaction.atomic():
            event, created = ExecutionNotificationEvent.objects.get_or_create(
                project=project,
                subscription=subscription,
                event_key=str(event_key)[:255],
                defaults={
                    'event_type': event_type,
                    'payload': safe_payload,
                    'status': 'pending',
                },
            )
            if not created and event.status == 'sent':
                results.append(event)
                continue
            event.payload = safe_payload
            event.status = 'pending'
            event.error_message = ''
            try:
                title = f'TestHub {event_type}'
                message = json.dumps(safe_payload, ensure_ascii=False, default=str)
                ok, detail = send_via_channel(subscription.channel, message=message, subject=title)
                if ok:
                    event.status = 'sent'
                    event.sent_at = timezone.now()
                else:
                    event.status = 'failed'
                    event.error_message = str(detail or 'notification delivery failed')[:4000]
            except Exception as exc:  # noqa: BLE001 - notification failure is isolated from the gate
                event.status = 'failed'
                event.error_message = str(exc)[:4000]
            event.save(update_fields=['payload', 'status', 'error_message', 'sent_at'])
            results.append(event)
    return results
def notify_gate_failed(project, event_key: str, payload: Mapping[str, Any]):
    return emit_project_notification(project, 'gate_failed', event_key, payload)


def notify_integration_failed(project, event_key: str, payload: Mapping[str, Any]):
    return emit_project_notification(project, 'integration_failed', event_key, payload)


def notify_failure_reopened(project, event_key: str, payload: Mapping[str, Any]):
    return emit_project_notification(project, 'failure_reopened', event_key, payload)


def notify_quarantine_expiring(project, event_key: str, payload: Mapping[str, Any]):
    return emit_project_notification(project, 'quarantine_expiring', event_key, payload)


__all__ = [
    'emit_project_notification',
    'notify_gate_failed',
    'notify_integration_failed',
    'notify_failure_reopened',
    'notify_quarantine_expiring',
]
