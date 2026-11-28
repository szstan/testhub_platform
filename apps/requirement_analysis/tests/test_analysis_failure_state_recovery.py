"""A failed failure-state transaction must not strand an owned running claim."""
from unittest.mock import AsyncMock, patch

import pytest
from celery.exceptions import Retry
from django.db import OperationalError

from apps.requirement_analysis.models import AnalysisTask, RequirementAnalysis
from apps.requirement_analysis.services import RequirementAnalysisService
from apps.requirement_analysis.tasks import process_requirement_document_task as task
from apps.requirement_analysis.tests.test_analysis_retries import ANALYSIS_RESULT, retry_document


pytestmark = pytest.mark.django_db


def deliver(retries, args, kwargs=None):
    task.push_request(retries=retries)
    try:
        return task.run(*args, **(kwargs or {}))
    finally:
        task.pop_request()


@pytest.mark.parametrize('commit_before_error', [False, True])
def test_owner_receipt_recovers_failure_state_without_stealing_a_live_attempt(retry_document, commit_before_error):
    document, _client = retry_document
    claim = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    persist = RequirementAnalysisService._persist_analysis_failure

    def failed_persistence(*args, **kwargs):
        if commit_before_error:
            persist(*args, **kwargs)
        raise OperationalError('failure-state commit outcome unknown')

    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock,
               side_effect=[RuntimeError('transient processing failure'), ANALYSIS_RESULT]) as analyze:
        with patch.object(RequirementAnalysisService, '_persist_analysis_failure', side_effect=failed_persistence):
            with patch.object(task, 'retry', side_effect=Retry('scheduled recovery')) as retry:
                with pytest.raises(Retry):
                    deliver(0, (document.pk, claim.task_id, 0))
        options = retry.call_args.kwargs
        assert options['kwargs']['recover_attempt'] == 0
        assert options['kwargs']['recover_nonce']
        claim.refresh_from_db()
        assert claim.status == ('pending' if commit_before_error else 'running')
        if not commit_before_error:
            assert claim.worker_invocation_nonce == options['kwargs']['recover_nonce']

        if not commit_before_error:
            # A higher retry number alone does not prove the live owner's call ended.
            assert deliver(1, options['args'])['status'] == 'not_claimed'
            assert analyze.await_count == 1

        result = deliver(1, options['args'], options['kwargs'])
        duplicate = deliver(1, options['args'], options['kwargs'])
    assert result['status'] == duplicate['status'] == 'completed'
    assert analyze.await_count == 2
    claim.refresh_from_db()
    document.refresh_from_db()
    assert claim.retry_attempt == 1
    assert claim.status == 'completed'
    assert document.status == 'analyzed'
    assert RequirementAnalysis.objects.filter(document=document).count() == 1


def test_exhausted_attempt_recovers_metadata_after_database_outage_without_calling_ai_again(retry_document):
    document, _client = retry_document
    claim = AnalysisTask.objects.create(
        document=document, task_id='last-attempt-cleanup', task_type='requirement_analysis', retry_attempt=2,
    )
    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock,
               side_effect=RuntimeError('last processing attempt failed')) as analyze:
        with patch.object(RequirementAnalysisService, '_persist_analysis_failure', side_effect=OperationalError('database unavailable')):
            with patch.object(task, 'retry', side_effect=Retry('state recovery')) as retry:
                with pytest.raises(Retry):
                    deliver(0, (document.pk, claim.task_id, 2))
        recovery = retry.call_args.kwargs
        claim.refresh_from_db()
        assert claim.status == 'running'
        assert recovery['kwargs']['recover_attempt'] == 2
        assert recovery['kwargs']['recover_nonce']

        # Even a long outage keeps only metadata recovery retryable; the global
        # processing budget never restarts an AI call after attempt two.
        for retry_number in (1, 5, 10):
            with patch.object(RequirementAnalysisService, '_persist_analysis_failure', side_effect=OperationalError('still unavailable')):
                with patch.object(task, 'retry', side_effect=Retry('state recovery')) as retry:
                    with pytest.raises(Retry):
                        deliver(retry_number, recovery['args'], recovery['kwargs'])
            recovery = retry.call_args.kwargs
            assert recovery['kwargs']['recover_attempt'] == 2
            assert recovery['kwargs']['recover_nonce']
            assert recovery['countdown'] <= 60
            assert analyze.await_count == 1

        result = deliver(11, recovery['args'], recovery['kwargs'])
    assert result['status'] == 'not_claimed'
    analyze.assert_awaited_once()
    claim.refresh_from_db()
    document.refresh_from_db()
    assert claim.status == 'failed'
    assert document.status == 'failed'
    assert claim.retry_attempt == 2


def test_old_recovery_receipt_cannot_reset_a_newer_running_attempt(retry_document):
    document, _client = retry_document
    claim = AnalysisTask.objects.create(
        document=document, task_id='newer-owner', task_type='requirement_analysis',
        retry_attempt=1, status='running', worker_invocation_nonce='newer-owner-nonce',
    )
    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock) as analyze:
        result = deliver(1, (document.pk, claim.task_id, 0), {
            'recover_attempt': 0, 'recover_nonce': 'older-owner-nonce',
        })
    assert result['status'] == 'not_claimed'
    analyze.assert_not_awaited()
    claim.refresh_from_db()
    assert claim.status == 'running'
    assert claim.retry_attempt == 1


@pytest.mark.parametrize('fail_after_recovery', [False, True])
def test_committed_transition_and_long_metadata_outage_preserve_processing_budget(retry_document, fail_after_recovery):
    document, _client = retry_document
    claim = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    persist = RequirementAnalysisService._persist_analysis_failure

    def commit_then_lose_ack(*args, **kwargs):
        persist(*args, **kwargs)
        raise OperationalError('commit acknowledgement lost')

    responses = [RuntimeError('attempt zero failed')]
    if fail_after_recovery:
        responses.append(OperationalError('attempt one configuration read failed'))
    responses.append(ANALYSIS_RESULT)
    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock,
               side_effect=responses) as analyze:
        with patch.object(RequirementAnalysisService, '_persist_analysis_failure', side_effect=commit_then_lose_ack):
            with patch.object(task, 'retry', side_effect=Retry('recover committed transition')) as retry:
                with pytest.raises(Retry):
                    deliver(0, (document.pk, claim.task_id, 0))
        options = retry.call_args.kwargs
        claim.refresh_from_db()
        assert claim.status == 'pending'
        assert claim.retry_attempt == 1

        for retry_number in (1, 5, 10):
            with patch.object(RequirementAnalysisService, '_persist_analysis_failure', side_effect=OperationalError('database still unavailable')):
                with patch.object(task, 'retry', side_effect=Retry('metadata recovery')) as retry:
                    with pytest.raises(Retry):
                        deliver(retry_number, options['args'], options['kwargs'])
            options = retry.call_args.kwargs
            assert options['kwargs']['recover_attempt'] == 0
            assert options['kwargs']['recover_nonce']
            assert analyze.await_count == 1

        if fail_after_recovery:
            with patch.object(task, 'retry', side_effect=Retry('processing attempt two')) as retry:
                with pytest.raises(Retry):
                    deliver(11, options['args'], options['kwargs'])
            options = retry.call_args.kwargs
            assert options['kwargs'] == {}, 'settled processing must discard the old receipt'
            assert options['args'][2] + 12 == 2
            result = deliver(12, options['args'], options['kwargs'])
        else:
            result = deliver(11, options['args'], options['kwargs'])
    claim.refresh_from_db()
    document.refresh_from_db()
    assert result['status'] == 'completed'
    assert claim.status == 'completed'
    assert claim.retry_attempt == (2 if fail_after_recovery else 1)
    assert analyze.await_count == (3 if fail_after_recovery else 2)
    assert document.status == 'analyzed'
