from unittest.mock import Mock, patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.executions.adapters import ApiExecutorAdapter
from apps.executions.models import (
    ExecutionAISuggestion,
    ExecutionEvent,
    ExecutionJob,
    ExecutionQualityGateEvaluation,
    ExecutionRetryPolicy,
    ExecutionRetryRequest,
    ExecutionProfile,
)
from apps.executions.policies import (
    AISuggestionDisabled,
    check_dispatch_governance,
    record_ai_suggestion,
    review_ai_suggestion,
)
from apps.executions.result_events import ResultEventIngestor
from apps.executions.retry_outbox import dispatch_retry_request
from apps.executions.state_machine import ExecutionStatus
from apps.projects.models import Project
from apps.quality_intelligence.models import (
    QualityQuarantine,
    QualityQuarantineApprovalStatus,
    QualityQuarantineAudit,
    QualityRun,
    QualityRunItem,
    QualityTestProfile,
)
from apps.quality_intelligence.services.governance import approve_quarantine
from apps.quality_intelligence.services.flakiness import validate_quarantine_state
from apps.users.models import User


@pytest.fixture
def governed_job(db):
    owner = User.objects.create_user(username='governance-owner', password='pass')
    project = Project.objects.create(name='Governance Project', owner=owner)
    profile = ExecutionProfile.objects.create(
        project=project,
        name='governed-profile',
        source_type='api',
        retry_limit=0,
        quality_gate={'pass_rate': {'min': 90}},
        created_by=owner,
    )
    job = ExecutionJob.objects.create(
        project=project,
        profile=profile,
        source_type='api',
        source_id='case-governed',
        requested_by=owner,
    )
    return owner, project, profile, job


@pytest.mark.django_db
def test_automatic_retry_preserves_final_result_and_reason(governed_job):
    owner, project, profile, job = governed_job
    profile.quality_gate = {}
    profile.save(update_fields=['quality_gate', 'updated_at'])
    ExecutionRetryPolicy.objects.create(
        project=project,
        profile=profile,
        case_key='case-governed',
        max_attempts=2,
        retryable_error_types=['timeout'],
        retryable_statuses=['failed'],
        created_by=owner,
    )
    adapter = ApiExecutorAdapter(dispatcher=lambda _job, attempt: f'dispatch-{attempt.attempt_number}')
    first = adapter.submit(job)
    attempt_one = job.attempts.get(pk=first.attempt_id)
    collected = adapter.collect_result(
        job,
        attempt_one,
        {'status': 'failed', 'error_type': 'timeout', 'result': {'attempt': 1}},
    )
    job.refresh_from_db()
    assert collected.status == ExecutionStatus.QUEUED
    assert job.attempts.count() == 2
    attempt_one.refresh_from_db()
    assert attempt_one.retry_reason.startswith('automatic retry:')
    assert attempt_one.is_final is False
    assert job.final_result == {}

    second = adapter.submit(job)
    assert second.attempt_id != attempt_one.pk
    attempt_two = job.attempts.get(pk=second.attempt_id)
    adapter.collect_result(job, attempt_two, {'status': 'passed', 'result': {'attempt': 2}})
    job.refresh_from_db()
    attempt_two.refresh_from_db()
    assert job.status == ExecutionStatus.PASSED
    assert job.final_result == {'attempt': 2}
    assert job.final_attempt_id == attempt_two.pk
    assert attempt_two.is_final is True


@pytest.mark.django_db
def test_automatic_retry_is_dispatched_and_outboxed(governed_job):
    owner, project, profile, job = governed_job
    profile.quality_gate = {}
    profile.save(update_fields=['quality_gate', 'updated_at'])
    ExecutionRetryPolicy.objects.create(
        project=project,
        profile=profile,
        case_key='case-governed',
        max_attempts=2,
        retryable_error_types=['timeout'],
        retryable_statuses=['failed'],
        created_by=owner,
    )
    calls = []
    adapter = ApiExecutorAdapter(dispatcher=lambda _job, attempt: calls.append(attempt.attempt_number) or f'dispatch-{attempt.attempt_number}')
    first = adapter.submit(job)
    attempt = job.attempts.get(pk=first.attempt_id)
    adapter.collect_result(job, attempt, {'status': 'failed', 'error_type': 'timeout'})
    job.refresh_from_db()
    assert calls == [1]
    assert job.attempts.count() == 2
    retry_request = ExecutionRetryRequest.objects.get(job=job)
    assert retry_request.status in {'queued', 'dispatched'}
    assert retry_request.to_attempt.attempt_number == 2
    assert job.retry_policy_snapshot['policy_id'] == retry_request.policy_id

    retry_request.status = 'queued'
    retry_request.save(update_fields=['status', 'updated_at'])
    failed = dispatch_retry_request(retry_request.pk)
    assert failed.status == 'failed'

    retry_request.status = 'queued'
    retry_request.save(update_fields=['status', 'updated_at'])
    broker_result = Mock(id='governance-retry-task')
    with override_settings(EXECUTION_RETRY_DISPATCH_ROUTES={'api': 'worker.retry'}):
        with patch('apps.executions.retry_outbox.current_app.send_task', return_value=broker_result) as send_task:
            dispatched = dispatch_retry_request(retry_request.pk)
    assert dispatched.status == 'dispatched'
    send_task.assert_called_once_with(
        'worker.retry',
        args=[job.pk, retry_request.to_attempt_id],
        kwargs={'retry_request_id': retry_request.pk},
        task_id=retry_request.idempotency_key,
    )


@pytest.mark.django_db
def test_late_events_do_not_change_final_snapshot_or_cross_job_idempotency(governed_job):
    _owner, project, profile, job = governed_job
    profile.quality_gate = {}
    profile.save(update_fields=['quality_gate', 'updated_at'])
    first = ResultEventIngestor.ingest(job, 'run_finished', {'attempt': 1, 'status': 'passed', 'result': {'value': 'final'}}, idempotency_key='provider-key')
    job.refresh_from_db()
    assert job.final_result == {'value': 'final'}
    late = ResultEventIngestor.ingest(job, 'run_finished', {'attempt': 2, 'status': 'failed', 'result': {'value': 'late'}}, idempotency_key='provider-key-late')
    job.refresh_from_db()
    assert late.created is True
    assert job.final_result == {'value': 'final'}
    assert job.final_status == 'passed'

    other = ExecutionJob.objects.create(project=project, source_type='api', requested_by=job.requested_by)
    second = ResultEventIngestor.ingest(other, 'run_started', {'attempt': 1}, idempotency_key='provider-key')
    assert second.created is True
    assert ExecutionEvent.objects.filter(idempotency_key__endswith=':provider-key').count() == 2


@pytest.mark.django_db
def test_quality_gate_blocks_and_is_audited(governed_job):
    _owner, _project, _profile, job = governed_job
    adapter = ApiExecutorAdapter(dispatcher=lambda _job, attempt: str(attempt.pk))
    receipt = adapter.submit(job)
    attempt = job.attempts.get(pk=receipt.attempt_id)
    adapter.collect_result(job, attempt, {'status': 'passed', 'result': {'pass_rate': 80}})
    job.refresh_from_db()
    assert job.status == ExecutionStatus.BLOCKED
    assert job.final_status == ExecutionStatus.BLOCKED
    evaluation = ExecutionQualityGateEvaluation.objects.get(job=job, stage='result')
    assert evaluation.passed is False
    assert evaluation.failures[0]['metric'] == 'pass_rate'


@pytest.mark.django_db
def test_ai_suggestion_is_redacted_deterministic_and_human_reviewed(governed_job):
    owner, project, _profile, job = governed_job
    with override_settings(EXECUTION_AI_SUGGESTIONS_ENABLED=False):
        with pytest.raises(AISuggestionDisabled):
            record_ai_suggestion(
                project=project,
                job=job,
                kind='failure_classification',
                input_payload={'password': 'secret-value'},
                output_payload={'category': 'TEST'},
            )
    first = record_ai_suggestion(
        project=project,
        job=job,
        kind='failure_classification',
        input_payload={'password': 'secret-value', 'message': 'timeout'},
        output_payload={'category': 'TEST'},
        requested_by=owner,
        force=True,
    )
    replay = record_ai_suggestion(
        project=project,
        job=job,
        kind='failure_classification',
        input_payload={'password': 'secret-value', 'message': 'timeout'},
        output_payload={'category': 'TEST'},
        requested_by=owner,
        force=True,
    )
    assert first.pk == replay.pk
    assert first.input_redacted['password'] == '[REDACTED]'
    assert first.status == 'proposed'
    review_ai_suggestion(first, owner, 'approved', 'human checked')
    first.refresh_from_db()
    assert first.status == 'approved'
    assert first.reviewed_by_id == owner.pk
    assert ExecutionAISuggestion.objects.count() == 1


@pytest.mark.django_db
def test_quarantine_approval_and_override_are_audited(governed_job):
    owner, project, _profile, job = governed_job
    owner.is_staff = True
    owner.save(update_fields=['is_staff'])
    reviewer = User.objects.create_user(username='governance-reviewer', password='pass', is_staff=True)
    quality_profile = QualityTestProfile.objects.create(
        source_type='API',
        project_type='API',
        project_id=str(project.pk),
        test_key='case-governed',
        environment_fingerprint='unknown',
    )
    quarantine = QualityQuarantine.objects.create(
        profile=quality_profile,
        reason='flaky test',
        owner=owner,
        created_by=owner,
        approval_status=QualityQuarantineApprovalStatus.PENDING,
        starts_at=timezone.now(),
    )
    assert validate_quarantine_state(quarantine)['is_active'] is False
    approve_quarantine(quarantine, reviewer, 'reviewed')
    quarantine.refresh_from_db()
    assert validate_quarantine_state(quarantine)['is_active'] is True

    denied = check_dispatch_governance(job)
    assert denied['allowed'] is False
    allowed = check_dispatch_governance(job, override=True, override_reason='release for diagnosis', actor=reviewer)
    assert allowed['allowed'] is True


@pytest.mark.django_db
def test_unified_result_projects_to_quality_intelligence_when_enabled(governed_job):
    _owner, _project, _profile, job = governed_job
    with override_settings(QUALITY_INTELLIGENCE_ENABLED=True):
        adapter = ApiExecutorAdapter(dispatcher=lambda _job, attempt: str(attempt.pk))
        receipt = adapter.submit(job)
        attempt = job.attempts.get(pk=receipt.attempt_id)
        adapter.collect_result(
            job,
            attempt,
            {'status': 'passed', 'result': {'pass_rate': 100}},
        )
    run = QualityRun.objects.get(source_execution_id=f'unified:{job.pk}')
    assert run.source_type == 'API'
    assert QualityRunItem.objects.filter(run=run, outcome='PASS').exists()
    assert QualityTestProfile.objects.filter(project_id=str(job.project_id), test_key=job.source_id).exists()
