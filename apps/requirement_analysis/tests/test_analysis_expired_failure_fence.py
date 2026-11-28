"""An expired analysis owner cannot turn an unknown outcome into an AI retry."""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from celery.exceptions import Retry
from django.db import OperationalError, connection
from django.utils import timezone

from apps.requirement_analysis.models import AnalysisTask
from apps.requirement_analysis.services import RequirementAnalysisService
from apps.requirement_analysis.tasks import reconcile_analysis_tasks
from apps.requirement_analysis.tests.test_analysis_retries import ANALYSIS_RESULT, retry_document
from apps.requirement_analysis.tests.test_analysis_worker_recovery import deliver


pytestmark = pytest.mark.django_db


def test_provider_failure_after_lease_expiry_cannot_enqueue_another_processing_attempt(retry_document):
    document, _client = retry_document
    claim = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')

    async def expire_during_provider_call(_text, _title):
        await sync_to_async(
            lambda: AnalysisTask.objects.filter(pk=claim.pk).update(
                worker_lease_expires_at=timezone.now() - timedelta(seconds=1),
            ), thread_sensitive=True,
        )()
        raise RuntimeError('provider connection lost; result unknown')

    with patch(
        'apps.requirement_analysis.services.AIService.analyze_requirements',
        new_callable=AsyncMock, side_effect=expire_during_provider_call,
    ) as ai:
        with pytest.raises(RuntimeError, match='result unknown'):
            RequirementAnalysisService._process_document_analysis(document.pk, claim.task_id)
        claim.refresh_from_db()
        assert claim.status == 'running'
        assert claim.retry_attempt == 0
        assert claim.worker_invocation_nonce
        # The recovery path must settle unknown, not publish another AI attempt.
        with patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay') as publish:
            assert reconcile_analysis_tasks(limit=1) == 1
        publish.assert_not_called()
        ai.assert_awaited_once()
    claim.refresh_from_db()
    document.refresh_from_db()
    assert claim.status == 'failed'
    assert '结果未知' in claim.error_message
    assert claim.retry_attempt == 0
    assert document.status == 'failed'


def test_matching_nonce_does_not_authorize_failure_write_after_lease_expiry(retry_document):
    document, _client = retry_document
    claim = AnalysisTask.objects.create(
        document=document, task_id='expired-failure-owner', task_type='requirement_analysis',
        status='running', retry_attempt=1, progress=30,
        worker_invocation_nonce='expired-but-matching',
        worker_lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    RequirementAnalysisService._persist_analysis_failure(
        document.pk, claim.pk, 1, invocation_nonce='expired-but-matching',
    )
    claim.refresh_from_db()
    assert claim.status == 'running'
    assert claim.retry_attempt == 1
    assert claim.progress == 30
    assert claim.worker_invocation_nonce == 'expired-but-matching'


@pytest.mark.django_db(transaction=True)
def test_claim_commit_ack_recovery_uses_a_real_commit_and_a_fresh_invocation(retry_document):
    document, _client = retry_document
    claim = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    original = RequirementAnalysisService._claim_analysis_task
    committed_nonces = []

    def commit_before_ack_loss(*args, **kwargs):
        original(*args, **kwargs)
        # transaction=True means the claim really committed, rather than merely
        # releasing an inner savepoint inside pytest's wrapping transaction.
        assert not connection.in_atomic_block
        committed = AnalysisTask.objects.get(pk=claim.pk)
        assert committed.status == 'running'
        committed_nonces.append(committed.worker_invocation_nonce)
        raise OperationalError('claim COMMIT completed; acknowledgment lost')

    with patch.object(RequirementAnalysisService, '_claim_analysis_task', side_effect=commit_before_ack_loss):
        with patch('apps.requirement_analysis.tasks.process_requirement_document_task.retry', side_effect=Retry('resume')) as retry:
            with pytest.raises(Retry):
                deliver(0, (document.pk, claim.task_id, 0))
    receipt = retry.call_args.kwargs
    assert receipt['kwargs']['resume_nonce'] == committed_nonces[0]
    recovered_nonces = []

    async def analyze(_text, _title):
        nonce = await sync_to_async(
            lambda: AnalysisTask.objects.get(pk=claim.pk).worker_invocation_nonce,
            thread_sensitive=True,
        )()
        recovered_nonces.append(nonce)
        return ANALYSIS_RESULT

    with patch(
        'apps.requirement_analysis.services.AIService.analyze_requirements',
        new_callable=AsyncMock, side_effect=analyze,
    ) as ai:
        result = deliver(1, receipt['args'], receipt['kwargs'])
    ai.assert_awaited_once()
    assert result['status'] == 'completed'
    assert recovered_nonces[0] != committed_nonces[0]
    claim.refresh_from_db()
    assert claim.retry_attempt == 0
    assert claim.status == 'completed'
