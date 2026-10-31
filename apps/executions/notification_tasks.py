"""Leased notification delivery outside business/database transactions.

The provider and database do not share a commit protocol. If a worker dies
following provider acceptance, lease recovery may deliver again (at least once).
A live lease suppresses duplicate tasks and fenced completion rejects stale
workers; no exactly-once provider delivery is claimed.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from celery import shared_task
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.monitor.utils.notifiers import send_via_channel

from .models import ExecutionNotificationEvent
from .notifications import MAX_DELIVERY_ATTEMPTS
from .result_events import redact_result_payload

DELIVERY_LEASE_SECONDS = 120
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 1800


@dataclass(frozen=True)
class DeliveryClaim:
    event_id: int
    claim_id: str
    attempts: int
    event_type: str
    payload: Any
    channel: Any


def _claim_delivery(event_id: int):
    with transaction.atomic():
        event = ExecutionNotificationEvent.objects.select_for_update().filter(pk=event_id).first()
        if event is None:
            return None, 'missing'
        now = timezone.now()
        if event.status == 'sent':
            return None, 'sent'
        if event.status == 'dispatching':
            if event.delivery_claim_expires_at and event.delivery_claim_expires_at > now:
                return None, 'busy'
        elif event.status in {'pending', 'failed'}:
            if event.status == 'failed' and event.next_attempt_at is None:
                return None, 'failed'
            if event.next_attempt_at and event.next_attempt_at > now:
                return None, 'deferred'
        else:
            return None, 'skipped'

        subscription = event.subscription
        channel = subscription.channel
        invalid = (
            not subscription.enabled or not channel.enabled
            or subscription.project_id != event.project_id
            or subscription.event_type != event.event_type
        )
        if invalid or event.delivery_attempts >= MAX_DELIVERY_ATTEMPTS:
            event.status = 'failed'
            event.delivery_claim_id = ''
            event.delivery_claim_expires_at = None
            event.next_attempt_at = None
            event.error_message = (
                'notification subscription disabled or changed'
                if invalid else 'notification delivery attempts exhausted'
            )
            event.save(update_fields=[
                'status', 'delivery_claim_id', 'delivery_claim_expires_at',
                'next_attempt_at', 'error_message',
            ])
            return None, 'failed'

        event.status = 'dispatching'
        event.delivery_claim_id = uuid.uuid4().hex
        event.delivery_claim_expires_at = now + timedelta(seconds=DELIVERY_LEASE_SECONDS)
        event.delivery_attempts += 1
        event.next_attempt_at = None
        # Redact again to protect old intents written before the outbox existed.
        event.payload = redact_result_payload(event.payload)
        event.error_message = ''
        event.save(update_fields=[
            'status', 'delivery_claim_id', 'delivery_claim_expires_at',
            'delivery_attempts', 'next_attempt_at', 'payload', 'error_message',
        ])
        return DeliveryClaim(
            event.pk, event.delivery_claim_id, event.delivery_attempts,
            event.event_type, event.payload, channel,
        ), 'dispatching'


def _complete_delivery(claim: DeliveryClaim, *, success: bool, error='', terminal=False):
    now = timezone.now()
    next_attempt_at = None
    if not success and not terminal and claim.attempts < MAX_DELIVERY_ATTEMPTS:
        delay = min(RETRY_BASE_SECONDS * 2 ** (claim.attempts - 1), RETRY_MAX_SECONDS)
        next_attempt_at = now + timedelta(seconds=delay)
    updated = ExecutionNotificationEvent.objects.filter(
        pk=claim.event_id,
        status='dispatching',
        delivery_claim_id=claim.claim_id,
        delivery_claim_expires_at__gt=now,
    ).update(
        status='sent' if success else 'failed',
        sent_at=now if success else None,
        error_message='' if success else str(redact_result_payload(str(error)))[:4000],
        next_attempt_at=next_attempt_at,
        delivery_claim_id='',
        delivery_claim_expires_at=None,
    )
    return 'sent' if updated and success else 'failed' if updated else 'stale'


@shared_task(
    acks_late=True, reject_on_worker_lost=True, soft_time_limit=60, time_limit=90,
)
def deliver_execution_notification(event_id: int):
    """Claim briefly, deliver without locks, then complete only the live claim."""

    if transaction.get_connection().in_atomic_block:
        raise RuntimeError('Notification delivery must run outside a database transaction')
    claim, status = _claim_delivery(event_id)
    if claim is None:
        return {'event_id': event_id, 'status': status}

    # Recheck current subscription scope immediately before external I/O. A
    # queued intent must not retain authorization after its subscription changes.
    authorized = ExecutionNotificationEvent.objects.filter(
        pk=claim.event_id,
        status='dispatching',
        delivery_claim_id=claim.claim_id,
        delivery_claim_expires_at__gt=timezone.now(),
        project_id=F('subscription__project_id'),
        event_type=F('subscription__event_type'),
        subscription__enabled=True,
        subscription__channel__enabled=True,
        subscription__channel_id=claim.channel.pk,
    ).exists()
    if not authorized:
        status = _complete_delivery(
            claim, success=False, error='notification subscription disabled or changed', terminal=True,
        )
    else:
        try:
            message = json.dumps(claim.payload, ensure_ascii=False, default=str)
            ok, detail = send_via_channel(
                claim.channel, message=message,
                subject=f'TestHub {redact_result_payload(claim.event_type)}',
            )
        except Exception as exc:  # noqa: BLE001 - provider failure is isolated from the business event
            ok, detail = False, str(exc)
        status = _complete_delivery(
            claim, success=bool(ok), error=detail or 'notification delivery failed',
        )
    return {'event_id': event_id, 'status': status, 'attempts': claim.attempts}


__all__ = ['deliver_execution_notification']
