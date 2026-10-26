from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.test import override_settings

from apps.executions.adapters import (
    AdapterConflict,
    ApiExecutorAdapter,
    AppExecutorAdapter,
    PerformanceExecutorAdapter,
    UiExecutorAdapter,
    UnsupportedExecutorError,
    get_executor_adapter,
)
from apps.executions.dispatch import CeleryExecutionDispatcher
from apps.executions.models import ExecutionArtifact, ExecutionJob, ExecutionStepResult
from apps.executions.source_contract import SourceBindingError, parse_source_id
from apps.executions.state_machine import AttemptStatus, ExecutionStatus
from apps.projects.models import Project
from apps.users.models import User


@pytest.fixture
def adapter_job(db):
    owner = User.objects.create_user(username='adapter-owner', password='pass')
    project = Project.objects.create(name='Adapter Project', owner=owner)
    return ExecutionJob.objects.create(
        project=project,
        source_type='api',
        source_id='suite-1',
        requested_by=owner,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    'adapter_class,source_type',
    [
        (ApiExecutorAdapter, 'api'),
        (UiExecutorAdapter, 'ui'),
        (AppExecutorAdapter, 'app'),
        (PerformanceExecutorAdapter, 'performance'),
    ],
)
def test_source_adapters_submit_and_collect(adapter_job, adapter_class, source_type):
    adapter_job.source_type = source_type
    adapter_job.save(update_fields=['source_type', 'updated_at'])
    adapter = adapter_class()

    receipt = adapter.submit(adapter_job, dispatcher=lambda job, attempt: f'legacy-{attempt.id}')
    assert receipt.supported is True
    assert receipt.status == ExecutionStatus.RUNNING
    assert receipt.dispatch_id.startswith('legacy-')

    attempt = adapter_job.attempts.get(pk=receipt.attempt_id)
    collected = adapter.collect_result(
        adapter_job,
        attempt,
        {
            'status': 'passed',
            'result': {'source': source_type},
            'steps': [
                {'case_key': 'case-1', 'step_key': 'step-1', 'status': 'passed', 'duration_ms': 12},
            ],
            'artifacts': [
                {'uri': f'artifact://{source_type}/1.json', 'media_type': 'application/json'},
            ],
        },
    )
    adapter_job.refresh_from_db()
    attempt.refresh_from_db()
    assert collected.status == ExecutionStatus.PASSED
    assert adapter_job.status == ExecutionStatus.PASSED
    assert attempt.status == AttemptStatus.PASSED
    assert ExecutionStepResult.objects.filter(attempt=attempt, case_key='case-1').count() == 1
    assert ExecutionArtifact.objects.filter(attempt=attempt).count() == 1


@pytest.mark.django_db
def test_adapter_without_dispatcher_fails_closed(adapter_job):
    receipt = ApiExecutorAdapter().submit(adapter_job)
    adapter_job.refresh_from_db()
    assert receipt.supported is False
    assert receipt.status == ExecutionStatus.UNSUPPORTED
    assert adapter_job.status == ExecutionStatus.UNSUPPORTED
    with pytest.raises(AdapterConflict, match='retry cannot be queued'):
        ApiExecutorAdapter().retry(adapter_job)


@pytest.mark.django_db
def test_adapter_retry_creates_new_attempt_after_failure(adapter_job):
    adapter = ApiExecutorAdapter(dispatcher=lambda _job, attempt: str(attempt.id))
    first = adapter.submit(adapter_job)
    attempt = adapter_job.attempts.get(pk=first.attempt_id)
    adapter.collect_result(adapter_job, attempt, {'status': 'failed', 'error_type': 'timeout'})
    adapter_job.refresh_from_db()

    retry = adapter.retry(adapter_job)
    adapter_job.refresh_from_db()
    assert retry.attempt_number == 2
    assert adapter_job.status == ExecutionStatus.QUEUED
    assert adapter_job.attempt == 2
    assert adapter_job.attempts.count() == 2


@pytest.mark.django_db
def test_adapter_cancel_is_idempotent(adapter_job):
    adapter = ApiExecutorAdapter()
    first = adapter.cancel(adapter_job, reason='operator requested')
    second = adapter.cancel(adapter_job, reason='operator requested')
    adapter_job.refresh_from_db()
    assert first.status == ExecutionStatus.CANCEL_REQUESTED
    assert second.status == ExecutionStatus.CANCEL_REQUESTED
    assert adapter_job.status == ExecutionStatus.CANCEL_REQUESTED


def test_default_registry_exposes_all_source_adapters():
    assert get_executor_adapter('api').source_type == 'api'
    assert get_executor_adapter('ui').source_type == 'ui'
    assert get_executor_adapter('app').source_type == 'app'
    assert get_executor_adapter('performance').source_type == 'performance'
    with pytest.raises(UnsupportedExecutorError):
        get_executor_adapter('missing')

    dispatcher = get_executor_adapter('api').dispatcher
    assert isinstance(dispatcher, CeleryExecutionDispatcher)
    with override_settings(EXECUTION_DISPATCH_ROUTES={'api': 'workers.api'}):
        assert dispatcher.supports('api') is True
        assert dispatcher.supports('ui') is False
        job = SimpleNamespace(pk=10, source_type='api', source_id='suite-1')
        attempt = SimpleNamespace(pk=20)
        broker_result = Mock(id='celery-10-20')
        with patch('apps.executions.dispatch.current_app.send_task', return_value=broker_result) as send_task:
            assert dispatcher(job, attempt) == 'celery-10-20'
        send_task.assert_called_once_with(
            'workers.api',
            args=[10, 20],
            kwargs={
                'execution_job_id': 10,
                'source_type': 'api',
                'source_id': 'suite-1',
                'variant_id': None,
            },
            task_id='execution-10-20',
        )

    parsed = parse_source_id('api', 'api:suite:42')
    assert parsed.kind == 'suite'
    assert parsed.object_id == 42
    with pytest.raises(SourceBindingError):
        parse_source_id('ui', 'api:suite:42')
