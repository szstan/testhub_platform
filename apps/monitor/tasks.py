"""Durable, leased monitor checks dispatched by the unified scheduler."""

import logging
from datetime import timedelta
from uuid import uuid4

from celery import shared_task
from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.core.scheduler import ScheduleClaimLost, ScheduleLeaseHeartbeat, load_claimed_task
from .models import MonitorTarget

logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=240, time_limit=270, ignore_result=True)
def execute_monitor_check(self, target_id, scheduler_token, lease_seconds=300):
    """Only the current live lease may persist probe results or alerts."""
    heartbeat = None
    close_old_connections()
    try:
        with transaction.atomic():
            target = load_claimed_task(
                MonitorTarget, target_id, scheduler_token, require_active=True,
                status_field='enabled', active_status=True,
            )
            # Convert delivery authority to worker authority exactly once. A
            # duplicate broker delivery still carries the old dispatch token.
            scheduler_token = uuid4().hex
            target.scheduler_lease_token = scheduler_token
            target.save(update_fields=['scheduler_lease_token', 'updated_at'])
        heartbeat = ScheduleLeaseHeartbeat(
            MonitorTarget, target_id, scheduler_token, lease_seconds=lease_seconds,
            status_field='enabled', active_status=True,
        )
        heartbeat.start()
        log = target.run_check(scheduler_token=scheduler_token)
        return {'status': 'checked', 'log_id': log.pk}
    except ScheduleClaimLost:
        return {'status': 'superseded'}
    except Exception as exc:
        # A transient probe/persistence failure must not hot-loop on every poll.
        # Do not log probe configuration, responses, or credentials.
        logger.error('monitor check failed: target_id=%s error_type=%s', target_id, type(exc).__name__)
        MonitorTarget.objects.filter(
            pk=target_id, scheduler_lease_token=scheduler_token,
            scheduler_lease_expires_at__gt=timezone.now(),
        ).update(next_check_at=timezone.now() + timedelta(seconds=60))
        return {'status': 'failed'}
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        # An old worker is never allowed to clear a successor's lease.
        MonitorTarget.objects.filter(pk=target_id, scheduler_lease_token=scheduler_token).update(
            scheduler_lease_token='', scheduler_lease_expires_at=None,
        )
        close_old_connections()
