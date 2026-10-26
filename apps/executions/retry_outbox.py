"""Durable consumer for the unified execution retry outbox.

A retry row is only marked ``dispatched`` after a broker send succeeds. The
consumer never executes a process-local callback, so queued rows survive web
process restarts. Production must configure a source route; without one the
row is marked ``failed`` with an explicit operator-visible reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from celery import current_app, shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import ExecutionRetryRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryDispatchReceipt:
    request_id: int
    status: str
    dispatch_id: str = ''
    error: str = ''


def _routes():
    configured = getattr(settings, 'EXECUTION_RETRY_DISPATCH_ROUTES', {})
    if isinstance(configured, dict):
        return configured
    routes = {}
    for item in str(configured or '').split(','):
        source, separator, task_name = item.partition('=')
        if separator and source.strip() and task_name.strip():
            routes[source.strip()] = task_name.strip()
    return routes


def _route_name(source_type):
    route = _routes().get(source_type)
    if isinstance(route, dict):
        route = route.get('task')
    return str(route or '').strip()


def dispatch_retry_request(request_id: int, *, dispatcher=None, now: datetime | None = None):
    """Consume one due outbox row with row-lock fencing and idempotency."""

    now = now or timezone.now()
    with transaction.atomic():
        retry_request = (
            ExecutionRetryRequest.objects.select_for_update()
            .select_related('job', 'to_attempt')
            .filter(pk=request_id)
            .first()
        )
        if retry_request is None:
            return RetryDispatchReceipt(request_id=request_id, status='missing')
        if retry_request.status != 'queued':
            return RetryDispatchReceipt(request_id=request_id, status=retry_request.status)
        if retry_request.scheduled_at > now:
            return RetryDispatchReceipt(request_id=request_id, status='deferred')

        job = retry_request.job
        attempt = retry_request.to_attempt
        try:
            if dispatcher is not None:
                dispatch_id = dispatcher(job, attempt, retry_request)
            else:
                task_name = _route_name(job.source_type)
                if not task_name:
                    raise RuntimeError(
                        f'No durable dispatcher route configured for source type {job.source_type!r}'
                    )
                async_result = current_app.send_task(
                    task_name,
                    args=[job.pk, attempt.pk],
                    kwargs={'retry_request_id': retry_request.pk},
                    task_id=retry_request.idempotency_key,
                )
                dispatch_id = getattr(async_result, 'id', '') or ''

            retry_request.status = 'dispatched'
            retry_request.dispatched_at = now
            retry_request.error_message = ''
            retry_request.save(update_fields=['status', 'dispatched_at', 'error_message', 'updated_at'])
            return RetryDispatchReceipt(
                request_id=request_id,
                status='dispatched',
                dispatch_id=str(dispatch_id or ''),
            )
        except Exception as exc:  # noqa: BLE001 - failed rows are visible and retryable by operator
            message = str(exc)[:4000]
            retry_request.status = 'failed'
            retry_request.error_message = message
            retry_request.save(update_fields=['status', 'error_message', 'updated_at'])
            logger.error('execution retry outbox dispatch failed: request_id=%s error=%s', request_id, message)
            return RetryDispatchReceipt(request_id=request_id, status='failed', error=message)


def consume_retry_outbox(*, limit=100, now=None):
    """Consume due queued rows; concurrent consumers safely skip claimed rows."""

    now = now or timezone.now()
    limit = max(int(limit), 1)
    request_ids = list(
        ExecutionRetryRequest.objects.filter(
            status='queued',
            scheduled_at__lte=now,
        ).order_by('scheduled_at', 'pk').values_list('pk', flat=True)[:limit]
    )
    return [dispatch_retry_request(request_id, now=now) for request_id in request_ids]


@shared_task(bind=True, ignore_result=False)
def consume_execution_retry_outbox(self, request_id=None, limit=100):
    """Celery entry point for a scheduled retry-outbox consumer."""

    if request_id is not None:
        receipt = dispatch_retry_request(int(request_id))
        return receipt.__dict__
    return [receipt.__dict__ for receipt in consume_retry_outbox(limit=limit)]


__all__ = [
    'RetryDispatchReceipt',
    'consume_execution_retry_outbox',
    'consume_retry_outbox',
    'dispatch_retry_request',
]
