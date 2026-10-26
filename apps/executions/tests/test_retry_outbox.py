from unittest.mock import Mock, patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.executions.models import ExecutionJob, ExecutionRetryRequest
from apps.executions.retry_outbox import dispatch_retry_request
from apps.projects.models import Project
from apps.users.models import User


@pytest.fixture
def retry_request(db):
    owner = User.objects.create_user(username='retry-consumer-owner', password='pass')
    project = Project.objects.create(name='Retry Consumer Project', owner=owner)
    job = ExecutionJob.objects.create(
        project=project,
        source_type='api',
        source_id='retry-case',
        requested_by=owner,
        status='queued',
    )
    from_attempt, _ = job.create_attempt()
    from_attempt.transition_to('queued')
    from_attempt.transition_to('running')
    from_attempt.transition_to('failed')
    to_attempt, _ = job.create_attempt()
    to_attempt.transition_to('queued')
    return ExecutionRetryRequest.objects.create(
        job=job,
        from_attempt=from_attempt,
        to_attempt=to_attempt,
        idempotency_key='retry-consumer-key',
        reason='automatic retry',
        scheduled_at=timezone.now(),
    )


@pytest.mark.django_db
def test_retry_consumer_fails_closed_without_a_durable_route(retry_request):
    receipt = dispatch_retry_request(retry_request.pk)

    assert receipt.status == 'failed'
    retry_request.refresh_from_db()
    assert retry_request.status == 'failed'
    assert 'No durable dispatcher route' in retry_request.error_message


@pytest.mark.django_db
def test_retry_consumer_marks_dispatched_only_after_broker_send(retry_request):
    async_result = Mock(id='broker-task-id')
    with override_settings(EXECUTION_RETRY_DISPATCH_ROUTES={'api': 'worker.retry'}):
        with patch('apps.executions.retry_outbox.current_app.send_task', return_value=async_result) as send_task:
            receipt = dispatch_retry_request(retry_request.pk)

    assert receipt.status == 'dispatched'
    assert receipt.dispatch_id == 'broker-task-id'
    send_task.assert_called_once_with(
        'worker.retry',
        args=[retry_request.job_id, retry_request.to_attempt_id],
        kwargs={'retry_request_id': retry_request.pk},
        task_id=retry_request.idempotency_key,
    )
    retry_request.refresh_from_db()
    assert retry_request.status == 'dispatched'
    assert retry_request.dispatched_at is not None
