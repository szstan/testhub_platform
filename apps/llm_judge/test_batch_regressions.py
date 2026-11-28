from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.llm_judge.models import (
    JudgeBatch,
    JudgeBatchCaseResult,
    JudgeRecord,
    Rubric,
    RubricDimension,
    RubricRule,
)
from apps.llm_judge.serializers import JudgeBatchSerializer, RubricCreateSerializer
from apps.llm_judge.tasks import score_batch_task, worker_lease_deadline
from apps.users.models import User


class _ReachableConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ensure_connection(self, **_kwargs):
        return None


class _FakeWorker:
    def __init__(self, task_id):
        self.request = SimpleNamespace(id=task_id)

    def update_state(self, **_kwargs):
        return None


def _run_batch(batch_id, task_id, claim_token=None):
    return score_batch_task.__wrapped__.__func__(
        _FakeWorker(task_id), batch_id, claim_token,
    )


def _valid_batch_score(record_fields, response):
    return {
        'request_id': record_fields['request_id'],
        'record_id': None,
        'final_score': 90.0,
        'overall_label': 'excellent',
        'gate_zone': 'green',
        'vetoed': False,
    }


def _record_fields(case, batch, request_id):
    return {
        'request_id': request_id,
        'rubric_id': case.get('rubric'),
        'batch': batch,
        'question': case['question'],
        'answer': case['answer'],
        'created_by': batch.created_by,
        'final_score': 90.0,
        'overall_label': 'excellent',
        'gate_zone': 'green',
        'blocked': False,
        'rule_findings': [],
        'verdict_dimensions': [],
    }


@pytest.mark.django_db
def test_batch_rejects_case_rubric_hidden_from_regular_user():
    user = User.objects.create_user(username='batch-rubric-regular', password='pass')
    Rubric.objects.create(name='batch-visible-default', is_default=True)
    hidden = Rubric.objects.create(name='batch-hidden-rubric', is_default=False)
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post(
        '/api/llm-judge/batches/',
        {'cases': [{'question': 'q', 'answer': 'a', 'rubric': hidden.pk}]},
        format='json',
    )

    assert response.status_code == 400
    assert JudgeBatch.objects.count() == 0


@pytest.mark.django_db
def test_batch_staff_can_use_active_non_default_case_rubric():
    staff = User.objects.create_user(username='batch-rubric-staff', password='pass', is_staff=True)
    default = Rubric.objects.create(name='batch-selected-default', is_default=True)
    private = Rubric.objects.create(name='batch-staff-private', is_default=False)
    client = APIClient()
    client.force_authenticate(user=staff)
    async_result = SimpleNamespace(id='batch-staff-task')

    with patch('kombu.Connection', return_value=_ReachableConnection()):
        with patch('apps.llm_judge.tasks.score_batch_task.apply_async', return_value=async_result):
            response = client.post(
                '/api/llm-judge/batches/',
                {
                    'rubric': default.pk,
                    'cases': [{'question': 'q', 'answer': 'a', 'rubric': private.pk}],
                },
                format='json',
            )

    assert response.status_code == 201
    batch = JudgeBatch.objects.get(pk=response.data['id'])
    assert batch.rubric_id == default.pk
    assert batch.cases_data[0]['rubric'] == private.pk
    assert batch.worker_heartbeat_at is None
    assert batch.worker_lease_expires_at is None


@pytest.mark.django_db
def test_batch_rubric_overrides_resume_once_and_never_rewrites_results_buffer():
    default = Rubric.objects.create(name='batch-base-rubric', is_default=True)
    override = Rubric.objects.create(name='batch-case-rubric', is_default=False)
    batch = JudgeBatch.objects.create(
        rubric=default,
        cases_data=[
            {'question': 'case-0', 'answer': 'answer-0'},
            {'question': 'case-1', 'answer': 'answer-1', 'rubric': override.pk},
            {'question': 'case-2', 'answer': 'answer-2'},
        ],
        total=3,
        status='running',
        celery_task_id='batch-worker-1',
    )
    scored_rubrics = []
    pause_after_first_checkpoint = {'value': True}

    class FakeJudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            scored_rubrics.append(case['rubric'])
            fields = _record_fields(case, batch, request_id)
            response = _valid_batch_score(fields, {})
            checkpoint_callback(fields, response)
            if case['question'] == 'case-0' and pause_after_first_checkpoint['value']:
                pause_after_first_checkpoint['value'] = False
                JudgeBatch.objects.filter(pk=batch.pk).update(
                    is_paused=True, status='paused', celery_task_id='',
                )
            return response

        def summarize_batch(self, results):
            return {
                'mean_score': 90.0,
                'std_dev': 0.0,
                'safety_pass_rate': 1.0,
                'critical_success_rate': 1.0,
                'gate_zone': 'green',
                'blocked': False,
            }

    with CaptureQueriesContext(connection) as captured:
        with patch('apps.llm_judge.service.JudgeService', FakeJudgeService):
            paused = _run_batch(batch.pk, 'batch-worker-1')
            assert paused['status'] == 'paused'
            batch.refresh_from_db()
            assert batch.scored == 1
            assert JudgeBatchCaseResult.objects.filter(batch=batch).count() == 1
            assert JudgeRecord.objects.filter(batch=batch).count() == 1

            JudgeBatch.objects.filter(pk=batch.pk).update(
                is_paused=False,
                status='running',
                celery_task_id='batch-worker-2',
                worker_heartbeat_at=None,
                worker_lease_expires_at=worker_lease_deadline(),
            )
            completed = _run_batch(batch.pk, 'batch-worker-2')

    batch_updates = [
        query['sql'].lower() for query in captured.captured_queries
        if query['sql'].lstrip().lower().startswith('update')
        and 'llm_judge_batch' in query['sql'].lower()
    ]
    assert batch_updates
    assert all('results_buffer' not in query for query in batch_updates)

    batch.refresh_from_db()
    assert completed['status'] == 'completed'
    assert batch.scored == 3
    assert batch.results_buffer == []
    assert scored_rubrics == [default.pk, override.pk, default.pk]
    assert JudgeBatchCaseResult.objects.filter(batch=batch).count() == 3
    assert JudgeRecord.objects.filter(batch=batch).count() == 3
    assert list(
        JudgeRecord.objects.filter(batch=batch).order_by('request_id').values_list('request_id', flat=True)
    ) == [f'jb-{batch.pk}-case-{index}' for index in range(3)]
    assert len(JudgeBatchSerializer(batch).data['results_buffer']) == 3


@pytest.mark.django_db
def test_batch_resume_converts_existing_json_checkpoint_without_rescoring_it():
    batch = JudgeBatch.objects.create(
        cases_data=[
            {'question': 'legacy-case', 'answer': 'legacy-answer'},
            {'question': 'new-case', 'answer': 'new-answer'},
        ],
        total=2,
        scored=1,
        progress=50,
        results_buffer=[{'request_id': 'legacy-record', 'final_score': 80.0}],
        status='running',
        celery_task_id='legacy-worker',
    )
    scored_questions = []

    class FakeJudgeService:
        def __init__(self, rubric_id=None):
            self.rubric_id = rubric_id

        def score_single(self, case, batch, created_by, request_id, checkpoint_callback):
            scored_questions.append(case['question'])
            fields = _record_fields(case, batch, request_id)
            checkpoint_callback(fields, _valid_batch_score(fields, {}))

        def summarize_batch(self, results):
            return {
                'mean_score': 90.0,
                'std_dev': 0.0,
                'safety_pass_rate': 1.0,
                'critical_success_rate': 1.0,
                'gate_zone': 'green',
                'blocked': False,
            }

    with patch('apps.llm_judge.service.JudgeService', FakeJudgeService):
        result = _run_batch(batch.pk, 'legacy-worker')

    batch.refresh_from_db()
    assert result['status'] == 'completed'
    assert scored_questions == ['new-case']
    assert batch.scored == 2
    assert batch.results_buffer == [{'request_id': 'legacy-record', 'final_score': 80.0}]
    assert [row.result for row in batch.case_results.all()] == [
        {'request_id': 'legacy-record', 'final_score': 80.0},
        {
            'request_id': f'jb-{batch.pk}-case-1',
            'record_id': JudgeRecord.objects.get(batch=batch).pk,
            'final_score': 90.0,
            'overall_label': 'excellent',
            'gate_zone': 'green',
            'vetoed': False,
        },
    ]
    assert JudgeBatchSerializer(batch).data['results_buffer'] == [row.result for row in batch.case_results.all()]


@pytest.mark.django_db
def test_nested_rubric_create_ignores_child_parent_id_and_attaches_to_new_parent():
    existing = Rubric.objects.create(name='existing-nested-parent')
    serializer = RubricCreateSerializer(data={
        'name': 'new-nested-parent',
        'dimensions': [{
            'rubric': existing.pk,
            'dim_key': 'clarity',
            'name': 'Clarity',
        }],
        'rules': [{
            'rubric': existing.pk,
            'rule_key': 'safe',
            'name': 'Safe response',
        }],
    })
    assert serializer.is_valid(), serializer.errors

    created = serializer.save()

    assert list(created.dimensions.values_list('rubric_id', flat=True)) == [created.pk]
    assert list(created.rules.values_list('rubric_id', flat=True)) == [created.pk]
    assert existing.dimensions.count() == 0
    assert existing.rules.count() == 0
    assert serializer.data['dimensions'][0]['rubric'] == created.pk
    assert serializer.data['rules'][0]['rubric'] == created.pk


@pytest.mark.django_db
def test_nested_rubric_create_rolls_back_parent_and_children_on_failure():
    serializer = RubricCreateSerializer(data={
        'name': 'rolled-back-rubric',
        'dimensions': [{'dim_key': 'clarity', 'name': 'Clarity'}],
        'rules': [{'rule_key': 'safe', 'name': 'Safe response'}],
    })
    assert serializer.is_valid(), serializer.errors

    with patch.object(RubricRule.objects, 'create', side_effect=RuntimeError('forced child failure')):
        with pytest.raises(RuntimeError, match='forced child failure'):
            serializer.save()

    assert not Rubric.objects.filter(name='rolled-back-rubric').exists()
    assert not RubricDimension.objects.filter(dim_key='clarity').exists()
    assert not RubricRule.objects.filter(rule_key='safe').exists()


@pytest.mark.django_db
def test_score_service_resets_explicit_none_rubric_after_case_override():
    from apps.llm_judge.judge_engine.models import RuleReport, ScoreResponse
    from apps.llm_judge.judge_engine.service import JudgeService

    rubrics = {
        17: SimpleNamespace(name='private', raw={'name': 'private'}, version='1'),
        None: SimpleNamespace(name='fallback', raw={'name': 'fallback'}, version='1'),
    }
    loaded_ids = []
    used_rubrics = []

    def load_rubric(rubric_id=None, **_kwargs):
        loaded_ids.append(rubric_id)
        return rubrics[rubric_id], rubric_id

    class FakeRules:
        def __init__(self, rubric):
            self.rubric = rubric

        def run(self, _request):
            return RuleReport()

    def build_response(rubric, _request, rule_report, verdict, request_id=None):
        used_rubrics.append(rubric.name)
        return ScoreResponse(
            request_id=request_id,
            rule_report=rule_report,
            verdict=verdict,
            final_score=90.0,
            gate_zone='green',
        )

    config = SimpleNamespace(judge_model='mock-model', cache_timeout=60)
    with patch('apps.llm_judge.judge_engine.service.load_rubric', side_effect=load_rubric):
        with patch('apps.llm_judge.judge_engine.service.RuleEngine', FakeRules):
            with patch('apps.llm_judge.judge_engine.service.get_config', return_value=config):
                with patch('apps.llm_judge.judge_engine.service.get_cached', return_value={
                    'reasoning': 'cached',
                    'dimensions': [],
                    'overall_label': 'acceptable',
                    'model': 'mock-model',
                }):
                    with patch('apps.llm_judge.judge_engine.service.build_response', side_effect=build_response):
                        service = JudgeService()
                        for index, rubric_id in enumerate((17, None)):
                            service.score_single(
                                {
                                    'question': f'q{index}',
                                    'answer': f'a{index}',
                                    'rubric': rubric_id,
                                },
                                request_id=f'test-case-{index}',
                                checkpoint_callback=lambda _fields, _result: None,
                            )

    assert loaded_ids == [17, None]
    assert used_rubrics == ['private', 'fallback']


@pytest.mark.django_db
def test_resume_preserves_failure_status_when_all_cases_are_checkpointed():
    user = User.objects.create_user(username='batch-finished-errors', password='pass')
    batch = JudgeBatch.objects.create(
        cases_data=[{'question': 'q', 'answer': 'a'}],
        total=1,
        scored=1,
        progress=100,
        error_count=1,
        results_buffer=[{'error': 'scoring failed', 'case_index': 0}],
        status='paused',
        is_paused=True,
        created_by=user,
    )
    client = APIClient()
    client.force_authenticate(user=user)

    def eager_dispatch(args, **kwargs):
        _run_batch(args[0], kwargs['task_id'])
        return SimpleNamespace(id=kwargs['task_id'])

    with (
        patch('kombu.Connection', return_value=_ReachableConnection()),
        patch.object(score_batch_task, 'apply_async', side_effect=eager_dispatch),
    ):
        response = client.post(f'/api/llm-judge/batches/{batch.pk}/resume/', format='json')

    batch.refresh_from_db()
    assert response.status_code == 200
    assert batch.status == 'failed'
    assert batch.error_message == '全部 1 条失败'
