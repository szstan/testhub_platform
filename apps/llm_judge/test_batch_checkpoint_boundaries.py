"""Exercise batch configuration and the pause-before-checkpoint boundary directly."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import close_old_connections
from django.utils import timezone
from rest_framework.test import APIClient

from apps.llm_judge.models import JudgeBatch, JudgeBatchCaseResult, JudgeRecord, Rubric
from apps.llm_judge.tasks import (
    _WorkerLeaseHeartbeat,
    PUBLISHER_LEASE_SECONDS,
    _renew_worker_lease,
    reconcile_stale_batches,
    score_batch_task,
    worker_lease_deadline,
)
from apps.llm_judge.test_batch_regressions import (
    _ReachableConnection, _record_fields, _run_batch, _valid_batch_score,
)
from apps.users.models import User


def _run_batch_in_thread(batch_id, task_id, claim_token=None):
    close_old_connections()
    try:
        return _run_batch(batch_id, task_id, claim_token)
    finally:
        close_old_connections()


def _queued_task(*_args, **kwargs):
    return SimpleNamespace(id=kwargs['task_id'])


def _summary():
    return {
        'mean_score': 90.0, 'std_dev': 0.0, 'safety_pass_rate': 1.0,
        'critical_success_rate': 1.0, 'gate_zone': 'green', 'blocked': False,
    }


@pytest.mark.django_db
def test_non_default_top_level_batch_rubric_is_used_without_case_override():
    staff = User.objects.create_user(username='top-level-batch-rubric-staff', is_staff=True)
    default = Rubric.objects.create(name='Default rubric', is_default=True)
    selected = Rubric.objects.create(name='Explicit batch rubric', is_default=False)
    client = APIClient()
    client.force_authenticate(staff)
    with (
        patch('kombu.Connection', return_value=_ReachableConnection()),
        patch.object(score_batch_task, 'apply_async', side_effect=_queued_task),
    ):
        response = client.post('/api/llm-judge/batches/', {
            'rubric': selected.pk,
            'cases': [{'question': 'Question without override', 'answer': 'Answer'}],
        }, format='json')
    assert response.status_code == 201, response.data
    batch = JudgeBatch.objects.get(pk=response.data['id'])
    assert batch.rubric_id == selected.pk
    assert 'rubric' not in batch.cases_data[0]
    used = []

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            used.append(case['rubric'])
            fields = _record_fields(case, batch, request_id)
            result = _valid_batch_score(fields, {})
            checkpoint_callback(fields, result)
            return result

        def summarize_batch(self, _results):
            assert self.rubric_id == selected.pk
            return _summary()

    with patch('apps.llm_judge.service.JudgeService', JudgeService):
        result = _run_batch(batch.pk, batch.celery_task_id)
    batch.refresh_from_db()
    assert result['status'] == 'completed'
    assert used == [selected.pk]
    assert used != [default.pk]
    assert JudgeRecord.objects.get(batch=batch).rubric_id == selected.pk
    assert JudgeBatchCaseResult.objects.get(batch=batch).record.rubric_id == selected.pk
    assert batch.scored == 1


@pytest.mark.django_db
def test_pause_during_inflight_score_cannot_leave_record_ahead_of_checkpoint():
    user = User.objects.create_user(username='inflight-pause-owner')
    rubric = Rubric.objects.create(name='In-flight pause rubric', is_default=True)
    batch = JudgeBatch.objects.create(
        rubric=rubric, cases_data=[{'question': 'In-flight question', 'answer': 'Answer'}],
        total=1, status='running', celery_task_id='worker-before-pause', created_by=user,
    )
    client = APIClient()
    client.force_authenticate(user)
    request_ids = []
    receipts = []

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            request_ids.append(request_id)
            if len(request_ids) == 1:
                # The user pauses while score_single is still in flight, before
                # the score can commit either its audit record or case result.
                with patch.object(score_batch_task, 'AsyncResult'):
                    paused = client.post(f'/api/llm-judge/batches/{batch.pk}/pause/', format='json')
                assert paused.status_code == 200
                assert paused.data['status'] == 'paused'
                assert not JudgeRecord.objects.filter(batch=batch).exists()
                assert not JudgeBatchCaseResult.objects.filter(batch=batch).exists()
            fields = _record_fields(case, batch, request_id)
            result = _valid_batch_score(fields, {})
            record = checkpoint_callback(fields, result)
            receipts.append(record.pk if record else None)
            return result

        def summarize_batch(self, _results):
            return _summary()

    with patch('apps.llm_judge.service.JudgeService', JudgeService):
        paused_result = _run_batch(batch.pk, 'worker-before-pause')
        batch.refresh_from_db()
        assert paused_result['status'] == 'paused'
        assert batch.scored == 0
        assert batch.progress == 0
        assert receipts == [None]
        assert not JudgeRecord.objects.filter(batch=batch).exists()
        assert not JudgeBatchCaseResult.objects.filter(batch=batch).exists()

        with (
            patch('kombu.Connection', return_value=_ReachableConnection()),
            patch.object(score_batch_task, 'apply_async', side_effect=_queued_task),
        ):
            resumed = client.post(f'/api/llm-judge/batches/{batch.pk}/resume/', format='json')
        assert resumed.status_code == 200
        batch.refresh_from_db()
        resumed_task_id = batch.celery_task_id
        assert resumed_task_id and resumed_task_id != 'worker-before-pause'
        completed = _run_batch(batch.pk, resumed_task_id)
        assert _run_batch(batch.pk, resumed_task_id)['status'] == 'completed'

    batch.refresh_from_db()
    assert completed['status'] == 'completed'
    assert batch.scored == 1
    assert batch.progress == 100
    assert JudgeRecord.objects.filter(batch=batch).count() == 1
    checkpoint = JudgeBatchCaseResult.objects.get(batch=batch, case_index=0)
    record = JudgeRecord.objects.get(batch=batch)
    assert checkpoint.record_id == record.pk
    assert checkpoint.result['record_id'] == record.pk
    assert record.request_id == f'jb-{batch.pk}-case-0'
    assert request_ids == [record.request_id, record.request_id]
    assert receipts == [None, record.pk]


@pytest.mark.django_db
def test_pause_during_second_score_summarizes_prior_checkpoint():
    user = User.objects.create_user(username='pause-summary-owner')
    rubric = Rubric.objects.create(name='Pause summary rubric', is_default=True)
    batch = JudgeBatch.objects.create(
        rubric=rubric,
        cases_data=[
            {'question': 'first question', 'answer': 'first answer'},
            {'question': 'second question', 'answer': 'second answer'},
        ],
        total=2,
        status='running',
        celery_task_id='pause-summary-worker',
        created_by=user,
    )
    client = APIClient()
    client.force_authenticate(user)
    scored_cases = []
    summarized_results = []

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            scored_cases.append(case['question'])
            if case['question'] == 'second question':
                with patch.object(score_batch_task, 'AsyncResult'):
                    response = client.post(
                        f'/api/llm-judge/batches/{batch.pk}/pause/', format='json')
                assert response.status_code == 200
                assert response.data['status'] == 'paused'
            fields = _record_fields(case, batch, request_id)
            result = _valid_batch_score(fields, {})
            checkpoint_callback(fields, result)
            return result

        def summarize_batch(self, results):
            summarized_results.append(results)
            return {
                'mean_score': 84.0,
                'std_dev': 2.0,
                'safety_pass_rate': 1.0,
                'critical_success_rate': 1.0,
                'gate_zone': 'yellow',
                'blocked': False,
            }

    with patch('apps.llm_judge.service.JudgeService', JudgeService):
        result = _run_batch(batch.pk, 'pause-summary-worker')

    batch.refresh_from_db()
    assert result['status'] == 'paused'
    assert scored_cases == ['first question', 'second question']
    assert len(summarized_results) == 1
    assert len(summarized_results[0]) == 1
    assert batch.status == 'paused'
    assert batch.scored == 1
    assert batch.progress == 50
    assert batch.mean_score == 84.0
    assert batch.gate_zone == 'yellow'
    assert batch.blocked is False
    assert batch.worker_attempt_token == ''
    assert JudgeBatchCaseResult.objects.filter(batch=batch).count() == 1
    assert JudgeRecord.objects.filter(batch=batch).count() == 1


@pytest.mark.django_db(transaction=True)
def test_duplicate_delivery_with_same_task_id_claims_one_worker_attempt():
    user = User.objects.create_user(username='duplicate-batch-owner')
    rubric = Rubric.objects.create(name='Duplicate delivery rubric', is_default=True)
    batch = JudgeBatch.objects.create(
        rubric=rubric,
        cases_data=[{'question': 'single question', 'answer': 'answer'}],
        total=1,
        status='running',
        celery_task_id='duplicate-worker-id',
        created_by=user,
    )
    scoring_started = Event()
    allow_score_to_finish = Event()
    scored_cases = []

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            scored_cases.append(case['question'])
            scoring_started.set()
            if not allow_score_to_finish.wait(timeout=10):
                raise TimeoutError('test did not release the active score')
            fields = _record_fields(case, batch, request_id)
            checkpoint_callback(fields, _valid_batch_score(fields, {}))

        def summarize_batch(self, _results):
            return _summary()

    def run_in_worker_thread():
        close_old_connections()
        try:
            return _run_batch(batch.pk, 'duplicate-worker-id')
        finally:
            close_old_connections()

    with patch('apps.llm_judge.service.JudgeService', JudgeService):
        with ThreadPoolExecutor(max_workers=1) as pool:
            first_worker = pool.submit(run_in_worker_thread)
            assert scoring_started.wait(timeout=5)
            batch.refresh_from_db()
            claimed_token = batch.worker_attempt_token
            assert claimed_token

            duplicate = _run_batch(batch.pk, 'duplicate-worker-id')
            assert duplicate['status'] == 'already_running'
            batch.refresh_from_db()
            assert batch.worker_attempt_token == claimed_token
            assert scored_cases == ['single question']

            allow_score_to_finish.set()
            completed = first_worker.result(timeout=10)

    batch.refresh_from_db()
    assert completed['status'] == 'completed'
    assert batch.scored == 1
    assert batch.worker_attempt_token == ''
    assert JudgeBatchCaseResult.objects.filter(batch=batch).count() == 1
    assert JudgeRecord.objects.filter(batch=batch).count() == 1


@pytest.mark.django_db
def test_batch_create_does_not_reopen_completed_batch_after_eager_dispatch():
    user = User.objects.create_user(username='eager-batch-owner')
    Rubric.objects.create(name='Eager dispatch rubric', is_default=True)
    client = APIClient()
    client.force_authenticate(user)
    eager_result = {}

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            fields = _record_fields(case, batch, request_id)
            result = _valid_batch_score(fields, {})
            checkpoint_callback(fields, result)
            return result

        def summarize_batch(self, _results):
            return _summary()

    def eager_dispatch(args, **kwargs):
        eager_result.update(_run_batch(args[0], kwargs['task_id']))
        return SimpleNamespace(id=kwargs['task_id'])

    with (
        patch('kombu.Connection', return_value=_ReachableConnection()),
        patch.object(score_batch_task, 'apply_async', side_effect=eager_dispatch),
        patch('apps.llm_judge.service.JudgeService', JudgeService),
    ):
        response = client.post('/api/llm-judge/batches/', {
            'cases': [{'question': 'eager question', 'answer': 'answer'}],
        }, format='json')

    assert response.status_code == 201, response.data
    batch = JudgeBatch.objects.get(pk=response.data['id'])
    assert eager_result['status'] == 'completed'
    assert batch.status == 'completed'
    assert batch.scored == 1
    assert batch.worker_attempt_token == ''
    assert 'worker_attempt_token' not in response.data


@pytest.mark.django_db
def test_batch_resume_does_not_reopen_completed_batch_after_eager_dispatch():
    user = User.objects.create_user(username='eager-resume-owner')
    rubric = Rubric.objects.create(name='Eager resume rubric', is_default=True)
    batch = JudgeBatch.objects.create(
        rubric=rubric,
        cases_data=[{'question': 'resume question', 'answer': 'answer'}],
        total=1,
        status='paused',
        is_paused=True,
        created_by=user,
    )
    client = APIClient()
    client.force_authenticate(user)

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            fields = _record_fields(case, batch, request_id)
            result = _valid_batch_score(fields, {})
            checkpoint_callback(fields, result)
            return result

        def summarize_batch(self, _results):
            return _summary()

    def eager_dispatch(args, **kwargs):
        result = _run_batch(args[0], kwargs['task_id'])
        assert result['status'] == 'completed'
        return SimpleNamespace(id=kwargs['task_id'])

    with (
        patch('kombu.Connection', return_value=_ReachableConnection()),
        patch.object(score_batch_task, 'apply_async', side_effect=eager_dispatch),
        patch('apps.llm_judge.service.JudgeService', JudgeService),
    ):
        response = client.post(f'/api/llm-judge/batches/{batch.pk}/resume/', format='json')

    assert response.status_code == 200, response.data
    batch.refresh_from_db()
    assert batch.status == 'completed'
    assert batch.scored == 1
    assert batch.worker_attempt_token == ''
    assert response.data['status'] == 'completed'


@pytest.mark.django_db
def test_old_fallback_exception_cannot_overwrite_new_worker_attempt(monkeypatch):
    user = User.objects.create_user(username='fallback-fence-owner')
    Rubric.objects.create(name='Fallback fence rubric', is_default=True)
    client = APIClient()
    client.force_authenticate(user)
    deferred_threads = []

    def fail_old_fallback(*_args, **_kwargs):
        raise RuntimeError('late fallback failure')

    fake_task = SimpleNamespace(__wrapped__=SimpleNamespace(__func__=fail_old_fallback))

    class DeferredThread:
        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            deferred_threads.append(self)

    monkeypatch.setattr('apps.llm_judge.tasks.score_batch_task', fake_task)
    monkeypatch.setattr('threading.Thread', DeferredThread)
    monkeypatch.setattr('django.db.close_old_connections', lambda: None)

    with patch('kombu.Connection', side_effect=RuntimeError('broker unavailable')):
        response = client.post('/api/llm-judge/batches/', {
            'cases': [{'question': 'fallback question', 'answer': 'answer'}],
        }, format='json')
    assert response.status_code == 201, response.data
    assert len(deferred_threads) == 1

    batch = JudgeBatch.objects.get(pk=response.data['id'])
    JudgeBatch.objects.filter(pk=batch.pk).update(
        status='running',
        celery_task_id='new-worker-attempt',
        worker_attempt_token='new-attempt-token',
        error_message='new attempt state',
    )
    deferred_threads[0].target()

    batch.refresh_from_db()
    assert batch.status == 'running'
    assert batch.celery_task_id == 'new-worker-attempt'
    assert batch.worker_attempt_token == 'new-attempt-token'
    assert batch.error_message == 'new attempt state'


@pytest.mark.django_db
def test_pause_during_final_summary_persists_computed_checkpoint_summary():
    user = User.objects.create_user(username='pause-final-summary-owner')
    rubric = Rubric.objects.create(name='Pause final summary rubric', is_default=True)
    batch = JudgeBatch.objects.create(
        rubric=rubric,
        cases_data=[{'question': 'final-summary question', 'answer': 'answer'}],
        total=1,
        status='running',
        celery_task_id='final-summary-worker',
        created_by=user,
    )
    client = APIClient()
    client.force_authenticate(user)
    summaries = []
    expected = {
        'mean_score': 72.0, 'std_dev': 4.0, 'safety_pass_rate': 0.75,
        'critical_success_rate': 0.8, 'gate_zone': 'yellow', 'blocked': False,
    }

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            fields = _record_fields(case, batch, request_id)
            checkpoint_callback(fields, _valid_batch_score(fields, {}))

        def summarize_batch(self, results):
            summaries.append(results)
            with patch.object(score_batch_task, 'AsyncResult'):
                response = client.post(f'/api/llm-judge/batches/{batch.pk}/pause/', format='json')
            assert response.status_code == 200
            assert response.data['status'] == 'paused'
            return expected

    with patch('apps.llm_judge.service.JudgeService', JudgeService):
        result = _run_batch(batch.pk, 'final-summary-worker')

    batch.refresh_from_db()
    assert result['status'] == 'paused'
    assert len(summaries) == 1
    assert len(summaries[0]) == 1
    assert batch.scored == 1
    assert batch.mean_score == expected['mean_score']
    assert batch.std_dev == expected['std_dev']
    assert batch.safety_pass_rate == expected['safety_pass_rate']
    assert batch.critical_success_rate == expected['critical_success_rate']
    assert batch.gate_zone == expected['gate_zone']
    assert batch.blocked is expected['blocked']
    assert batch.worker_attempt_token == ''
    assert batch.worker_lease_expires_at is None


@pytest.mark.django_db(transaction=True)
def test_resume_before_old_inflight_summary_returns_recomputes_and_fences_old_summary():
    user = User.objects.create_user(username='summary-resume-race-owner')
    rubric = Rubric.objects.create(name='Summary resume race rubric', is_default=True)
    batch = JudgeBatch.objects.create(
        rubric=rubric,
        cases_data=[{'question': 'summary-race question', 'answer': 'answer'}],
        total=1,
        status='running',
        celery_task_id='summary-race-old-worker',
        created_by=user,
    )
    client = APIClient()
    client.force_authenticate(user)
    old_summary_started = Event()
    release_old_summary = Event()
    summary_calls = []
    calls_lock = Lock()
    expected = {
        'mean_score': 93.0, 'std_dev': 0.0, 'safety_pass_rate': 1.0,
        'critical_success_rate': 1.0, 'gate_zone': 'green', 'blocked': False,
    }
    stale = {
        'mean_score': 10.0, 'std_dev': 0.0, 'safety_pass_rate': 0.0,
        'critical_success_rate': 0.0, 'gate_zone': 'red', 'blocked': True,
    }

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            fields = _record_fields(case, batch, request_id)
            checkpoint_callback(fields, _valid_batch_score(fields, {}))

        def summarize_batch(self, _results):
            with calls_lock:
                summary_calls.append(len(summary_calls) + 1)
                call_number = summary_calls[-1]
            if call_number == 1:
                old_summary_started.set()
                if not release_old_summary.wait(timeout=10):
                    raise TimeoutError('test did not release the old summary')
                return stale
            return expected

    with patch('apps.llm_judge.service.JudgeService', JudgeService):
        with ThreadPoolExecutor(max_workers=1) as pool:
            old_worker = pool.submit(
                _run_batch_in_thread, batch.pk, 'summary-race-old-worker',
            )
            try:
                assert old_summary_started.wait(timeout=5)
                batch.refresh_from_db()
                assert batch.scored == 1
                assert batch.worker_attempt_token

                with patch.object(score_batch_task, 'AsyncResult'):
                    paused = client.post(
                        f'/api/llm-judge/batches/{batch.pk}/pause/', format='json',
                    )
                assert paused.status_code == 200
                assert paused.data['status'] == 'paused'

                with (
                    patch('kombu.Connection', return_value=_ReachableConnection()),
                    patch.object(score_batch_task, 'apply_async', side_effect=_queued_task),
                ):
                    resumed = client.post(
                        f'/api/llm-judge/batches/{batch.pk}/resume/', format='json',
                    )
                assert resumed.status_code == 200
                assert resumed.data['status'] == 'running'
                batch.refresh_from_db()
                new_task_id = batch.celery_task_id
                assert new_task_id != 'summary-race-old-worker'
                assert batch.worker_attempt_token == ''

                new_result = _run_batch(batch.pk, new_task_id)
                assert new_result['status'] == 'completed'
                release_old_summary.set()
                old_result = old_worker.result(timeout=10)
            finally:
                release_old_summary.set()

    batch.refresh_from_db()
    assert old_result['status'] == 'superseded'
    assert summary_calls == [1, 2]
    assert batch.status == 'completed'
    assert batch.mean_score == expected['mean_score']
    assert batch.gate_zone == expected['gate_zone']
    assert batch.blocked is expected['blocked']
    assert batch.worker_attempt_token == ''
    assert JudgeBatchCaseResult.objects.filter(batch=batch).count() == 1
    assert JudgeRecord.objects.filter(batch=batch).count() == 1


@pytest.mark.django_db(transaction=True)
def test_worker_heartbeat_renews_live_lease_and_cannot_revive_an_expired_lease(monkeypatch):
    batch = JudgeBatch.objects.create(
        cases_data=[{'question': 'heartbeat question', 'answer': 'answer'}],
        total=1,
        status='running',
        celery_task_id='heartbeat-worker',
        worker_attempt_token='heartbeat-token',
    )
    initial_heartbeat = timezone.now() - timedelta(seconds=10)
    JudgeBatch.objects.filter(pk=batch.pk).update(worker_heartbeat_at=initial_heartbeat)
    renewed = Event()
    original_renew = _renew_worker_lease

    def observe_renewal(batch_id, request_id, attempt_token, now=None):
        result = original_renew(batch_id, request_id, attempt_token, now=now)
        if result:
            renewed.set()
        return result

    monkeypatch.setattr('apps.llm_judge.tasks._renew_worker_lease', observe_renewal)
    monkeypatch.setattr('apps.llm_judge.tasks.WORKER_HEARTBEAT_INTERVAL_SECONDS', 0.01)
    heartbeat = _WorkerLeaseHeartbeat(batch.pk, 'heartbeat-worker', 'heartbeat-token')
    heartbeat.start()
    try:
        assert renewed.wait(timeout=3)
    finally:
        heartbeat.stop()

    batch.refresh_from_db()
    assert batch.worker_heartbeat_at > initial_heartbeat
    assert batch.worker_lease_expires_at > timezone.now()
    expired_at = batch.worker_lease_expires_at + timedelta(seconds=1)
    assert original_renew(
        batch.pk, 'heartbeat-worker', 'heartbeat-token', now=expired_at,
    ) is False


@pytest.mark.django_db(transaction=True)
def test_expired_worker_recovers_from_checkpoint_and_fences_old_worker_result():
    user = User.objects.create_user(username='stale-worker-recovery-owner')
    rubric = Rubric.objects.create(name='Stale worker recovery rubric', is_default=True)
    batch = JudgeBatch.objects.create(
        rubric=rubric,
        cases_data=[
            {'question': 'already checkpointed', 'answer': 'answer 0'},
            {'question': 'in-flight case', 'answer': 'answer 1'},
        ],
        total=2,
        scored=1,
        progress=50,
        status='running',
        celery_task_id='lost-worker-id',
        created_by=user,
    )
    JudgeBatchCaseResult.objects.create(
        batch=batch,
        case_index=0,
        result={'final_score': 80.0, 'overall_label': 'acceptable', 'vetoed': False},
    )
    old_score_started = Event()
    release_old_score = Event()
    score_calls = []
    calls_lock = Lock()

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            with calls_lock:
                score_calls.append(case['question'])
                call_number = len(score_calls)
            if call_number == 1:
                old_score_started.set()
                if not release_old_score.wait(timeout=10):
                    raise TimeoutError('test did not release the old case score')
            fields = _record_fields(case, batch, request_id)
            checkpoint_callback(fields, _valid_batch_score(fields, {}))

        def summarize_batch(self, _results):
            return _summary()

    with patch('apps.llm_judge.service.JudgeService', JudgeService):
        with ThreadPoolExecutor(max_workers=1) as pool:
            old_worker = pool.submit(_run_batch_in_thread, batch.pk, 'lost-worker-id')
            try:
                assert old_score_started.wait(timeout=5)
                batch.refresh_from_db()
                old_token = batch.worker_attempt_token
                assert old_token
                duplicate = _run_batch(batch.pk, 'lost-worker-id')
                assert duplicate['status'] == 'already_running'
                assert reconcile_stale_batches(now=timezone.now()) == 0

                recovery_now = timezone.now() + timedelta(seconds=2)
                JudgeBatch.objects.filter(pk=batch.pk).update(
                    worker_lease_expires_at=recovery_now - timedelta(seconds=1),
                )
                with patch.object(score_batch_task, 'apply_async', side_effect=_queued_task) as dispatch:
                    assert reconcile_stale_batches(now=recovery_now) == 1
                    batch.refresh_from_db()
                    new_task_id = batch.celery_task_id
                    new_token = batch.worker_attempt_token
                    assert new_task_id.startswith('judge-recovery-')
                    assert new_token and new_token != old_token
                    assert batch.worker_recovery_attempts == 1
                    assert batch.worker_heartbeat_at is None
                    assert batch.worker_lease_expires_at is None
                    assert reconcile_stale_batches(now=recovery_now) == 0
                    dispatch.assert_called_once()

                recovered = _run_batch(batch.pk, new_task_id, new_token)
                assert recovered['status'] == 'completed'
                release_old_score.set()
                old_result = old_worker.result(timeout=10)
            finally:
                release_old_score.set()

    batch.refresh_from_db()
    assert old_result['status'] == 'superseded'
    assert score_calls == ['in-flight case', 'in-flight case']
    assert batch.status == 'completed'
    assert batch.scored == 2
    assert batch.worker_recovery_attempts == 0
    assert batch.worker_attempt_token == ''
    assert [row.case_index for row in batch.case_results.all()] == [0, 1]
    assert JudgeRecord.objects.filter(batch=batch).count() == 1


@pytest.mark.django_db
def test_resume_refuses_live_running_lease_but_reclaims_expired_running_batch():
    user = User.objects.create_user(username='manual-stale-resume-owner')
    batch = JudgeBatch.objects.create(
        cases_data=[{'question': 'manual resume question', 'answer': 'answer'}],
        total=1,
        status='running',
        celery_task_id='manual-resume-old-worker',
        worker_attempt_token='manual-resume-token',
        created_by=user,
    )
    client = APIClient()
    client.force_authenticate(user)

    with (
        patch('kombu.Connection', return_value=_ReachableConnection()),
        patch.object(score_batch_task, 'apply_async', side_effect=_queued_task) as dispatch,
    ):
        live = client.post(f'/api/llm-judge/batches/{batch.pk}/resume/', format='json')
        assert live.status_code == 400
        dispatch.assert_not_called()

        JudgeBatch.objects.filter(pk=batch.pk).update(
            worker_lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        stale = client.post(f'/api/llm-judge/batches/{batch.pk}/resume/', format='json')

    assert stale.status_code == 200, stale.data
    batch.refresh_from_db()
    assert batch.status == 'running'
    assert batch.celery_task_id != 'manual-resume-old-worker'
    assert batch.worker_attempt_token == ''
    assert batch.worker_heartbeat_at is None
    assert batch.worker_lease_expires_at is None
    assert batch.worker_recovery_attempts == 0
    assert dispatch.call_count == 1
    with patch.object(score_batch_task, 'apply_async', side_effect=_queued_task) as duplicate_dispatch:
        waiting = client.post(f'/api/llm-judge/batches/{batch.pk}/resume/', format='json')
    assert waiting.status_code == 400
    duplicate_dispatch.assert_not_called()
    assert 'worker_attempt_token' not in stale.data
    assert 'worker_heartbeat_at' not in stale.data
    assert 'worker_lease_expires_at' not in stale.data
    assert 'worker_recovery_attempts' not in stale.data


@pytest.mark.django_db
def test_stale_recovery_broker_failure_is_bounded_by_publisher_lease_and_attempt_limit():
    now = timezone.now()
    batch = JudgeBatch.objects.create(
        cases_data=[{'question': 'broker failure question', 'answer': 'answer'}],
        total=1,
        status='running',
        celery_task_id='broker-failure-worker',
        worker_attempt_token='broker-failure-token',
        worker_lease_expires_at=now - timedelta(seconds=1),
    )

    second_batch = JudgeBatch.objects.create(
        cases_data=[{'question': 'second broker failure question', 'answer': 'answer'}],
        total=1,
        status='running',
        celery_task_id='broker-failure-worker-2',
        worker_attempt_token='broker-failure-token-2',
        worker_lease_expires_at=now - timedelta(seconds=1),
    )

    with patch.object(score_batch_task, 'apply_async', side_effect=RuntimeError('broker down')) as dispatch:
        assert reconcile_stale_batches(now=now) == 0
        assert dispatch.call_count == 1
        batch.refresh_from_db()
        second_batch.refresh_from_db()
        assert batch.worker_lease_expires_at > now
        assert second_batch.worker_lease_expires_at <= now
        JudgeBatch.objects.filter(pk=second_batch.pk).update(
            worker_lease_expires_at=now + timedelta(seconds=120),
        )
        assert reconcile_stale_batches(now=now) == 0
        dispatch.assert_called_once()

    batch.refresh_from_db()
    second_batch.refresh_from_db()
    assert batch.status == 'running'
    assert batch.worker_attempt_token
    assert batch.worker_heartbeat_at is None
    assert batch.worker_lease_expires_at > now
    assert batch.worker_recovery_attempts == 1
    assert second_batch.status == 'running'
    assert second_batch.celery_task_id == 'broker-failure-worker-2'
    assert second_batch.worker_lease_expires_at > now
    assert second_batch.worker_recovery_attempts == 0

    JudgeBatch.objects.filter(pk=batch.pk).update(
        worker_lease_expires_at=now - timedelta(seconds=1),
        worker_recovery_attempts=3,
    )
    assert reconcile_stale_batches(now=now) == 0
    batch.refresh_from_db()
    assert batch.status == 'failed'
    assert batch.worker_attempt_token == ''
    assert batch.worker_lease_expires_at is None
    assert batch.error_message == 'worker lease expired after maximum recovery attempts'
    with patch('apps.llm_judge.service.JudgeService') as judge_service:
        stale_delivery = _run_batch(batch.pk, 'broker-failure-worker')
    judge_service.assert_not_called()
    batch.refresh_from_db()
    assert stale_delivery['status'] == 'failed'
    assert batch.status == 'failed'
    assert batch.celery_task_id == ''
    assert batch.worker_attempt_token == ''
    assert batch.worker_recovery_attempts == 3
    assert batch.scored == 0


@pytest.mark.django_db(transaction=True)
def test_recovery_delivery_with_same_token_cannot_claim_during_inflight_score():
    task_id = 'judge-recovery-duplicate-delivery'
    claim_token = 'recovery-claim-token'
    batch = JudgeBatch.objects.create(
        cases_data=[{'question': 'recovery duplicate question', 'answer': 'answer'}],
        total=1,
        status='running',
        celery_task_id=task_id,
        worker_attempt_token=claim_token,
        worker_heartbeat_at=None,
        worker_lease_expires_at=worker_lease_deadline(),
    )
    scoring_started = Event()
    release_score = Event()
    score_calls = []

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            score_calls.append(case['question'])
            scoring_started.set()
            if not release_score.wait(timeout=10):
                raise TimeoutError('test did not release the recovery score')
            fields = _record_fields(case, batch, request_id)
            checkpoint_callback(fields, _valid_batch_score(fields, {}))

        def summarize_batch(self, _results):
            return _summary()

    with patch('apps.llm_judge.service.JudgeService', JudgeService):
        with ThreadPoolExecutor(max_workers=1) as pool:
            first_delivery = pool.submit(
                _run_batch_in_thread, batch.pk, task_id, claim_token,
            )
            try:
                assert scoring_started.wait(timeout=5)
                batch.refresh_from_db()
                assert batch.worker_attempt_token == claim_token
                assert batch.worker_heartbeat_at is not None

                duplicate = _run_batch(batch.pk, task_id, claim_token)
                assert duplicate['status'] == 'already_running'
                assert score_calls == ['recovery duplicate question']

                release_score.set()
                completed = first_delivery.result(timeout=10)
            finally:
                release_score.set()

    batch.refresh_from_db()
    assert completed['status'] == 'completed'
    assert score_calls == ['recovery duplicate question']
    assert batch.scored == 1
    assert JudgeBatchCaseResult.objects.filter(batch=batch).count() == 1
    assert JudgeRecord.objects.filter(batch=batch).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('claim_token', 'recovery_attempts'),
    [(None, 0), ('acked-recovery-token', 2)],
)
def test_acked_queued_batch_waits_beyond_publisher_lease_until_worker_claims(
    claim_token, recovery_attempts,
):
    assert score_batch_task.acks_late is True
    assert score_batch_task.reject_on_worker_lost is True
    batch = JudgeBatch.objects.create(
        cases_data=[{'question': 'long queued question', 'answer': 'answer'}],
        total=1,
        status='running',
        celery_task_id='acked-long-queue-task',
        worker_attempt_token=claim_token or '',
        worker_heartbeat_at=None,
        worker_lease_expires_at=None,
        worker_recovery_attempts=recovery_attempts,
    )
    scored_questions = []

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            scored_questions.append(case['question'])
            fields = _record_fields(case, batch, request_id)
            checkpoint_callback(fields, _valid_batch_score(fields, {}))

        def summarize_batch(self, _results):
            return _summary()

    much_later = timezone.now() + timedelta(seconds=3 * PUBLISHER_LEASE_SECONDS)
    with patch.object(score_batch_task, 'apply_async') as dispatch:
        assert reconcile_stale_batches(now=much_later) == 0
        dispatch.assert_not_called()
        batch.refresh_from_db()
        assert batch.worker_attempt_token == (claim_token or '')
        assert batch.worker_heartbeat_at is None
        assert batch.worker_lease_expires_at is None
        assert batch.worker_recovery_attempts == recovery_attempts

    with patch('apps.llm_judge.service.JudgeService', JudgeService):
        result = _run_batch(batch.pk, batch.celery_task_id, claim_token)

    batch.refresh_from_db()
    assert result['status'] == 'completed'
    assert scored_questions == ['long queued question']
    assert batch.worker_recovery_attempts == 0
    assert batch.scored == 1


@pytest.mark.django_db
def test_initial_publish_lease_expiry_recovers_lost_publish_and_fences_original_message():
    now = timezone.now()
    original_task_id = 'judge-initial-publish-lost'
    batch = JudgeBatch.objects.create(
        cases_data=[{'question': 'initial lost publish question', 'answer': 'answer'}],
        total=1,
        status='running',
        celery_task_id=original_task_id,
        worker_heartbeat_at=None,
        worker_lease_expires_at=now - timedelta(seconds=1),
    )

    with patch.object(score_batch_task, 'apply_async', side_effect=_queued_task) as dispatch:
        assert reconcile_stale_batches(now=now) == 1
        dispatch.assert_called_once()

    batch.refresh_from_db()
    replacement_task_id = batch.celery_task_id
    replacement_token = batch.worker_attempt_token
    assert replacement_task_id != original_task_id
    assert replacement_token
    assert batch.worker_heartbeat_at is None
    assert batch.worker_lease_expires_at is None
    assert batch.worker_recovery_attempts == 1
    stale = _run_batch(batch.pk, original_task_id)
    assert stale['status'] == 'superseded'

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            fields = _record_fields(case, batch, request_id)
            checkpoint_callback(fields, _valid_batch_score(fields, {}))

        def summarize_batch(self, _results):
            return _summary()

    with patch('apps.llm_judge.service.JudgeService', JudgeService):
        recovered = _run_batch(batch.pk, replacement_task_id, replacement_token)
    assert recovered['status'] == 'completed'


@pytest.mark.django_db
def test_expired_active_worker_recovers_after_recovery_publish_lease_expires():
    now = timezone.now()
    batch = JudgeBatch.objects.create(
        cases_data=[{'question': 'recovery lost publish question', 'answer': 'answer'}],
        total=1,
        status='running',
        celery_task_id='judge-active-expired-worker',
        worker_attempt_token='expired-active-token',
        worker_heartbeat_at=now - timedelta(minutes=3),
        worker_lease_expires_at=now - timedelta(seconds=1),
    )

    with patch.object(score_batch_task, 'apply_async', side_effect=RuntimeError('publisher crashed')):
        assert reconcile_stale_batches(now=now) == 0
    batch.refresh_from_db()
    first_recovery_id = batch.celery_task_id
    first_recovery_token = batch.worker_attempt_token
    first_lease = batch.worker_lease_expires_at
    assert first_recovery_id.startswith('judge-recovery-')
    assert first_recovery_token != 'expired-active-token'
    assert batch.worker_heartbeat_at is None
    assert first_lease > now
    assert batch.worker_recovery_attempts == 1

    retry_time = first_lease + timedelta(seconds=1)
    with patch.object(score_batch_task, 'apply_async', side_effect=_queued_task) as dispatch:
        assert reconcile_stale_batches(now=retry_time) == 1
        dispatch.assert_called_once()

    batch.refresh_from_db()
    final_task_id = batch.celery_task_id
    final_token = batch.worker_attempt_token
    assert final_task_id != first_recovery_id
    assert final_token != first_recovery_token
    assert batch.worker_heartbeat_at is None
    assert batch.worker_lease_expires_at is None
    assert batch.worker_recovery_attempts == 2
    assert _run_batch(batch.pk, first_recovery_id, first_recovery_token)['status'] == 'superseded'

    class JudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            fields = _record_fields(case, batch, request_id)
            checkpoint_callback(fields, _valid_batch_score(fields, {}))

        def summarize_batch(self, _results):
            return _summary()

    with patch('apps.llm_judge.service.JudgeService', JudgeService):
        result = _run_batch(batch.pk, final_task_id, final_token)

    batch.refresh_from_db()
    assert result['status'] == 'completed'
    assert batch.worker_recovery_attempts == 0
    assert batch.scored == 1
