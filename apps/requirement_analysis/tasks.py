"""Background tasks for requirement analysis and testcase generation."""

import logging
import uuid
from datetime import timedelta

from asgiref.sync import async_to_sync
from celery import shared_task
from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from .models import RequirementDocument
from .services import RequirementAnalysisService

logger = logging.getLogger(__name__)
GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS = 7800
GENERATION_TASK_TIME_LIMIT_SECONDS = 7860
GENERATION_STALE_RECOVERY_GRACE_SECONDS = 300
MIN_GENERATION_STALE_AFTER_SECONDS = (
    GENERATION_TASK_TIME_LIMIT_SECONDS + GENERATION_STALE_RECOVERY_GRACE_SECONDS
)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def process_requirement_document_task(self, document_id):
    document = RequirementDocument.objects.get(pk=document_id)
    return async_to_sync(RequirementAnalysisService.process_document_analysis)(document)


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
