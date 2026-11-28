from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from celery.exceptions import Retry
from django.db import OperationalError, close_old_connections
from django.utils import timezone
from datetime import timedelta

from apps.requirement_analysis.models import AnalysisTask
from apps.requirement_analysis.services import RequirementAnalysisService
from apps.requirement_analysis.tasks import process_requirement_document_task, reconcile_analysis_tasks
from apps.requirement_analysis.tests.test_analysis_retries import ANALYSIS_RESULT, retry_document


pytestmark = pytest.mark.django_db


def deliver(retries, args, kwargs=None):
    process_requirement_document_task.push_request(retries=retries)
    try:
        return process_requirement_document_task.run(*args, **(kwargs or {}))
    finally:
        process_requirement_document_task.pop_request()


def test_claim_commit_ack_loss_resumes_only_with_matching_invocation_nonce(retry_document):
    document, _client = retry_document
    claim = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    claim_attempt = RequirementAnalysisService._claim_analysis_task

    def commit_then_lose_ack(*args, **kwargs):
        claim_attempt(*args, **kwargs)
        raise OperationalError('claim commit acknowledgement lost')

    with patch.object(RequirementAnalysisService, '_claim_analysis_task', side_effect=commit_then_lose_ack):
        with patch.object(process_requirement_document_task, 'retry', side_effect=Retry('resume claim')) as retry:
            with pytest.raises(Retry):
                deliver(0, (document.pk, claim.task_id, 0))

    retry_kwargs = retry.call_args.kwargs['kwargs']
    assert retry_kwargs['resume_attempt'] == 0
    assert retry_kwargs['resume_nonce']
    claim.refresh_from_db()
    assert claim.status == 'running'
    committed_nonce = claim.worker_invocation_nonce
    assert committed_nonce == retry_kwargs['resume_nonce']

    invocation_nonces = []

    async def analyze(_text, _title):
        current = await sync_to_async(
            lambda: AnalysisTask.objects.get(pk=claim.pk).worker_invocation_nonce,
            thread_sensitive=True,
        )()
        invocation_nonces.append(current)
        return ANALYSIS_RESULT

    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock, side_effect=analyze) as ai:
        result = deliver(1, retry.call_args.kwargs['args'], retry_kwargs)
    ai.assert_awaited_once()
    assert result['status'] == 'completed'
    assert invocation_nonces and invocation_nonces[0] != committed_nonce
    claim.refresh_from_db()
    assert claim.status == 'completed'
    assert claim.retry_attempt == 0


@pytest.mark.django_db(transaction=True)
def test_rolled_back_claim_receipt_cannot_adopt_another_live_worker(retry_document):
    document, _client = retry_document
    claim = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    with patch.object(
        RequirementAnalysisService, '_claim_analysis_task',
        side_effect=OperationalError('claim transaction rolled back'),
    ):
        with patch.object(process_requirement_document_task, 'retry', side_effect=Retry('retry claim')) as retry:
            with pytest.raises(Retry):
                deliver(0, (document.pk, claim.task_id, 0))

    receipt = retry.call_args.kwargs['kwargs']
    claim.refresh_from_db()
    assert claim.status == 'pending'
    assert receipt['resume_nonce'] != claim.worker_invocation_nonce

    started, release = Event(), Event()

    async def blocked_analysis(_text, _title):
        started.set()
        if not release.wait(timeout=10):
            raise TimeoutError('test did not release analysis')
        return ANALYSIS_RESULT

    def run_live_worker():
        close_old_connections()
        try:
            return deliver(0, (document.pk, claim.task_id, 0))
        finally:
            close_old_connections()

    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock, side_effect=blocked_analysis) as ai:
        with ThreadPoolExecutor(max_workers=1) as pool:
            active = pool.submit(run_live_worker)
            try:
                assert started.wait(timeout=5)
                duplicate = deliver(1, retry.call_args.kwargs['args'], receipt)
                assert duplicate['status'] == 'not_claimed'
                release.set()
                assert active.result(timeout=10)['status'] == 'completed'
            finally:
                release.set()
    ai.assert_awaited_once()


@pytest.mark.django_db(transaction=True)
def test_stale_owner_cannot_finalize_after_ownership_token_changes(retry_document):
    document, _client = retry_document
    claim = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    started, release = Event(), Event()

    async def blocked_analysis(_text, _title):
        started.set()
        if not release.wait(timeout=10):
            raise TimeoutError('test did not release analysis')
        return ANALYSIS_RESULT

    def run_worker():
        close_old_connections()
        try:
            return process_requirement_document_task.run(document.pk, claim.task_id, 0)
        finally:
            close_old_connections()

    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock, side_effect=blocked_analysis) as ai:
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(run_worker)
            try:
                assert started.wait(timeout=5)
                claim.refresh_from_db()
                original_nonce = claim.worker_invocation_nonce
                AnalysisTask.objects.filter(pk=claim.pk).update(
                    worker_invocation_nonce='newer-owner',
                    worker_heartbeat_at=timezone.now(),
                    worker_lease_expires_at=timezone.now() + timedelta(minutes=5),
                )
                release.set()
                assert worker.result(timeout=10)['status'] == 'not_claimed'
            finally:
                release.set()
    ai.assert_awaited_once()
    assert original_nonce
    assert not AnalysisTask.objects.filter(document=document, status='completed').exists()


def test_reconciler_recovers_unpublished_claim_but_leaves_acknowledged_queue_waiting(retry_document):
    document, _client = retry_document
    now = timezone.now()
    acknowledged = AnalysisTask.objects.create(
        document=document,
        task_id='acknowledged-queued-analysis',
        task_type='requirement_analysis',
        status='pending',
        dispatch_state='published',
        dispatch_acknowledged_at=now - timedelta(days=3),
    )
    unpublished = AnalysisTask.objects.create(
        document=document,
        task_id='committed-before-publish-analysis',
        task_type='requirement_analysis',
        status='pending',
        retry_attempt=1,
        dispatch_state='unpublished',
    )

    with patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay', return_value=object()) as publish:
        assert reconcile_analysis_tasks(limit=1, now=now) == 1
    publish.assert_called_once_with(document.pk, unpublished.task_id, 1)
    acknowledged.refresh_from_db()
    unpublished.refresh_from_db()
    assert acknowledged.status == 'pending'
    assert acknowledged.dispatch_state == 'published'
    assert unpublished.dispatch_state == 'published'
    assert unpublished.dispatch_acknowledged_at is not None


def test_expired_running_analysis_settles_unknown_without_ai_replay_and_can_be_explicitly_retried(retry_document):
    document, client = retry_document
    claim = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    now = timezone.now()
    AnalysisTask.objects.filter(pk=claim.pk).update(
        status='running',
        started_at=now - timedelta(hours=1),
        worker_invocation_nonce='lost-worker',
        worker_heartbeat_at=now - timedelta(hours=1),
        worker_lease_expires_at=now - timedelta(seconds=1),
    )
    document.status = 'analyzing'
    document.save(update_fields=['status', 'updated_at'])

    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock) as ai:
        assert reconcile_analysis_tasks(limit=10, now=now) == 1
        ai.assert_not_awaited()
    claim.refresh_from_db()
    document.refresh_from_db()
    assert claim.status == 'failed'
    assert '结果未知' in claim.error_message
    assert not claim.worker_invocation_nonce
    assert document.status == 'failed'

    with patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay', return_value=type('Delivery', (), {'id': 'explicit-retry'})()):
        response = client.post(f'/api/requirement-analysis/documents/{document.pk}/analyze/', {}, format='multipart')
    assert response.status_code == 202, response.data
    assert response.data['analysis_task_id'] != claim.task_id


def test_worker_task_uses_late_ack_and_worker_loss_redelivery():
    assert process_requirement_document_task.acks_late is True
    assert process_requirement_document_task.reject_on_worker_lost is True


def test_worker_heartbeat_is_nonce_and_lease_fenced_and_tokens_are_not_serialized(retry_document):
    from apps.requirement_analysis.serializers import AnalysisTaskSerializer

    document, _client = retry_document
    now = timezone.now()
    claim = AnalysisTask.objects.create(
        document=document,
        task_id='heartbeat-fence',
        task_type='requirement_analysis',
        status='running',
        retry_attempt=1,
        worker_invocation_nonce='current-owner',
        worker_heartbeat_at=now,
        worker_lease_expires_at=now + timedelta(minutes=1),
        dispatch_nonce='publisher-secret',
    )
    assert not RequirementAnalysisService.heartbeat_analysis_task(
        claim.pk, 1, 'older-owner', now=now,
    )
    assert RequirementAnalysisService.heartbeat_analysis_task(
        claim.pk, 1, 'current-owner', now=now,
    )
    claim.refresh_from_db()
    assert claim.worker_lease_expires_at > now + timedelta(minutes=1)
    serialized = AnalysisTaskSerializer(claim).data
    assert not {
        'worker_invocation_nonce', 'worker_heartbeat_at', 'worker_lease_expires_at',
        'dispatch_state', 'dispatch_nonce', 'dispatch_lease_expires_at',
        'dispatch_acknowledged_at',
    }.intersection(serialized)
