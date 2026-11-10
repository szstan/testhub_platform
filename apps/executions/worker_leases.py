"""Database-fenced ownership and bounded recovery for unified source workers."""

from __future__ import annotations

import logging
from datetime import timedelta
from threading import Event, Thread
from uuid import uuid4

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Case, F, OuterRef, Q, Subquery, When
from django.utils import timezone

from .models import ExecutionAttempt, ExecutionJob
from .state_machine import AttemptStatus, ExecutionStatus

logger = logging.getLogger(__name__)
ACTIVE = (AttemptStatus.PENDING, AttemptStatus.QUEUED, AttemptStatus.RUNNING, AttemptStatus.CANCEL_REQUESTED)


class ExecutionClaimLost(RuntimeError):
    pass


def lease_seconds():
    return max(30, int(getattr(settings, 'EXECUTION_WORKER_LEASE_SECONDS', 300)))


def is_current_attempt(job, attempt, *, claim_id=None):
    """Variants have independent attempt counters, even within the same job."""
    if attempt.job_id != job.pk or job.final_attempt_id or job.is_terminal or attempt.is_terminal:
        return False
    latest_id = job.attempts.filter(variant_id=attempt.variant_id).order_by('-attempt_number').values_list('pk', flat=True).first()
    if latest_id != attempt.pk:
        return False
    if attempt.worker_claim_id:
        if claim_id != attempt.worker_claim_id:
            return False
        if not attempt.worker_claim_expires_at or attempt.worker_claim_expires_at <= timezone.now():
            return False
    elif claim_id:
        return False
    return True


def claim_attempt(job_id, attempt_id, task_id=''):
    """A delivery gets a fresh UUID; Celery's stable dispatch id is not ownership."""
    with transaction.atomic():
        job = ExecutionJob.objects.select_for_update().get(pk=job_id)
        attempt = ExecutionAttempt.objects.select_for_update().get(pk=attempt_id, job_id=job_id)
        if not is_current_attempt(job, attempt) or attempt.worker_claim_id:
            return ''
        if attempt.dispatch_id and task_id and attempt.dispatch_id != str(task_id):
            raise ExecutionClaimLost('worker task id does not match its dispatch reservation')
        now = timezone.now()
        attempt.worker_claim_id = uuid4().hex
        attempt.worker_claimed_at = now
        attempt.worker_heartbeat_at = now
        attempt.worker_claim_expires_at = now + timedelta(seconds=lease_seconds())
        attempt.save(update_fields=['worker_claim_id', 'worker_claimed_at', 'worker_heartbeat_at', 'worker_claim_expires_at', 'updated_at'])
        return attempt.worker_claim_id


def check_claim(job_id, attempt_id, claim_id, *, renew=False):
    if not renew:
        # Polling must observe fresh ownership at every action boundary, but it
        # does not mutate state and needs neither row locks nor large results.
        latest_single = ExecutionAttempt.objects.filter(
            job_id=OuterRef('job_id'), variant_id__isnull=True,
        ).order_by('-attempt_number').values('pk')[:1]
        latest_variant = ExecutionAttempt.objects.filter(
            job_id=OuterRef('job_id'), variant_id=OuterRef('variant_id'),
        ).order_by('-attempt_number').values('pk')[:1]
        ownership = (
            Q(worker_claim_id=claim_id, worker_claim_expires_at__gt=timezone.now())
            if claim_id else Q(worker_claim_id='')
        )
        attempt = ExecutionAttempt.objects.select_related('job').only(
            'id', 'job_id', 'variant_id', 'attempt_number', 'status',
            'worker_claim_id', 'worker_claim_expires_at',
            'job__id', 'job__status', 'job__final_attempt_id',
        ).annotate(
            current_attempt_id=Case(
                When(variant_id__isnull=True, then=Subquery(latest_single)),
                default=Subquery(latest_variant),
            ),
        ).filter(
            ownership, pk=attempt_id, job_id=job_id, pk__isnull=False,
            status__in=ACTIVE, job__status__in=ACTIVE,
            job__final_attempt_id__isnull=True,
        ).filter(pk=F('current_attempt_id')).first()
        if attempt is None:
            raise ExecutionClaimLost('execution worker ownership was lost or expired')
        if attempt.job.status == ExecutionStatus.CANCEL_REQUESTED:
            raise ExecutionClaimLost('execution cancellation requested')
        return attempt

    with transaction.atomic():
        job = ExecutionJob.objects.select_for_update().get(pk=job_id)
        attempt = ExecutionAttempt.objects.select_for_update().get(pk=attempt_id, job_id=job_id)
        if not is_current_attempt(job, attempt, claim_id=claim_id):
            raise ExecutionClaimLost('execution worker ownership was lost or expired')
        if job.status == ExecutionStatus.CANCEL_REQUESTED:
            raise ExecutionClaimLost('execution cancellation requested')
        now = timezone.now()
        attempt.worker_heartbeat_at = now
        attempt.worker_claim_expires_at = now + timedelta(seconds=lease_seconds())
        attempt.save(update_fields=['worker_heartbeat_at', 'worker_claim_expires_at', 'updated_at'])
        return attempt


class ExecutionLeaseHeartbeat:
    def __init__(self, job_id, attempt_id, claim_id, *, on_lost=None):
        self.job_id, self.attempt_id, self.claim_id = job_id, attempt_id, claim_id
        self.on_lost = on_lost
        self.interval_seconds = max(1, min(lease_seconds() // 3, 30))
        self._stop, self._lost = Event(), Event()
        self._thread = None

    def start(self):
        self.raise_if_lost()
        self._thread = Thread(target=self._run, daemon=True, name=f'execution-lease-{self.attempt_id}')
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def raise_if_lost(self):
        if self._lost.is_set():
            raise ExecutionClaimLost('execution worker lease was lost')
        try:
            check_claim(self.job_id, self.attempt_id, self.claim_id)
        except Exception:
            self._lost.set()
            raise

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            close_old_connections()
            try:
                check_claim(self.job_id, self.attempt_id, self.claim_id, renew=True)
            except Exception:
                # Fail closed on unavailable ownership as well as a stale UUID.
                self._lost.set()
                if self.on_lost is not None:
                    try:
                        self.on_lost()
                    except Exception:
                        logger.exception('could not stop source after execution lease loss: attempt=%s', self.attempt_id)
                return
            finally:
                close_old_connections()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.stop()


def reconcile_stale_execution_claims(limit=100):
    """End a crashed delivery; policy retry creates a NEW attempt, never reuses it."""
    from .result_events import ResultEventIngestor

    now = timezone.now()
    cutoff = now - timedelta(seconds=lease_seconds())
    stale = (
        Q(worker_claim_expires_at__lte=now)
        | Q(worker_claim_id='', dispatch_reserved_at__lte=cutoff)
        | (Q(worker_claim_id='', dispatch_reserved_at__isnull=True, updated_at__lte=cutoff) & ~Q(dispatch_id=''))
        | (Q(worker_claim_expires_at__isnull=True) & ~Q(worker_claim_id='') & Q(worker_claimed_at__lte=cutoff))
    )
    candidates = list(ExecutionAttempt.objects.filter(status__in=ACTIVE).filter(stale).order_by('pk').values_list('pk', flat=True)[:max(1, min(int(limit), 1000))])
    recovered = 0
    for attempt_id in candidates:
        with transaction.atomic():
            candidate = ExecutionAttempt.objects.filter(pk=attempt_id).first()
            if candidate is None:
                continue
            job = ExecutionJob.objects.select_for_update().get(pk=candidate.job_id)
            attempt = ExecutionAttempt.objects.select_for_update().filter(pk=attempt_id, status__in=ACTIVE).filter(stale).first()
            if attempt is None or job.final_attempt_id or job.is_terminal:
                continue
            latest = job.attempts.filter(variant_id=attempt.variant_id).order_by('-attempt_number').first()
            if latest.pk != attempt.pk:
                attempt.status = AttemptStatus.FAILED
                attempt.error_type = 'infrastructure'
                attempt.error_message = 'Superseded execution attempt expired'
                attempt.finished_at = now
                attempt.save(update_fields=['status', 'error_type', 'error_message', 'finished_at', 'updated_at'])
                continue
            # Reserve a recovery UUID inside the same lock. The old delivery's
            # late heartbeat/result cannot pass this fence, including same task id.
            attempt.worker_claim_id = uuid4().hex
            attempt.worker_claim_expires_at = now + timedelta(seconds=lease_seconds())
            attempt.save(update_fields=['worker_claim_id', 'worker_claim_expires_at', 'updated_at'])
            ResultEventIngestor.ingest(
                job, 'run_finished',
                {'attempt': attempt.attempt_number,
                 'status': 'cancelled' if job.status == ExecutionStatus.CANCEL_REQUESTED else 'failed',
                 'error_type': 'infrastructure', 'error_message': 'Execution worker lease expired'},
                idempotency_key=f'worker-expired:{attempt.pk}',
                worker_claim_id=attempt.worker_claim_id,
            )
            recovered += 1
    return recovered
