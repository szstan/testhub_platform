from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.executions.adapters import AdapterConflict, ApiExecutorAdapter, _variant_aggregate
from apps.executions.gate_policy import evaluate_metric_rules
from apps.executions.models import (
    ExecutionAttempt,
    ExecutionDataset,
    ExecutionJob,
    ExecutionProfile,
    ExecutionQualityGateEvaluation,
    ExecutionRetryRequest,
    ExecutionStepResult,
)
from apps.executions.retry_outbox import consume_retry_outbox
from apps.executions.run_comparison import _case_results, _final_attempt, compare_jobs, evaluate_release_gate
from apps.projects.models import Project
from apps.users.models import User


class _VariantRows(list):
    def order_by(self, *_fields):
        return self


def _aggregate(results, *, metric='failed', statuses=None):
    statuses = statuses or ['passed'] * len(results)
    variants = _VariantRows([
        SimpleNamespace(
            variant_key=f'variant-{index + 1}',
            ordinal=index,
            status=statuses[index],
            result=result,
            error_type='',
            error_message='',
        )
        for index, result in enumerate(results)
    ])
    job = SimpleNamespace(
        variants=variants,
        profile=SimpleNamespace(quality_gate={metric: {'max': 10}}),
    )
    return _variant_aggregate(job)


@pytest.mark.parametrize('metric,reported', [('failed', 3), ('unmatched', 2)])
def test_partial_count_metric_fails_closed_and_retains_reported_sum(metric, reported):
    _status, aggregate = _aggregate([{metric: reported}, {}], metric=metric)

    assert aggregate[metric] is None
    assert aggregate['metric_aggregation'][metric] == {
        'state': 'partial',
        'reported_variants': 1,
        'expected_variants': 2,
        'reported_total': reported,
        'invalid_values': 0,
    }
    assert aggregate['variants'][0]['result'][metric] == reported
    assert evaluate_metric_rules({metric: {'max': 10}}, aggregate) == [
        {'metric': metric, 'reason': 'missing_metric'}
    ]


def test_complete_reported_count_metric_is_summed():
    _status, aggregate = _aggregate([{'failed': 2}, {'failed': 3}])

    assert aggregate['failed'] == 5
    assert aggregate['metric_aggregation']['failed']['state'] == 'complete'
    assert aggregate['metric_aggregation']['failed']['reported_total'] == 5
    assert evaluate_metric_rules({'failed': {'max': 5}}, aggregate) == []


def test_all_missing_count_metrics_use_derived_counts():
    status, aggregate = _aggregate([{}, {}])

    assert status == 'passed'
    assert aggregate['failed'] == 0
    assert aggregate['metric_aggregation']['failed']['state'] == 'derived'
    assert evaluate_metric_rules({'failed': {'max': 0}}, aggregate) == []


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), -1, 1.5, True, '2'])
def test_invalid_reported_count_does_not_fall_back_to_derived_count(invalid):
    _status, aggregate = _aggregate([{'failed': invalid}, {'failed': invalid}])

    assert aggregate['failed'] is None
    assert aggregate['metric_aggregation']['failed']['state'] == 'invalid'
    assert aggregate['metric_aggregation']['failed']['invalid_values'] == 2
    assert evaluate_metric_rules({'failed': {'max': 10}}, aggregate) == [
        {'metric': 'failed', 'reason': 'missing_metric'}
    ]


@pytest.fixture
def matrix_context(db):
    user = User.objects.create_user(username='matrix-review-owner', password='pass')
    project = Project.objects.create(name='Matrix review integrity', owner=user)
    return project, user


def _matrix_job(project, user, *, source_id, data=None, profile=None):
    dataset = ExecutionDataset.objects.create(
        project=project,
        name=f'dataset-{source_id}',
        created_by=user,
        data=data if data is not None else [{'value': 1}, {'value': 2}],
    )
    job = ExecutionJob.objects.create(
        project=project,
        profile=profile,
        dataset=dataset,
        source_type='api',
        source_id=source_id,
        requested_by=user,
    )
    variants = job.initialize_dataset_variants()
    return job, variants


def _create_variant_attempt(job, variant, *, status, result=None, error_type='', error_message=''):
    attempt, _created = job.create_attempt(
        variant=variant,
        status=status,
        result=result or {},
        error_type=error_type,
        error_message=error_message,
    )
    return attempt


def _submit_matrix(job):
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'direct-dispatch')
    adapter.submit(job)
    attempts = {attempt.variant_id: attempt for attempt in job.attempts.filter(variant_id__isnull=False)}
    return adapter, attempts


def _finalize_matrix(job, variants, attempts, *, status, representative_index=-1):
    snapshot_variants = []
    for variant, attempt in zip(variants, attempts):
        variant.status = attempt.status
        variant.result = attempt.result
        variant.error_type = attempt.error_type
        variant.error_message = attempt.error_message
        variant.save(update_fields=['status', 'result', 'error_type', 'error_message', 'updated_at'])
        snapshot_variants.append({
            'variant_key': variant.variant_key,
            'ordinal': variant.ordinal,
            'status': attempt.status,
            'result': attempt.result,
            'error_type': attempt.error_type,
            'error_message': attempt.error_message,
        })

    representative = attempts[representative_index]
    representative.is_final = True
    representative.save(update_fields=['is_final', 'updated_at'])
    snapshot = {'status': status, 'count': len(variants), 'variants': snapshot_variants}
    job.status = status
    job.final_status = status
    job.final_attempt = representative
    job.result = snapshot
    job.final_result = snapshot
    job.finalized_at = timezone.now()
    job.save(update_fields=[
        'status', 'final_status', 'final_attempt', 'result', 'final_result', 'finalized_at', 'updated_at',
    ])
    return snapshot


def _final_single_job(project, user, *, source_id, status, result):
    job = ExecutionJob.objects.create(
        project=project,
        source_type='api',
        source_id=source_id,
        requested_by=user,
        status=status,
        final_status=status,
        result=result,
        final_result=result,
    )
    attempt, _created = job.create_attempt(status=status, result=result, is_final=True)
    job.final_attempt = attempt
    job.save(update_fields=['final_attempt', 'updated_at'])
    return job, attempt


def test_matrix_gate_fails_on_partial_failed_report(matrix_context):
    project, user = matrix_context
    profile = ExecutionProfile.objects.create(
        project=project,
        name='partial failed metric',
        quality_gate={'failed': {'max': 0}},
        created_by=user,
    )
    job, _variants = _matrix_job(project, user, source_id='partial-count', profile=profile)
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'delivery')
    adapter.submit(job)
    attempts = list(job.attempts.order_by('attempt_number'))

    adapter.collect_result(job, attempts[0], {'status': 'passed', 'result': {'failed': 3}})
    adapter.collect_result(job, attempts[1], {'status': 'passed', 'result': {}})
    job.refresh_from_db()

    assert job.final_status == 'blocked'
    assert job.final_result['failed'] is None
    assert job.final_result['metric_aggregation']['failed'] == {
        'state': 'partial',
        'reported_variants': 1,
        'expected_variants': 2,
        'reported_total': 3,
        'invalid_values': 0,
    }
    assert job.final_result['variants'][0]['result']['failed'] == 3
    assert any(
        failure['metric'] == 'failed' and failure['reason'] == 'missing_metric'
        for failure in job.quality_gate_result['failures']
    )


def test_matrix_comparison_reduces_latest_attempts_across_all_variants(matrix_context):
    project, user = matrix_context
    job, variants = _matrix_job(project, user, source_id='multi-variant-diff', data=[{}, {}])
    stale = _create_variant_attempt(job, variants[0], status='failed', result={'version': 'stale'})
    current = [
        _create_variant_attempt(job, variants[0], status='failed', result={'version': 'current-a'}),
        _create_variant_attempt(job, variants[1], status='failed', result={'version': 'current-b'}),
    ]
    _finalize_matrix(job, variants, current, status='failed')
    ExecutionStepResult.objects.create(
        attempt=stale, case_key='stale.case', step_key='old', status='failed',
    )
    ExecutionStepResult.objects.create(
        attempt=current[0], case_key='checkout.login', step_key='a', status='failed',
    )
    ExecutionStepResult.objects.create(
        attempt=current[1], case_key='checkout.login', step_key='b', status='passed',
    )
    ExecutionStepResult.objects.create(
        attempt=current[0], case_key='checkout.partial', step_key='a', status='passed',
    )
    ExecutionStepResult.objects.create(
        attempt=current[0], case_key='checkout.partial-failure', step_key='a', status='failed',
        error_message='failure outranks missing variant evidence',
    )

    baseline, baseline_attempt = _final_single_job(
        project, user, source_id='comparison-baseline', status='failed', result={'failed': 1},
    )
    for case_key in ('checkout.login', 'checkout.partial', 'stale.case'):
        ExecutionStepResult.objects.create(
            attempt=baseline_attempt, case_key=case_key, step_key='baseline', status='failed',
        )

    diff = compare_jobs(job, baseline)

    assert diff['unchanged_failures'] == ['checkout.login']
    assert diff['new_failures'] == ['checkout.partial-failure']
    assert diff['fixed_failures'] == []
    assert diff['unexecuted'] == ['checkout.partial']
    assert diff['removed_cases'] == ['stale.case']
    assert _case_results(job)['checkout.partial-failure']['status'] == 'failed'


def test_homogeneous_success_status_aliases_remain_valid_matrix_evidence(matrix_context):
    project, user = matrix_context
    job, variants = _matrix_job(project, user, source_id='success-aliases', data=[{}, {}])
    attempts = [
        _create_variant_attempt(job, variants[0], status='succeeded', result={'ok': True}),
        _create_variant_attempt(job, variants[1], status='completed', result={'ok': True}),
    ]
    _finalize_matrix(job, variants, attempts, status='passed')

    assert _final_attempt(job).pk == attempts[1].pk


def test_failed_matrix_aggregate_accepts_successful_representative_and_is_baseline(matrix_context):
    project, user = matrix_context
    job, variants = _matrix_job(project, user, source_id='mixed-outcomes', data=[{}, {}])
    adapter, attempts = _submit_matrix(job)
    adapter.collect_result(job, attempts[variants[1].pk], {
        'status': 'failed', 'result': {'ok': False}, 'error_message': 'variant failed',
    })
    adapter.collect_result(job, attempts[variants[0].pk], {'status': 'passed', 'result': {'ok': True}})
    job.refresh_from_db()

    assert job.final_status == 'failed'
    assert job.final_result['status'] == 'failed'
    assert job.final_attempt.status == 'passed'
    assert _final_attempt(job).pk == job.final_attempt_id

    for index, variant in enumerate(variants):
        attempt = attempts[variant.pk]
        ExecutionStepResult.objects.create(
            attempt=attempt,
            case_key='checkout.login',
            step_key=f'login-{index}',
            status='failed' if index == 1 else 'passed',
            error_message='variant failed' if index == 1 else '',
            duration_ms=42 if index == 1 else 5,
        )
        ExecutionStepResult.objects.create(
            attempt=attempt,
            case_key='checkout.logout',
            step_key=f'logout-{index}',
            status='passed',
            error_message=f'variant {index}',
            duration_ms=10 + index,
        )

    cases = _case_results(job)
    assert cases['checkout.login'] == {
        'case_key': 'checkout.login', 'status': 'failed',
        'error_message': 'variant failed', 'duration_ms': 42,
    }
    assert cases['checkout.logout'] == {
        'case_key': 'checkout.logout', 'status': 'passed',
        'error_message': 'variant 1', 'duration_ms': 11,
    }

    current, current_attempt = _final_single_job(
        project, user, source_id='passing-comparison-current', status='passed',
        result={'failed': 0, 'unmatched': 0},
    )
    for case_key in ('checkout.login', 'checkout.logout'):
        ExecutionStepResult.objects.create(
            attempt=current_attempt, case_key=case_key, step_key='current', status='passed',
        )

    diff = compare_jobs(current, job)
    assert diff['current_total'] == diff['baseline_total'] == 2
    assert diff['fixed_failures'] == ['checkout.login']
    assert diff['new_failures'] == []
    gate = evaluate_release_gate(current, baseline=job)
    assert gate['passed'] is True
    assert gate['diff']['fixed_failures'] == ['checkout.login']
    failed_job_gate = evaluate_release_gate(job)
    assert failed_job_gate['passed'] is False
    assert any(item['reason'] == 'final_status_not_successful' for item in failed_job_gate['failures'])


def test_gateblocked_matrix_requires_persisted_failed_gate_proof(matrix_context):
    project, user = matrix_context
    profile = ExecutionProfile.objects.create(
        project=project,
        name='blocking matrix gate',
        quality_gate={'p95': {'max': 100}},
        created_by=user,
    )
    job, variants = _matrix_job(
        project, user, source_id='gateblocked-matrix', profile=profile, data=[{}, {}],
    )
    adapter, attempts = _submit_matrix(job)
    for variant in variants:
        adapter.collect_result(job, attempts[variant.pk], {
            'status': 'passed', 'result': {'ok': True, 'p95': 125},
        })
    job.refresh_from_db()

    assert job.final_status == 'blocked'
    assert job.final_result['status'] == 'passed'
    assert job.quality_gate_result['configured'] is True
    assert job.quality_gate_result['passed'] is False
    evaluation = ExecutionQualityGateEvaluation.objects.get(job=job, stage='result')
    assert evaluation.passed is False
    assert evaluation.profile_id == profile.pk
    assert evaluation.attempt_id == job.final_attempt_id
    assert evaluation.summary == job.final_result
    assert _final_attempt(job).pk == job.final_attempt_id

    for index, variant in enumerate(variants):
        ExecutionStepResult.objects.create(
            attempt=attempts[variant.pk], case_key='checkout.health',
            step_key=f'health-{index}', status='passed', duration_ms=20 + index,
        )
    diff = compare_jobs(job, job)
    assert diff['current_total'] == diff['baseline_total'] == 1
    assert diff['unexecuted'] == []
    gate = evaluate_release_gate(job)
    assert gate['passed'] is False
    assert any(item['metric'] == 'final_evidence' for item in gate['failures'])

    evaluation.summary = {'status': 'forged'}
    evaluation.save(update_fields=['summary'])
    job.refresh_from_db()
    assert _final_attempt(job) is None
    assert compare_jobs(job, job)['current_total'] == 0


def test_gateblocked_matrix_without_persisted_gate_proof_is_not_final_evidence(matrix_context):
    project, user = matrix_context
    job, variants = _matrix_job(project, user, source_id='unproven-gateblocked-matrix', data=[{}, {}])
    attempts = [
        _create_variant_attempt(job, variant, status='passed', result={'ok': True})
        for variant in variants
    ]
    _finalize_matrix(job, variants, attempts, status='blocked')
    job.quality_gate_result = {
        'configured': True, 'passed': False, 'failures': [{'metric': 'p95'}],
    }
    job.save(update_fields=['quality_gate_result', 'updated_at'])

    assert _final_attempt(job) is None
    assert not ExecutionQualityGateEvaluation.objects.filter(
        job=job, stage='result', passed=False,
    ).exists()
    gate = evaluate_release_gate(job)
    assert gate['passed'] is False
    assert any(item['metric'] == 'final_evidence' for item in gate['failures'])


def test_matrix_final_snapshot_must_match_owned_current_attempts(matrix_context):
    project, user = matrix_context
    job, variants = _matrix_job(project, user, source_id='snapshot-owner', data=[{}, {}])
    attempts = [
        _create_variant_attempt(job, variant, status='passed', result={'id': index})
        for index, variant in enumerate(variants)
    ]
    _finalize_matrix(job, variants, attempts, status='passed')
    job.final_result['variants'][0]['result'] = {'id': 'foreign'}
    job.save(update_fields=['final_result', 'updated_at'])

    assert _final_attempt(job) is None


@pytest.mark.parametrize('snapshot_status', ['failed', 'success', 'running'])
def test_matrix_aggregate_status_snapshot_must_match_variant_precedence(matrix_context, snapshot_status):
    project, user = matrix_context
    job, variants = _matrix_job(project, user, source_id='aggregate-snapshot-status', data=[{}, {}])
    attempts = [
        _create_variant_attempt(job, variant, status='passed', result={'ok': True})
        for variant in variants
    ]
    _finalize_matrix(job, variants, attempts, status='passed')
    job.final_result['status'] = snapshot_status
    job.save(update_fields=['final_result', 'updated_at'])

    assert _final_attempt(job) is None


def test_matrix_final_pointer_must_own_a_current_variant_attempt(matrix_context):
    project, user = matrix_context
    job, variants = _matrix_job(project, user, source_id='foreign-final-pointer', data=[{}, {}])
    attempts = [
        _create_variant_attempt(job, variant, status='passed', result={'ok': True})
        for variant in variants
    ]
    _finalize_matrix(job, variants, attempts, status='passed')
    foreign_job, foreign_attempt = _final_single_job(
        project, user, source_id='foreign-pointer-target', status='passed', result={'ok': True},
    )
    job.final_attempt = foreign_attempt
    job.save(update_fields=['final_attempt', 'updated_at'])

    assert foreign_job.final_attempt_id == foreign_attempt.pk
    assert _final_attempt(job) is None


def test_matrix_final_evidence_requires_every_latest_variant_attempt_terminal(matrix_context):
    project, user = matrix_context
    job, variants = _matrix_job(project, user, source_id='nonterminal-current-variant', data=[{}, {}])
    attempts = [
        _create_variant_attempt(job, variant, status='passed', result={'ok': True})
        for variant in variants
    ]
    _finalize_matrix(job, variants, attempts, status='passed')
    job.create_attempt(variant=variants[1], status='running', result={'partial': True})

    assert _final_attempt(job) is None


def test_manual_matrix_retry_replays_one_outbox_row_per_eligible_variant(matrix_context):
    project, user = matrix_context
    profile = ExecutionProfile.objects.create(
        project=project,
        name='uneven variant retry budget',
        retry_policy={'max_attempts': 3},
        created_by=user,
    )
    job, variants = _matrix_job(
        project, user, source_id='uneven-retry', profile=profile, data=[{}, {}, {}, {}],
    )
    counts = [2, 3, 1, 1]
    statuses = ['failed', 'failed', 'failed', 'passed']
    current_attempts = []
    for variant, count, status in zip(variants, counts, statuses):
        current = None
        for index in range(count):
            current = _create_variant_attempt(
                job, variant, status=status, result={'variant': variant.ordinal, 'try': index + 1},
                error_type='infrastructure' if status == 'failed' else '',
            )
        current_attempts.append(current)
    final_snapshot = _finalize_matrix(job, variants, current_attempts, status='failed', representative_index=1)
    job.final_result = final_snapshot
    job.save(update_fields=['final_result', 'updated_at'])
    scheduled_callbacks = []
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'direct-dispatch')

    with patch('apps.executions.adapters.transaction.on_commit', side_effect=scheduled_callbacks.append):
        receipt = adapter.retry(job, reason='manual matrix retry', requested_by=user)

    requests = list(ExecutionRetryRequest.objects.filter(job=job).order_by('to_attempt__variant__ordinal'))
    assert len(requests) == 2
    assert [request.pk for request in requests] == receipt.details['retry_request_ids']
    assert receipt.details['retry_request_id'] == requests[0].pk
    assert receipt.attempt_id == requests[0].to_attempt_id
    assert len(scheduled_callbacks) == 2
    assert [(request.from_attempt_id, request.to_attempt_id) for request in requests] == [
        (current_attempts[0].pk, requests[0].to_attempt_id),
        (current_attempts[2].pk, requests[1].to_attempt_id),
    ]
    assert [request.to_attempt.variant_id for request in requests] == [variants[0].pk, variants[2].pk]
    assert requests[0].to_attempt.attempt_number != requests[1].to_attempt.attempt_number
    assert variants[1].latest_attempt.pk == current_attempts[1].pk
    assert variants[3].status == 'passed'
    assert variants[3].latest_attempt.pk == current_attempts[3].pk
    job.refresh_from_db()
    assert job.final_result == final_snapshot
    assert job.final_attempt_id is None and job.final_status == ''

    with override_settings(EXECUTION_RETRY_DISPATCH_ROUTES={'api': 'worker.retry'}):
        with patch('apps.executions.retry_outbox.current_app.send_task') as send_task:
            send_task.side_effect = [SimpleNamespace(id='replayed-a'), SimpleNamespace(id='replayed-c')]
            replayed = consume_retry_outbox()

    assert [receipt.status for receipt in replayed] == ['dispatched', 'dispatched']
    assert [call.kwargs['args'][1] for call in send_task.call_args_list] == [
        request.to_attempt_id for request in requests
    ]
    assert [call.kwargs['kwargs']['retry_request_id'] for call in send_task.call_args_list] == [
        request.pk for request in requests
    ]
    assert list(ExecutionRetryRequest.objects.filter(job=job, status='dispatched').order_by('pk').values_list('pk', flat=True)) == [
        request.pk for request in requests
    ]


def test_manual_matrix_retry_does_not_reset_successful_or_exhausted_variants(matrix_context):
    project, user = matrix_context
    profile = ExecutionProfile.objects.create(
        project=project,
        name='no eligible variant retry',
        retry_policy={'max_attempts': 2},
        created_by=user,
    )
    job, variants = _matrix_job(project, user, source_id='exhausted-retry', profile=profile, data=[{}, {}, {}])
    current_attempts = []
    for variant, count, status in zip(variants, [2, 2, 1], ['failed', 'failed', 'passed']):
        for _index in range(count):
            current = _create_variant_attempt(job, variant, status=status, result={'ordinal': variant.ordinal})
        current_attempts.append(current)
    _finalize_matrix(job, variants, current_attempts, status='failed')
    adapter = ApiExecutorAdapter(dispatcher=lambda *_: 'dispatch')

    with pytest.raises(AdapterConflict, match='MAX_ATTEMPTS'):
        adapter.retry(job)

    assert ExecutionRetryRequest.objects.filter(job=job).count() == 0
    assert variants[0].latest_attempt.pk == current_attempts[0].pk
    assert variants[1].latest_attempt.pk == current_attempts[1].pk
    assert variants[2].status == 'passed'
