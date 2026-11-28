"""Background tasks for requirement analysis and testcase generation."""

import logging
import uuid
from datetime import timedelta

from asgiref.sync import async_to_sync
from celery import shared_task
from celery.exceptions import Retry
from django.conf import settings
from django.db import DatabaseError, close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from .models import AnalysisTask, RequirementDocument
from .services import (
    AnalysisClaimStateError,
    AnalysisRetryStateError,
    MAX_ANALYSIS_ATTEMPTS,
    RequirementAnalysisService,
    analysis_worker_lease_seconds,
)

logger = logging.getLogger(__name__)
GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS = 7800
GENERATION_TASK_TIME_LIMIT_SECONDS = 7860
GENERATION_STALE_RECOVERY_GRACE_SECONDS = 300
MIN_GENERATION_STALE_AFTER_SECONDS = (
    GENERATION_TASK_TIME_LIMIT_SECONDS + GENERATION_STALE_RECOVERY_GRACE_SECONDS
)
ANALYSIS_PUBLISH_LEASE_SECONDS = 120
MAX_ANALYSIS_RECONCILE_LIMIT = 100


def _publish_lease_seconds():
    try:
        configured = int(getattr(settings, 'REQUIREMENT_ANALYSIS_PUBLISH_LEASE_SECONDS', ANALYSIS_PUBLISH_LEASE_SECONDS))
    except (TypeError, ValueError):
        configured = ANALYSIS_PUBLISH_LEASE_SECONDS
    return max(1, configured)


def _reserve_analysis_dispatch(task_pk, *, now=None, recovery_only=False, expected_attempt=None):
    now = now or timezone.now()
    lease_expires_at = now + timedelta(seconds=_publish_lease_seconds())
    with transaction.atomic():
        task = AnalysisTask.objects.select_for_update().filter(pk=task_pk).first()
        if task is None or task.status != 'pending':
            return None
        if expected_attempt is not None and task.retry_attempt != expected_attempt:
            return None
        if recovery_only:
            recoverable = task.dispatch_state == 'unpublished' or (
                task.dispatch_state == 'publishing'
                and (task.dispatch_lease_expires_at is None or task.dispatch_lease_expires_at <= now)
            )
            if not recoverable:
                return None

        dispatch_nonce = uuid.uuid4().hex
        task.dispatch_state = 'publishing'
        task.dispatch_nonce = dispatch_nonce
        task.dispatch_lease_expires_at = lease_expires_at
        task.dispatch_acknowledged_at = None
        task.save(update_fields=[
            'dispatch_state', 'dispatch_nonce',
            'dispatch_lease_expires_at', 'dispatch_acknowledged_at',
        ])
        return {
            'task_pk': task.pk,
            'document_id': task.document_id,
            'task_id': task.task_id,
            'attempt': task.retry_attempt,
            'dispatch_nonce': dispatch_nonce,
        }


def _acknowledge_analysis_dispatch(task_pk, dispatch_nonce, now=None):
    now = now or timezone.now()
    return AnalysisTask.objects.filter(
        pk=task_pk,
        status='pending',
        dispatch_state='publishing',
        dispatch_nonce=dispatch_nonce,
    ).update(
        dispatch_state='published',
        dispatch_nonce='',
        dispatch_lease_expires_at=None,
        dispatch_acknowledged_at=now,
    ) == 1


def _publish_reserved_analysis(reservation):
    delivery = process_requirement_document_task.delay(
        reservation['document_id'], reservation['task_id'], reservation['attempt'],
    )
    _acknowledge_analysis_dispatch(reservation['task_pk'], reservation['dispatch_nonce'])
    return delivery


def dispatch_analysis_task(task_pk):
    """Publish a persisted pending claim and record the publisher acknowledgement."""
    reservation = _reserve_analysis_dispatch(task_pk)
    if reservation is None:
        return None
    return _publish_reserved_analysis(reservation)


def _retry_dispatch_reservation(task_pk, pending_attempt):
    return _reserve_analysis_dispatch(task_pk, expected_attempt=pending_attempt)


def _retry_with_fenced_dispatch(
    task, *, exc, args, kwargs, countdown, task_pk=None, pending_attempt=None,
):
    reservation = None
    if task_pk is not None and pending_attempt is not None:
        try:
            reservation = _retry_dispatch_reservation(task_pk, pending_attempt)
        except DatabaseError:
            logger.warning('Unable to persist analysis retry publication lease: task_pk=%s', task_pk)
    try:
        result = task.retry(exc=exc, args=args, kwargs=kwargs, countdown=countdown)
    except Retry:
        if reservation is not None:
            _acknowledge_analysis_dispatch(reservation['task_pk'], reservation['dispatch_nonce'])
        raise
    if reservation is not None:
        _acknowledge_analysis_dispatch(reservation['task_pk'], reservation['dispatch_nonce'])
    return result


@shared_task(bind=True, max_retries=None, acks_late=True, reject_on_worker_lost=True)
def process_requirement_document_task(
    self, document_id, analysis_task_id=None, base_attempt=0, *,
    recover_attempt=None, recover_nonce=None, resume_attempt=None,
    resume_nonce=None, metadata_attempt=None,
):
    retries = self.request.retries
    invocation_nonce = uuid.uuid4().hex
    if recover_attempt is not None:
        attempt = recover_attempt + 1
    elif resume_attempt is not None:
        attempt = resume_attempt
    elif metadata_attempt is not None:
        attempt = metadata_attempt
    else:
        attempt = base_attempt + retries

    try:
        document = RequirementDocument.objects.filter(pk=document_id).first()
        if document is None:
            return {'document_id': document_id, 'status': 'missing'}
        if analysis_task_id is None:
            claim = RequirementAnalysisService.get_or_create_analysis_task(document.pk)
            if claim is not None:
                analysis_task_id = claim.task_id
        analysis = async_to_sync(RequirementAnalysisService.process_document_analysis)(
            document,
            analysis_task_id=analysis_task_id,
            attempt=attempt,
            recover_attempt=recover_attempt,
            recover_nonce=recover_nonce,
            resume_attempt=resume_attempt,
            resume_nonce=resume_nonce,
            invocation_nonce=invocation_nonce,
        )
    except Exception as exc:
        processing_settled = getattr(exc, '_testhub_analysis_failure_settled', None) == attempt
        state_recovery = False
        retry_kwargs = {}
        pending_attempt = None

        if isinstance(exc, AnalysisClaimStateError):
            state_recovery = True
            retry_kwargs = {
                'resume_attempt': exc.attempt,
                'resume_nonce': exc.invocation_nonce,
            }
        elif isinstance(exc, AnalysisRetryStateError):
            state_recovery = True
            retry_kwargs = {
                'recover_attempt': exc.recovery_attempt,
                'recover_nonce': exc.recovery_nonce,
            }
            pending_attempt = exc.recovery_attempt + 1
        elif processing_settled:
            pending_attempt = attempt + 1 if attempt + 1 < MAX_ANALYSIS_ATTEMPTS else None
        elif recover_attempt is not None and isinstance(exc, DatabaseError):
            state_recovery = True
            retry_kwargs = {
                'recover_attempt': recover_attempt,
                'recover_nonce': recover_nonce,
            }
            pending_attempt = attempt
        elif isinstance(exc, DatabaseError):
            # A pre-claim outage has no owner receipt; an attempt index alone
            # cannot steal a running task because the claim CAS still requires pending.
            state_recovery = True
            retry_kwargs = {'metadata_attempt': attempt}
            pending_attempt = attempt

        if attempt >= MAX_ANALYSIS_ATTEMPTS - 1 and not state_recovery:
            raise
        if processing_settled:
            recover_attempt = None
            recover_nonce = None
            resume_attempt = None
            resume_nonce = None
            metadata_attempt = None
        next_base = base_attempt if state_recovery else attempt - retries
        task_pk = None
        if analysis_task_id and pending_attempt is not None:
            try:
                task_pk = AnalysisTask.objects.filter(task_id=analysis_task_id).values_list('pk', flat=True).first()
            except DatabaseError:
                logger.warning('Unable to locate pending requirement analysis for retry publication')
        return _retry_with_fenced_dispatch(
            self,
            exc=exc,
            args=(document_id, analysis_task_id, next_base),
            kwargs=retry_kwargs,
            countdown=min(60, 2 ** min(retries, 6)),
            task_pk=task_pk,
            pending_attempt=pending_attempt,
        )
    return {
        'document_id': document_id,
        'analysis_id': analysis.pk if analysis is not None else None,
        'status': 'completed' if analysis is not None else 'not_claimed',
    }


def reconcile_analysis_tasks(limit=10, now=None):
    """Recover unacknowledged pending deliveries and settle expired workers."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return 0
    limit = max(0, min(limit, MAX_ANALYSIS_RECONCILE_LIMIT))
    if limit == 0:
        return 0
    now = now or timezone.now()
    lease_cutoff = now - timedelta(seconds=analysis_worker_lease_seconds())
    handled = 0

    stale_running_ids = list(
        AnalysisTask.objects.filter(status='running').filter(
            Q(worker_lease_expires_at__lte=now)
            | Q(worker_lease_expires_at__isnull=True, started_at__lte=lease_cutoff)
            | Q(worker_lease_expires_at__isnull=True, started_at__isnull=True, created_at__lte=lease_cutoff)
        ).order_by('worker_lease_expires_at', 'pk').values_list('pk', flat=True)[:limit]
    )
    for task_pk in stale_running_ids:
        if RequirementAnalysisService.settle_expired_analysis_task(task_pk, now=now):
            handled += 1

    remaining = limit - handled
    if remaining <= 0:
        return handled
    pending_ids = list(
        AnalysisTask.objects.filter(status='pending').filter(
            Q(dispatch_state='unpublished')
            | Q(dispatch_state='publishing', dispatch_lease_expires_at__lte=now)
            | Q(dispatch_state='publishing', dispatch_lease_expires_at__isnull=True)
        ).order_by('created_at', 'pk').values_list('pk', flat=True)[:remaining]
    )
    for task_pk in pending_ids:
        try:
            reservation = _reserve_analysis_dispatch(task_pk, now=now, recovery_only=True)
            if reservation is None:
                continue
            handled += 1
            _publish_reserved_analysis(reservation)
        except Exception:
            logger.exception('Unable to recover pending requirement analysis: task_pk=%s', task_pk)
    return handled


def _publish_generation_task(task_pk, claim_id):
    return generate_test_case_task.apply_async(
        args=(task_pk, claim_id),
        task_id=claim_id,
        queue=getattr(settings, 'REQUIREMENT_GENERATION_QUEUE', 'celery'),
        retry=False,
    )


@shared_task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=GENERATION_TASK_TIME_LIMIT_SECONDS,
    ignore_result=True,
)
def generate_test_case_task(self, task_pk, claim_id):
    """Run one persisted testcase-generation task outside the web process."""
    from .generation_worker import execute_generation_task

    close_old_connections()
    try:
        if self.request.id and self.request.id != claim_id:
            logger.warning('Generation task request ID did not match its claim: task_id=%s', task_pk)
            return {'task_id': task_pk, 'status': 'superseded'}
        return execute_generation_task(task_pk, claim_id)
    finally:
        close_old_connections()


def dispatch_generation_task(task_pk):
    """Persist a broker claim before publishing the task, returning its ID."""
    from .models import TestCaseGenerationTask

    claim_id = uuid.uuid4().hex
    updated = TestCaseGenerationTask.objects.filter(
        pk=task_pk,
        status='pending',
    ).update(
        celery_task_id=claim_id,
        worker_claim_id='',
        error_message='',
        updated_at=timezone.now(),
    )
    if updated != 1:
        return None

    try:
        _publish_generation_task(task_pk, claim_id)
    except Exception as exc:
        logger.exception('Unable to queue testcase generation: task_id=%s', task_pk)
        TestCaseGenerationTask.objects.filter(
            pk=task_pk,
            status='pending',
            celery_task_id=claim_id,
        ).update(
            status='failed',
            error_message=f'Celery dispatch failed: {exc}',
            updated_at=timezone.now(),
        )
        return None
    return claim_id


def _recover_stale_generation_tasks():
    """Reset and redispatch tasks whose Celery worker stopped heartbeating."""
    from .models import TestCaseGenerationTask

    stale_seconds = max(
        int(getattr(settings, 'REQUIREMENT_GENERATION_STALE_AFTER_SECONDS', MIN_GENERATION_STALE_AFTER_SECONDS)),
        MIN_GENERATION_STALE_AFTER_SECONDS,
    )
    cutoff = timezone.now() - timedelta(seconds=stale_seconds)
    stale_ids = list(
        TestCaseGenerationTask.objects.filter(
            Q(status='pending') | Q(status__in=('generating', 'reviewing', 'revising')),
            updated_at__lt=cutoff,
        ).order_by('updated_at').values_list('pk', flat=True)[:100]
    )
    requeued = 0
    failed = 0

    for task_pk in stale_ids:
        claim_id = uuid.uuid4().hex
        with transaction.atomic():
            task = TestCaseGenerationTask.objects.select_for_update().filter(pk=task_pk).first()
            if not task or task.status not in ('pending', 'generating', 'reviewing', 'revising') or task.updated_at >= cutoff:
                continue
            task.status = 'pending'
            task.progress = 0
            task.stream_buffer = ''
            task.stream_position = 0
            task.last_stream_update = None
            task.generated_test_cases = ''
            task.review_feedback = ''
            task.final_test_cases = ''
            task.generation_log = ''
            task.error_message = ''
            task.celery_task_id = claim_id
            task.worker_claim_id = ''
            task.updated_at = timezone.now()
            task.save(update_fields=[
                'status', 'progress', 'stream_buffer', 'stream_position', 'last_stream_update',
                'generated_test_cases', 'review_feedback', 'final_test_cases', 'generation_log',
                'error_message', 'celery_task_id', 'worker_claim_id', 'updated_at',
            ])

        try:
            _publish_generation_task(task_pk, claim_id)
            requeued += 1
        except Exception as exc:
            logger.exception('Unable to redispatch stale testcase generation: task_id=%s', task_pk)
            TestCaseGenerationTask.objects.filter(
                pk=task_pk,
                status='pending',
                celery_task_id=claim_id,
            ).update(
                status='failed',
                error_message=f'Stale-task recovery dispatch failed: {exc}',
                updated_at=timezone.now(),
            )
            failed += 1

    return {'requeued': requeued, 'failed': failed}


@shared_task(
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=60,
    time_limit=90,
    ignore_result=True,
)
def recover_stale_generation_tasks():
    close_old_connections()
    try:
        return _recover_stale_generation_tasks()
    finally:
        close_old_connections()
