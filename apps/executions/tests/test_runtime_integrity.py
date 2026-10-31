from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.executions.adapters import AdapterConflict, ApiExecutorAdapter
from apps.executions.models import ExecutionAttempt, ExecutionDataset, ExecutionJob, ExecutionProfile, ExecutionSourceBinding
from apps.executions.result_events import ResultEventIngestor
from apps.executions.source_workers import execute_api_source, normalize_ui_evidence, _variant_payload
from apps.executions.source_contract import SourceBindingError
from apps.executions.worker_leases import ExecutionClaimLost, claim_attempt, check_claim, reconcile_stale_execution_claims
from apps.projects.models import Project
from apps.users.models import User


@pytest.fixture
def runtime_job(db):
    user = User.objects.create_user(username='runtime-integrity-owner', password='pass')
    project = Project.objects.create(name='Runtime integrity', owner=user)
    return ExecutionJob.objects.create(project=project, source_type='api', source_id='api:request:1', requested_by=user)


def _matrix(job, rows):
    job.dataset = ExecutionDataset.objects.create(project=job.project, name='matrix', data=rows)
    job.save(update_fields=['dataset'])
    return job


def test_duplicate_dispatch_and_reentrant_delivery_reserve_once(runtime_job):
    adapter = ApiExecutorAdapter()
    calls = []
    def dispatch(job, attempt):
        calls.append(attempt.pk)
        replay = adapter.submit(job, dispatcher=dispatch)
        assert replay.attempt_id == attempt.pk
        assert replay.created_attempt is False
        return 'broker-id'
    first = adapter.submit(runtime_job, dispatcher=dispatch)
    second = adapter.submit(runtime_job, dispatcher=dispatch)
    assert calls == [first.attempt_id]
    assert second.attempt_id == first.attempt_id
    assert runtime_job.attempts.count() == 1


def test_eager_completion_is_not_overwritten_by_dispatch_return(runtime_job):
    adapter = ApiExecutorAdapter()
    def dispatch(job, attempt):
        adapter.collect_result(job, attempt, {'status': 'passed', 'result': {'answer': 42}})
        return 'eager-id'
    receipt = adapter.submit(runtime_job, dispatcher=dispatch)
    runtime_job.refresh_from_db()
    assert receipt.status == runtime_job.status == 'passed'
    assert runtime_job.final_result == {'answer': 42}
    with pytest.raises(AdapterConflict, match='explicit retry'):
        adapter.submit(runtime_job, dispatcher=dispatch)


def test_job_idempotency_constraint_race_rolls_back_only_insert(runtime_job):
    original = QuerySet.first
    missed = False
    def first(query):
        nonlocal missed
        if query.model is ExecutionJob and not missed:
            missed = True
            return None
        return original(query)
    with patch.object(QuerySet, 'first', first):
        replay, created = ExecutionJob.objects.create_idempotent(
            project=runtime_job.project, idempotency_key=runtime_job.idempotency_key,
            defaults={'source_type': runtime_job.source_type, 'source_id': runtime_job.source_id},
        )
    assert replay.pk == runtime_job.pk and created is False
    assert ExecutionJob.objects.filter(pk=runtime_job.pk).exists()


def test_dataset_events_wait_for_all_variants_and_gate_aggregate(runtime_job):
    _matrix(runtime_job, [{'value': 1}, {'value': 2}])
    profile = ExecutionProfile.objects.create(project=runtime_job.project, name='matrix gate', quality_gate={'p95': {'max': 100}})
    runtime_job.profile = profile
    runtime_job.save(update_fields=['profile'])
    adapter = ApiExecutorAdapter(dispatcher=lambda job, attempt, variant: f'delivery-{attempt.pk}')
    receipt = adapter.submit(runtime_job)
    replay = adapter.submit(runtime_job)
    assert replay.attempt_id == receipt.attempt_id
    assert runtime_job.attempts.count() == 2
    attempts = list(runtime_job.attempts.order_by('attempt_number'))
    for index, attempt in enumerate(attempts):
        claim = claim_attempt(runtime_job.pk, attempt.pk, attempt.dispatch_id)
        assert claim and claim != attempt.dispatch_id
        ResultEventIngestor.ingest(runtime_job, 'run_finished', {
            'attempt': attempt.attempt_number, 'status': 'passed', 'result': {'p95': 90 if index == 0 else 150},
        }, idempotency_key=f'variant-{index}', worker_claim_id=claim)
        runtime_job.refresh_from_db()
        if index == 0:
            assert runtime_job.status == 'running' and runtime_job.final_attempt_id is None
    assert runtime_job.status == runtime_job.final_status == 'blocked'
    assert runtime_job.final_result['count'] == 2
    assert runtime_job.quality_gate_result['passed'] is False
    assert runtime_job.quality_gate_evaluations.filter(stage='result').count() == 1


def test_dataset_aggregate_metric_gate_passes_complete_good_data(runtime_job):
    _matrix(runtime_job, [{'value': 1}, {'value': 2}])
    profile = ExecutionProfile.objects.create(project=runtime_job.project, name='good matrix', quality_gate={'p95': {'max': 100}})
    runtime_job.profile = profile
    runtime_job.save(update_fields=['profile'])
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'id')
    adapter.submit(runtime_job)
    for attempt in runtime_job.attempts.all():
        adapter.collect_result(runtime_job, attempt, {'status': 'passed', 'result': {'p95': 90}})
    runtime_job.refresh_from_db()
    assert runtime_job.final_status == 'passed'
    assert runtime_job.quality_gate_result['passed'] is True


def test_dataset_retry_is_scoped_to_failed_variant_and_ignores_late_attempt(runtime_job):
    _matrix(runtime_job, [{'value': 1}, {'value': 2}])
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'delivery')
    adapter.submit(runtime_job)
    first, second = list(runtime_job.attempts.order_by('attempt_number'))
    adapter.collect_result(runtime_job, first, {'status': 'passed'})
    adapter.collect_result(runtime_job, second, {'status': 'failed'})
    adapter.retry(runtime_job)
    adapter.submit(runtime_job)
    current = runtime_job.attempts.order_by('-attempt_number').first()
    assert current.variant_id == second.variant_id and current.variant_id != first.variant_id
    assert runtime_job.attempts.count() == 3
    adapter.collect_result(runtime_job, second, {'status': 'passed', 'result': {'late': True}})
    second.variant.refresh_from_db()
    assert second.variant.status == 'running'
    adapter.collect_result(runtime_job, current, {'status': 'passed'})
    runtime_job.refresh_from_db()
    assert runtime_job.final_status == 'passed'


def test_worker_uuid_expiry_recovers_and_old_worker_cannot_finalize(runtime_job):
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'same-celery-task')
    receipt = adapter.submit(runtime_job)
    claim = claim_attempt(runtime_job.pk, receipt.attempt_id, 'same-celery-task')
    assert claim and claim != 'same-celery-task'
    assert not claim_attempt(runtime_job.pk, receipt.attempt_id, 'same-celery-task')
    ExecutionAttempt.objects.filter(pk=receipt.attempt_id).update(worker_claim_expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(ExecutionClaimLost):
        check_claim(runtime_job.pk, receipt.attempt_id, claim, renew=True)
    assert reconcile_stale_execution_claims() == 1
    runtime_job.refresh_from_db()
    assert runtime_job.final_status == 'failed'
    ResultEventIngestor.ingest(runtime_job, 'run_finished', {'attempt': 1, 'status': 'passed'},
                               idempotency_key='late-worker', worker_claim_id=claim)
    runtime_job.refresh_from_db()
    assert runtime_job.final_status == 'failed'
    adapter.retry(runtime_job)
    adapter.submit(runtime_job)
    new = runtime_job.attempts.order_by('-attempt_number').first()
    assert new.pk != receipt.attempt_id
    assert claim_attempt(runtime_job.pk, new.pk, 'same-celery-task') != claim


def test_reserved_delivery_without_worker_is_bounded(runtime_job):
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'missing-worker')
    receipt = adapter.submit(runtime_job)
    ExecutionAttempt.objects.filter(pk=receipt.attempt_id).update(dispatch_reserved_at=timezone.now() - timedelta(hours=1))
    assert reconcile_stale_execution_claims() == 1
    runtime_job.refresh_from_db()
    assert runtime_job.final_status == 'failed'


def test_stale_result_events_do_not_overwrite_current_steps(runtime_job):
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'task')
    receipt = adapter.submit(runtime_job)
    old = runtime_job.attempts.get(pk=receipt.attempt_id)
    adapter.collect_result(runtime_job, old, {'status': 'failed'})
    adapter.retry(runtime_job)
    ResultEventIngestor.ingest(runtime_job, 'step_finished', {
        'attempt': old.attempt_number, 'status': 'passed', 'case_key': 'late', 'step_key': 'late',
    }, idempotency_key='late-step')
    assert old.step_results.count() == 0
    runtime_job.refresh_from_db()
    assert runtime_job.status == 'queued'


@pytest.mark.parametrize('source,payload', [('app', {'locale': 'en'}), ('performance', {'users': 100}), ('ui', {'locale': 'en'}), ('api', {'nested': {}})])
def test_unsupported_dataset_parameters_fail_explicitly(source, payload):
    attempt = SimpleNamespace(variant_id=1, variant=SimpleNamespace(payload=payload))
    with pytest.raises(SourceBindingError):
        _variant_payload(attempt, source)


def test_unified_ui_projection_preserves_captured_digest():
    reference = {'sha256': 'a' * 64, 'size_bytes': 27, 'mime_type': 'image/png', 'uri': 'urn:sha256:' + 'a' * 64,
                 'availability': 'AVAILABLE', 'metadata': {'producer': 'ui_test_executor', 'execution_id': '4'}}
    projected = normalize_ui_evidence({'screenshots': [{'url': 'data:image/png;base64,aaaa', 'evidence_reference': reference}]})
    evidence = projected['screenshots'][0]['evidence_reference']
    assert evidence['sha256'] == reference['sha256'] and evidence['size_bytes'] == 27
    assert evidence['source_uri'] == reference['uri']
    assert evidence['provenance'] == reference['metadata']
    assert 'url' not in projected['screenshots'][0]


def test_api_dataset_worker_applies_each_row_to_actual_request(runtime_job):
    from apps.api_testing.models import ApiCollection, ApiProject, ApiRequest
    project = ApiProject.objects.create(name='source project', owner=runtime_job.requested_by)
    collection = ApiCollection.objects.create(project=project, name='source collection')
    request = ApiRequest.objects.create(collection=collection, name='parameterized', url='https://example.test/{{item}}',
                                         params={'value': '{{item}}'}, created_by=runtime_job.requested_by)
    runtime_job.source_id = f'api:request:{request.pk}'
    runtime_job.save(update_fields=['source_id'])
    ExecutionSourceBinding.objects.create(project=runtime_job.project, source_type='api', source_id=runtime_job.source_id,
                                           source_project_id=str(project.pk), created_by=runtime_job.requested_by)
    _matrix(runtime_job, [{'item': 'one'}, {'item': 'two'}])
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: '')
    adapter.submit(runtime_job)
    response = Mock(status_code=200, headers={}, text='{}')
    response.json.return_value = {}
    with patch('requests.request', return_value=response) as http, patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda value, **_: value):
        for attempt in runtime_job.attempts.order_by('attempt_number'):
            result = execute_api_source.run(runtime_job.pk, attempt.pk, variant_id=attempt.variant_id)
            assert result['status'] == 'passed', result
    assert [call.kwargs['url'] for call in http.call_args_list] == ['https://example.test/one', 'https://example.test/two']
    assert [call.kwargs['params'] for call in http.call_args_list] == [{'value': 'one'}, {'value': 'two'}]
    request.refresh_from_db()
    assert request.url == 'https://example.test/{{item}}'
    runtime_job.refresh_from_db()
    assert runtime_job.final_status == 'passed'


@pytest.mark.parametrize('last_status,expected', [('passed', 'passed'), ('failed', 'failed')])
def test_dataset_automatic_retry_budget_counts_each_variant(runtime_job, last_status, expected):
    _matrix(runtime_job, [{'value': 1}, {'value': 2}])
    runtime_job.profile = ExecutionProfile.objects.create(
        project=runtime_job.project, name='scoped retry budget',
        retry_policy={'max_attempts': 2, 'retryable_statuses': ['failed'], 'retryable_error_types': ['infrastructure']},
    )
    runtime_job.save(update_fields=['profile'])
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'delivery')
    adapter.submit(runtime_job)
    first, second = list(runtime_job.attempts.order_by('attempt_number'))
    adapter.collect_result(runtime_job, first, {'status': 'passed'})
    adapter.collect_result(runtime_job, second, {'status': 'failed', 'error_type': 'infrastructure'})
    runtime_job.refresh_from_db()
    assert runtime_job.status == 'running' and runtime_job.final_attempt_id is None
    assert runtime_job.retry_requests.count() == 1
    current = runtime_job.attempts.order_by('-attempt_number').first()
    assert current.attempt_number == 3 and current.variant_id == second.variant_id
    adapter.submit(runtime_job)
    adapter.collect_result(runtime_job, current, {'status': last_status, 'error_type': 'infrastructure'})
    runtime_job.refresh_from_db()
    assert runtime_job.final_status == expected
    assert runtime_job.attempts.count() == 3
    if expected == 'failed':
        with pytest.raises(AdapterConflict, match='MAX_ATTEMPTS'):
            adapter.retry(runtime_job)


def test_matrix_gate_does_not_hide_missing_later_variant_metric(runtime_job):
    _matrix(runtime_job, [{'value': 1}, {'value': 2}])
    runtime_job.profile = ExecutionProfile.objects.create(project=runtime_job.project, name='missing row metric', quality_gate={'p95': {'max': 100}})
    runtime_job.save(update_fields=['profile'])
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'delivery')
    adapter.submit(runtime_job)
    first, second = list(runtime_job.attempts.order_by('attempt_number'))
    adapter.collect_result(runtime_job, first, {'status': 'passed', 'result': {'p95': 50}})
    adapter.collect_result(runtime_job, second, {'status': 'passed', 'result': {'p95': None}})
    runtime_job.refresh_from_db()
    assert runtime_job.final_status == 'blocked'
    assert runtime_job.quality_gate_result['passed'] is False


def test_unknown_active_attempt_is_controlled_error_without_partial_event(runtime_job):
    from apps.executions.result_events import ResultEventError
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'existing-delivery')
    adapter.submit(runtime_job)
    with pytest.raises(ResultEventError, match='attempt does not exist'):
        ResultEventIngestor.ingest(runtime_job, 'run_started', {'attempt': 2}, idempotency_key='unreserved-second')
    runtime_job.refresh_from_db()
    assert runtime_job.attempts.count() == 1
    assert runtime_job.events.count() == 0
    assert runtime_job.attempt == 1 and runtime_job.status == 'running'


def test_cancel_requested_finish_is_cancelled_and_retained_atomically(runtime_job):
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'cancelling-delivery')
    adapter.submit(runtime_job)
    adapter.cancel(runtime_job)
    ResultEventIngestor.ingest(runtime_job, 'run_finished', {'attempt': 1, 'status': 'failed'}, idempotency_key='cancelled-finish')
    runtime_job.refresh_from_db()
    assert runtime_job.final_status == runtime_job.status == 'cancelled'


def test_upgrade_legacy_unclaimed_delivery_is_reconciled(runtime_job):
    runtime_job.status = 'running'
    runtime_job.save(update_fields=['status'])
    attempt, _ = runtime_job.create_attempt(dispatch_id='legacy-broker', status='running')
    ExecutionAttempt.objects.filter(pk=attempt.pk).update(updated_at=timezone.now() - timedelta(hours=1))
    assert reconcile_stale_execution_claims() == 1
    runtime_job.refresh_from_db()
    assert runtime_job.final_status == 'failed'
