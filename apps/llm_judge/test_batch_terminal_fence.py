"""Old Celery deliveries cannot restart a batch after it becomes terminal."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.llm_judge.models import JudgeBatch, JudgeBatchCaseResult, JudgeRecord
from apps.llm_judge.tasks import reconcile_stale_batches, score_batch_task
from apps.llm_judge.test_batch_regressions import _run_batch


@pytest.mark.django_db
@pytest.mark.parametrize('terminal_status', ['failed', 'partial'])
def test_old_normal_delivery_preserves_terminal_batch_until_explicit_resume(terminal_status):
    batch = JudgeBatch.objects.create(
        status=terminal_status,
        celery_task_id='terminal-original-task',
        cases_data=[{'question': 'first', 'answer': 'one'}, {'question': 'second', 'answer': 'two'}],
        total=2,
        scored=1,
        error_count=1,
        progress=50,
        error_message='previous attempt failed',
        completed_at=timezone.now(),
        worker_attempt_token='',
        worker_heartbeat_at=None,
        worker_lease_expires_at=None,
    )
    checkpoint = JudgeBatchCaseResult.objects.create(
        batch=batch, case_index=0, result={'error': 'first case failed'},
    )
    before = JudgeBatch.objects.values().get(pk=batch.pk)

    with patch('apps.llm_judge.service.JudgeService') as service:
        result = _run_batch(batch.pk, 'terminal-original-task')
        service.assert_not_called()

    assert result['status'] == terminal_status
    assert JudgeBatch.objects.values().get(pk=batch.pk) == before
    assert list(batch.case_results.values_list('pk', flat=True)) == [checkpoint.pk]
    assert not JudgeRecord.objects.filter(batch=batch).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('delivered_token', [None, 'exhausted-worker-token'])
def test_recovery_exhaustion_cannot_be_bypassed_by_old_worker_delivery(delivered_token):
    now = timezone.now()
    original_task_id = 'original-before-recovery-exhaustion'
    batch = JudgeBatch.objects.create(
        status='running',
        celery_task_id=original_task_id,
        cases_data=[{'question': 'must not be scored again', 'answer': 'answer'}],
        total=1,
        worker_attempt_token='exhausted-worker-token',
        worker_heartbeat_at=now - timedelta(minutes=3),
        worker_lease_expires_at=now - timedelta(seconds=1),
        worker_recovery_attempts=3,
    )
    with patch.object(score_batch_task, 'apply_async') as dispatch:
        assert reconcile_stale_batches(now=now) == 0
        dispatch.assert_not_called()
    before = JudgeBatch.objects.values().get(pk=batch.pk)
    assert before['status'] == 'failed'
    assert before['celery_task_id'] == ''

    with patch('apps.llm_judge.service.JudgeService') as service:
        result = _run_batch(batch.pk, original_task_id, delivered_token)
        service.assert_not_called()

    assert result['status'] == 'failed'
    assert JudgeBatch.objects.values().get(pk=batch.pk) == before
    assert not JudgeBatchCaseResult.objects.filter(batch=batch).exists()
    assert not JudgeRecord.objects.filter(batch=batch).exists()
