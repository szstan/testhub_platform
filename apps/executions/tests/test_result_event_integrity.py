import pytest
from datetime import timedelta
from django.utils import timezone
from rest_framework.test import APIClient

from apps.executions.models import (
    ExecutionArtifact,
    ExecutionAttempt,
    ExecutionDataset,
    ExecutionEvent,
    ExecutionJob,
    ExecutionStepResult,
)
from apps.executions.result_events import (
    ResultEventError,
    ResultEventIngestor,
    redact_result_payload,
    redact_result_text,
)
from apps.executions.state_machine import AttemptStatus, ExecutionStatus
from apps.executions.worker_leases import claim_attempt
from apps.projects.models import Project
from apps.users.models import User


@pytest.fixture
def integrity_job(db):
    owner = User.objects.create_user(username='event-integrity-owner', password='pass')
    project = Project.objects.create(name='Event Integrity Project', owner=owner)
    job = ExecutionJob.objects.create(project=project, source_type='api', requested_by=owner)
    return owner, job


@pytest.mark.django_db
@pytest.mark.parametrize('payload', [None, [], 'not-an-object', 7])
def test_non_mapping_payload_is_rejected_without_writes(integrity_job, payload):
    _owner, job = integrity_job

    with pytest.raises(ResultEventError, match='payload must be an object'):
        ResultEventIngestor.ingest(job, 'run_started', payload)

    job.refresh_from_db()
    assert job.attempt == 0
    assert ExecutionAttempt.objects.filter(job=job).count() == 0
    assert ExecutionEvent.objects.filter(job=job).count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize('attempt', [True, False, 0, -1, 1.0, '1', None, 2_147_483_648])
def test_invalid_attempt_number_is_rejected_without_writes(integrity_job, attempt):
    _owner, job = integrity_job

    with pytest.raises(ResultEventError, match='attempt'):
        ResultEventIngestor.ingest(job, 'run_started', {'attempt': attempt})

    job.refresh_from_db()
    assert job.attempt == 0
    assert not ExecutionAttempt.objects.filter(job=job).exists()
    assert not ExecutionEvent.objects.filter(job=job).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('event_type', 'payload'),
    [
        ('run_finished', {'attempt': 1}),
        ('run_finished', {'attempt': 1, 'status': 'running'}),
        ('run_finished', {'attempt': 1, 'status': 'unknown'}),
        ('run_started', {}),
        ('step_finished', {'attempt': 1, 'status': 'passed', 'result': []}),
        ('run_finished', {'attempt': 1, 'status': 'passed', 'summary': 'not-an-object'}),
        ('artifact_uploaded', {'attempt': 1, 'uri': 'artifact://bad-size', 'size_bytes': True}),
        ('artifact_uploaded', {'attempt': 1, 'uri': 'artifact://bad-metadata', 'metadata': []}),
    ],
)
def test_invalid_result_shapes_and_nonterminal_finish_are_atomic(integrity_job, event_type, payload):
    _owner, job = integrity_job

    with pytest.raises(ResultEventError):
        ResultEventIngestor.ingest(job, event_type, payload)

    job.refresh_from_db()
    assert job.attempt == 0
    assert not ExecutionAttempt.objects.filter(job=job).exists()
    assert not ExecutionEvent.objects.filter(job=job).exists()


@pytest.mark.django_db
def test_event_api_returns_controlled_400_for_invalid_payload(integrity_job):
    owner, job = integrity_job
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(
        f'/api/executions/jobs/{job.pk}/events/',
        {'event_type': 'run_started', 'payload': {'attempt': True}},
        format='json',
    )

    assert response.status_code == 400
    assert not ExecutionEvent.objects.filter(job=job).exists()
    assert not ExecutionAttempt.objects.filter(job=job).exists()


def test_text_redaction_covers_auth_key_values_query_and_url_userinfo():
    text = (
        'Bearer bearer-secret Basic basic-secret password=plain-secret '
        'token: "quoted secret" api_key=api-secret '
        'https://url-user:url-password@example.test/path?token=query-secret'
    )
    safe = redact_result_text(text)

    for secret in (
        'bearer-secret',
        'basic-secret',
        'plain-secret',
        'quoted secret',
        'api-secret',
        'url-user',
        'url-password',
        'query-secret',
    ):
        assert secret not in safe
    assert '[REDACTED]' in safe


def test_payload_redaction_sanitizes_non_finite_numbers():
    assert redact_result_payload({'nan': float('nan'), 'positive': float('inf'), 'negative': float('-inf')}) == {
        'nan': None,
        'positive': None,
        'negative': None,
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    'payload',
    [
        [],
        {'result': []},
        {'case_key': 'c' * 256},
        {'step_key': 's' * 256},
        {'duration_ms': True},
        {'duration_ms': -1},
        {'duration_ms': 2_147_483_648},
    ],
)
def test_direct_step_recording_rejects_invalid_shape_before_writes(integrity_job, payload):
    _owner, job = integrity_job
    attempt, _ = job.create_attempt()

    with pytest.raises(ResultEventError):
        ResultEventIngestor._record_step(attempt, payload)

    assert not ExecutionStepResult.objects.filter(attempt=attempt).exists()


@pytest.mark.django_db
def test_step_default_idempotency_hashes_full_identity_and_redacts_direct_payload(integrity_job):
    _owner, job = integrity_job
    attempt, _ = job.create_attempt()
    prefix = 's' * 128

    first = ResultEventIngestor._record_step(
        attempt,
        {
            'case_key': 'case',
            'step_key': prefix + 'a',
            'status': 'passed',
            'result': {'token': 'hidden', 'message': 'api_key=embedded-secret'},
        },
    )
    second = ResultEventIngestor._record_step(
        attempt,
        {'case_key': 'case', 'step_key': prefix + 'b', 'status': 'passed', 'result': {}},
    )

    assert first.idempotency_key != second.idempotency_key
    assert len(first.idempotency_key) == len(second.idempotency_key) == 64
    assert first.result == {'token': '[REDACTED]', 'message': 'api_key=[REDACTED]'}
    assert ExecutionStepResult.objects.filter(attempt=attempt).count() == 2


@pytest.mark.django_db
def test_event_identity_uses_job_attempt_event_type_and_full_long_key(integrity_job):
    _owner, job = integrity_job
    first_key = 'same-prefix-' + ('x' * 140) + '-first'
    second_key = 'same-prefix-' + ('x' * 140) + '-second'

    first = ResultEventIngestor.ingest(job, 'run_started', {'attempt': 1}, idempotency_key=first_key)
    reserved_second, _ = job.create_attempt(attempt_number=2)
    second = ResultEventIngestor.ingest(job, 'run_started', {'attempt': 2}, idempotency_key=second_key)
    third = ResultEventIngestor.ingest(job, 'step_finished', {'attempt': 2, 'status': 'passed'}, idempotency_key=second_key)

    assert len({first.event_id, second.event_id, third.event_id}) == 3
    assert first.attempt_id is not None
    assert second.attempt_id == reserved_second.pk
    assert third.attempt_id == reserved_second.pk
    assert ExecutionEvent.objects.filter(job=job).count() == 3
    job.refresh_from_db()
    assert job.attempt == 2
    assert job.attempts.count() == 2
    stored_keys = list(ExecutionEvent.objects.filter(job=job).values_list('idempotency_key', flat=True))
    assert len(stored_keys) == len(set(stored_keys))
    assert all(len(key) <= 128 for key in stored_keys)
    with pytest.raises(ResultEventError, match='different content'):
        ResultEventIngestor.ingest(
            job,
            'run_started',
            {'attempt': 1, 'status': 'changed'},
            idempotency_key=first_key,
        )
    assert ExecutionEvent.objects.filter(job=job).count() == 3

    secret_key_receipt = ResultEventIngestor.ingest(
        job,
        'run_started',
        {'attempt': 2},
        idempotency_key='token=event-key-secret',
    )
    secret_key = ExecutionEvent.objects.get(pk=secret_key_receipt.event_id).idempotency_key
    assert 'event-key-secret' not in secret_key
    job.refresh_from_db()
    assert job.attempt == 2
    assert job.attempts.count() == 2


@pytest.mark.django_db
def test_worker_claim_must_match_before_state_changes(integrity_job):
    _owner, job = integrity_job
    attempt, _ = job.create_attempt()
    claim_id = claim_attempt(job.pk, attempt.pk)
    assert claim_id

    stale = ResultEventIngestor.ingest(
        job,
        'run_started',
        {'attempt': attempt.attempt_number},
        idempotency_key='wrong-worker-claim',
        worker_claim_id='stale-claim',
    )
    job.refresh_from_db()
    attempt.refresh_from_db()
    assert stale.created is True
    assert job.status == ExecutionStatus.PENDING
    assert attempt.status == AttemptStatus.PENDING

    current = ResultEventIngestor.ingest(
        job,
        'run_started',
        {'attempt': attempt.attempt_number},
        idempotency_key='current-worker-claim',
        worker_claim_id=claim_id,
    )
    job.refresh_from_db()
    attempt.refresh_from_db()
    assert current.created is True
    assert job.status == ExecutionStatus.RUNNING
    assert attempt.status == AttemptStatus.RUNNING


@pytest.mark.django_db
def test_expired_worker_claim_cannot_change_state(integrity_job):
    _owner, job = integrity_job
    attempt, _ = job.create_attempt()
    claim_id = claim_attempt(job.pk, attempt.pk)
    attempt.worker_claim_expires_at = timezone.now() - timedelta(seconds=1)
    attempt.save(update_fields=['worker_claim_expires_at', 'updated_at'])

    receipt = ResultEventIngestor.ingest(
        job,
        'run_started',
        {'attempt': attempt.attempt_number},
        idempotency_key='expired-worker-claim',
        worker_claim_id=claim_id,
    )

    job.refresh_from_db()
    attempt.refresh_from_db()
    assert receipt.created is True
    assert job.status == ExecutionStatus.PENDING
    assert attempt.status == AttemptStatus.PENDING


@pytest.mark.django_db
def test_artifact_idempotency_is_namespaced_and_replay_checks_content(integrity_job):
    _owner, job = integrity_job
    first_attempt, _ = job.create_attempt()
    payload = {
        'uri': 'https://artifact.example/run?token=artifact-secret',
        'idempotency_key': 'source-key-' + ('k' * 200),
        'content_hash': 'sha256:abc',
        'media_type': 'text/plain',
        'size_bytes': 12,
        'metadata': {'password': 'hidden', 'note': 'api_key=embedded-secret'},
    }

    first = ResultEventIngestor._record_artifact(job, first_attempt, payload)
    replay = ResultEventIngestor._record_artifact(job, first_attempt, payload)

    assert first.pk == replay.pk
    assert 'artifact-secret' not in first.uri
    assert first.metadata['password'] == '[REDACTED]'
    assert 'embedded-secret' not in first.metadata['note']
    with pytest.raises(ResultEventError, match='different content'):
        ResultEventIngestor._record_artifact(job, first_attempt, {**payload, 'content_hash': 'sha256:different'})

    second_attempt, _ = job.create_attempt()
    second = ResultEventIngestor._record_artifact(job, second_attempt, payload)
    assert second.pk != first.pk
    assert second.attempt_id == second_attempt.pk
    assert ExecutionArtifact.objects.filter(job=job).count() == 2


@pytest.mark.django_db
def test_step_error_and_artifact_event_payloads_are_redacted(integrity_job):
    _owner, job = integrity_job
    ResultEventIngestor.ingest(
        job,
        'step_finished',
        {
            'attempt': 1,
            'case_key': 'case',
            'step_key': 'step',
            'status': 'failed',
            'error_message': 'password=step-secret https://user:pass@example.test/path?token=url-secret',
            'result': {'message': 'Bearer result-secret', 'value': float('inf')},
        },
        idempotency_key='step-redaction',
    )
    step = ExecutionStepResult.objects.get(attempt__job=job)
    event = ExecutionEvent.objects.get(event_type='step_finished')

    assert 'step-secret' not in step.error_message
    assert 'url-secret' not in step.error_message
    assert 'result-secret' not in step.result['message']
    assert step.result['value'] is None
    assert 'step-secret' not in event.payload['error_message']

    ResultEventIngestor.ingest(
        job,
        'artifact_uploaded',
        {
            'attempt': 1,
            'uri': 'https://user:password@example.test/file?api_key=query-secret',
            'artifact_idempotency_key': 'redacted-artifact',
            'metadata': {'detail': 'token=metadata-secret'},
        },
        idempotency_key='artifact-redaction',
    )
    artifact = ExecutionArtifact.objects.get(job=job)
    artifact_event = ExecutionEvent.objects.get(event_type='artifact_uploaded')
    assert 'password' not in artifact.uri
    assert 'query-secret' not in artifact.uri
    assert 'metadata-secret' not in artifact.metadata['detail']
    assert 'metadata-secret' not in artifact_event.payload['metadata']['detail']


@pytest.mark.django_db
def test_late_events_are_retained_without_mutating_final_job_or_steps(integrity_job):
    _owner, job = integrity_job
    ResultEventIngestor.ingest(
        job,
        'run_finished',
        {'attempt': 1, 'status': 'passed', 'result': {'final': 'kept'}},
        idempotency_key='finish-current',
    )
    job.refresh_from_db()
    final_result = job.final_result
    final_attempt_id = job.final_attempt_id
    final_status = job.status

    late = ResultEventIngestor.ingest(
        job,
        'step_finished',
        {'attempt': 2, 'case_key': 'late', 'step_key': 'late', 'status': 'failed'},
        idempotency_key='late-step',
    )
    later = ResultEventIngestor.ingest(
        job,
        'run_finished',
        {'attempt': 2, 'status': 'failed', 'result': {'final': 'overwritten'}},
        idempotency_key='late-finish',
    )

    job.refresh_from_db()
    assert late.created is True
    assert later.created is True
    assert ExecutionEvent.objects.filter(job=job).count() == 3
    assert not ExecutionStepResult.objects.filter(attempt__job=job).exists()
    assert job.final_attempt_id == final_attempt_id
    assert job.final_result == final_result
    assert job.status == final_status == ExecutionStatus.PASSED


@pytest.mark.django_db
def test_dataset_events_update_variant_and_finish_through_adapter(integrity_job):
    owner, _base_job = integrity_job
    dataset = ExecutionDataset.objects.create(
        project=_base_job.project,
        name='event-integrity-data',
        data=[],
        matrix={'region': ['north', 'south']},
        created_by=owner,
    )
    job = ExecutionJob.objects.create(
        project=_base_job.project,
        source_type='api',
        requested_by=owner,
        dataset=dataset,
    )
    variants = job.initialize_dataset_variants()
    assert len(variants) == 2
    attempts = [job.create_attempt(variant=variant)[0] for variant in variants]

    for attempt in attempts:
        ResultEventIngestor.ingest(
            job,
            'run_started',
            {'attempt': attempt.attempt_number},
            idempotency_key=f'dataset-start-{attempt.attempt_number}',
        )
    variants[0].refresh_from_db()
    assert variants[0].status == ExecutionStatus.RUNNING

    ResultEventIngestor.ingest(
        job,
        'run_finished',
        {'attempt': attempts[0].attempt_number, 'status': 'passed', 'summary': {'passed': 1}},
        idempotency_key='dataset-finish-1',
    )
    variants[0].refresh_from_db()
    job.refresh_from_db()
    assert variants[0].status == ExecutionStatus.PASSED
    assert job.final_attempt_id is None
    assert job.status == ExecutionStatus.RUNNING

    second_finish = ResultEventIngestor.ingest(
        job,
        'run_finished',
        {'attempt': attempts[1].attempt_number, 'status': 'passed', 'summary': {'passed': 1}},
        idempotency_key='dataset-finish-2',
    )
    for variant in variants:
        variant.refresh_from_db()
        assert variant.status == ExecutionStatus.PASSED
    job.refresh_from_db()
    assert second_finish.status == ExecutionStatus.PASSED
    assert job.final_status == ExecutionStatus.PASSED
