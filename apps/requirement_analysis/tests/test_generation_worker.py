import asyncio
import hashlib
import threading
import uuid
from datetime import timedelta
from unittest.mock import Mock

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.management.commands.run_all_scheduled_tasks import Command as ScheduledTasksCommand
from apps.projects.models import Project
from apps.requirement_analysis.generation_worker import MAX_PROVIDER_RETRIES
from apps.requirement_analysis.models import (
    AIModelConfig,
    AIModelService,
    GenerationConfig,
    PromptConfig,
    TestCaseGenerationTask as GenerationTask,
)
from apps.requirement_analysis.tasks import (
    MIN_GENERATION_STALE_AFTER_SECONDS,
    generate_test_case_task,
    recover_stale_generation_tasks,
)
from apps.requirement_analysis.views import (
    GenerationConfigViewSet,
    TestCaseGenerationTaskViewSet as GenerationTaskViewSet,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db(transaction=True)


CASE_TEXT = (
    '| 用例编号 | 测试场景 | 前置条件 | 操作步骤 | 预期结果 | 优先级 |\n'
    '| --- | --- | --- | --- | --- | --- |\n'
    '| TC-001 | Generated case | A test user exists | Submit a valid request | The request succeeds | P1 |\n'
)
REVISED_CASE_TEXT = CASE_TEXT.replace('Generated case', 'Revised case')


def setup_generation(username, *, is_admin=False, auto_review=True, review_timeout=120):
    if is_admin:
        user = User.objects.create_superuser(username=username, email=f'{username}@example.test', password='test-pass')
    else:
        user = User.objects.create_user(username=username, password='test-pass')
    project = Project.objects.create(name=f'{username} project', owner=user)
    writer_model = AIModelConfig.objects.create(
        name=f'{username} writer', model_type='other', role='writer', api_key='',
        base_url='https://example.invalid/v1', model_name='mock-writer', created_by=user,
    )
    reviewer_model = AIModelConfig.objects.create(
        name=f'{username} reviewer', model_type='other', role='reviewer', api_key='',
        base_url='https://example.invalid/v1', model_name='mock-reviewer', created_by=user,
    )
    writer_prompt = PromptConfig.objects.create(
        name=f'{username} writer prompt', prompt_type='writer', content='writer prompt', created_by=user,
    )
    reviewer_prompt = PromptConfig.objects.create(
        name=f'{username} reviewer prompt', prompt_type='reviewer', content='reviewer prompt', created_by=user,
    )
    generation_config = GenerationConfig.objects.create(
        name=f'{username} generation config', enable_auto_review=auto_review,
        review_timeout=review_timeout, is_active=True,
    )
    return user, project, writer_model, reviewer_model, writer_prompt, reviewer_prompt, generation_config


def make_task(user, relations, *, status='pending', output_mode='stream', claim_id='claim-current'):
    _, _, writer_model, reviewer_model, writer_prompt, reviewer_prompt, generation_config = relations
    auto_review_enabled = GenerationConfig.auto_review_enabled_for(generation_config)
    return GenerationTask.objects.create(
        task_id=f'TASK_{uuid.uuid4().hex[:10].upper()}',
        title='Worker test task',
        requirement_text='A mocked requirement.',
        status=status,
        output_mode=output_mode,
        writer_model_config=writer_model,
        reviewer_model_config=reviewer_model if auto_review_enabled else None,
        writer_prompt_config=writer_prompt,
        reviewer_prompt_config=reviewer_prompt if auto_review_enabled else None,
        created_by=user,
        celery_task_id=claim_id,
    )


def post_generate(user, project, *, output_mode='stream', **options):
    project_id = options.pop('project_id', project.pk)
    payload = {
        'title': 'Queued task',
        'requirement_text': 'A mocked requirement.',
        'project': project_id,
        'output_mode': output_mode,
        **options,
    }
    request = APIRequestFactory().post(
        '/api/requirement-analysis/testcase-generation/generate/',
        payload,
        format='json',
    )
    force_authenticate(request, user=user)
    return GenerationTaskViewSet.as_view({'post': 'generate'})(request)


def run_worker(task):
    return generate_test_case_task.run(task.pk, task.celery_task_id)


def get_submission(user, client_request_id):
    request = APIRequestFactory().get(
        '/api/requirement-analysis/testcase-generation/submission/',
        {'client_request_id': client_request_id},
    )
    force_authenticate(request, user=user)
    return GenerationTaskViewSet.as_view({'get': 'submission'})(request)


def test_generate_endpoint_persists_and_dispatches_only_the_database_id(monkeypatch):
    user, project, *_ = setup_generation('generation-dispatch', auto_review=False)
    dispatch = Mock(return_value=None)
    provider_called = Mock(side_effect=AssertionError('provider must not run in the HTTP request'))
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)
    monkeypatch.setattr(AIModelService, 'generate_test_cases_stream', provider_called)

    response = post_generate(user, project)

    assert response.status_code == 201
    task = GenerationTask.objects.get(task_id=response.data['task_id'])
    assert task.status == 'pending'
    assert task.celery_task_id
    dispatch.assert_called_once_with(
        args=(task.pk, task.celery_task_id),
        task_id=task.celery_task_id,
        queue='celery',
        retry=False,
    )
    provider_called.assert_not_called()


def test_generate_replays_same_client_request_id_without_redispatch(monkeypatch):
    user, project, *_ = setup_generation('generation-idempotent-replay', auto_review=False)
    client_request_id = uuid.uuid4()
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    created = post_generate(
        user, project, client_request_id=str(client_request_id), task_id='TASK_CLIENT_OVERRIDE'
    )
    task = GenerationTask.objects.get(created_by=user)
    task.status = 'completed'
    task.save(update_fields=['status'])
    replayed = post_generate(
        user, project, client_request_id=str(client_request_id), task_id='TASK_CLIENT_OVERRIDE'
    )

    expected_digest = hashlib.sha256(f'{user.pk}:{client_request_id}'.encode()).hexdigest()[:32].upper()
    assert created.status_code == 201
    assert created.data['task_id'] == f'TASK_{expected_digest}'
    assert replayed.status_code == 200
    assert set(replayed.data) == {'task_id', 'task', 'replayed'}
    assert replayed.data['task']['status'] == 'completed'
    assert GenerationTask.objects.filter(created_by=user).count() == 1
    dispatch.assert_called_once()


@pytest.mark.parametrize('field', ['title', 'requirement_text', 'project', 'output_mode'])
def test_generate_rejects_conflicting_payload_for_existing_client_request_id(monkeypatch, field):
    user, project, *_ = setup_generation(f'generation-idempotency-conflict-{field}', auto_review=False)
    client_request_id = str(uuid.uuid4())
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)
    created = post_generate(user, project, client_request_id=client_request_id)
    original_task_id = created.data['task_id']

    changes = {
        'title': {'title': 'Different title'},
        'requirement_text': {'requirement_text': 'Different requirement.'},
        'project': {'project_id': Project.objects.create(name='Other project', owner=user).pk},
        'output_mode': {'output_mode': 'complete'},
    }
    conflict = post_generate(user, project, client_request_id=client_request_id, **changes[field])

    assert conflict.status_code == 409
    assert conflict.data['task_id'] == original_task_id
    assert GenerationTask.objects.filter(created_by=user).count() == 1
    dispatch.assert_called_once()


def test_generate_replays_existing_submission_before_model_configuration_validation(monkeypatch):
    user, project, writer_model, *_ = setup_generation(
        'generation-idempotent-no-model', auto_review=False
    )
    client_request_id = str(uuid.uuid4())
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)
    created = post_generate(user, project, client_request_id=client_request_id)

    writer_model.is_active = False
    writer_model.save(update_fields=['is_active'])
    replayed = post_generate(user, project, client_request_id=client_request_id)

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert replayed.data['task_id'] == created.data['task_id']
    assert replayed.data['replayed'] is True
    dispatch.assert_called_once()


def test_generate_creates_distinct_tasks_for_distinct_client_request_ids(monkeypatch):
    user, project, *_ = setup_generation('generation-distinct-client-ids', auto_review=False)
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    first = post_generate(user, project, client_request_id=str(uuid.uuid4()))
    second = post_generate(user, project, client_request_id=str(uuid.uuid4()))

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.data['task_id'] != second.data['task_id']
    assert GenerationTask.objects.filter(created_by=user).count() == 2
    assert dispatch.call_count == 2


def test_submission_lookup_validates_uuid_and_does_not_expose_other_users_task(monkeypatch):
    user, project, *_ = setup_generation('generation-submission-owner', auto_review=False)
    client_request_id = str(uuid.uuid4())
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)
    created = post_generate(user, project, client_request_id=client_request_id)

    recovered = get_submission(user, client_request_id)
    invalid = get_submission(user, 'not-a-uuid')
    other_user = User.objects.create_user(username='generation-submission-other', password='test-pass')
    project.members.add(other_user)
    private_lookup = get_submission(other_user, client_request_id)

    assert created.status_code == 201
    assert recovered.status_code == 200
    assert recovered.data['task_id'] == created.data['task_id']
    assert recovered.data['task']['id'] == created.data['task']['id']
    assert invalid.status_code == 400
    assert 'client_request_id' in invalid.data
    assert private_lookup.status_code == 404
    assert 'task_id' not in private_lookup.data
    dispatch.assert_called_once()


def test_generate_persists_explicit_complete_output_mode(monkeypatch):
    user, project, writer_model, reviewer_model, writer_prompt, reviewer_prompt, _ = setup_generation(
        'generation-complete-output-mode', auto_review=True
    )
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    response = post_generate(user, project, output_mode='complete')

    assert response.status_code == 201
    task = GenerationTask.objects.get(task_id=response.data['task_id'])
    assert task.output_mode == 'complete'
    assert task.writer_model_config_id == writer_model.pk
    assert task.reviewer_model_config_id == reviewer_model.pk
    assert task.writer_prompt_config_id == writer_prompt.pk
    assert task.reviewer_prompt_config_id == reviewer_prompt.pk
    assert response.data['task']['output_mode'] == 'complete'
    dispatch.assert_called_once()


def test_generate_does_not_require_reviewer_when_auto_review_is_disabled(monkeypatch):

    user, project, _, reviewer_model, _, reviewer_prompt, _ = setup_generation(
        'generation-no-review-config', auto_review=False
    )
    reviewer_model.delete()
    reviewer_prompt.delete()
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    response = post_generate(user, project)

    assert response.status_code == 201
    task = GenerationTask.objects.get(task_id=response.data['task_id'])
    assert task.reviewer_model_config_id is None
    assert task.reviewer_prompt_config_id is None
    dispatch.assert_called_once()


def test_generate_requires_reviewer_when_auto_review_is_enabled(monkeypatch):
    user, project, _, reviewer_model, _, reviewer_prompt, _ = setup_generation(
        'generation-review-required', auto_review=True
    )
    reviewer_model.delete()
    reviewer_prompt.delete()
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    response = post_generate(user, project)

    assert response.status_code == 400
    assert response.data['error'] == '未找到可用的测试用例评审模型配置'
    assert GenerationTask.objects.filter(created_by=user).count() == 0
    dispatch.assert_not_called()


def test_generate_uses_review_default_when_no_generation_config_exists(monkeypatch):
    user, project, _, reviewer_model, _, reviewer_prompt, generation_config = setup_generation(
        'generation-default-review', auto_review=True
    )
    generation_config.delete()
    reviewer_model.delete()
    reviewer_prompt.delete()
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    response = post_generate(user, project)

    assert response.status_code == 400
    assert response.data['error'] == '未找到可用的测试用例评审模型配置'
    dispatch.assert_not_called()


def test_generate_rejects_disabling_the_required_writer_model(monkeypatch):
    user, project, *_ = setup_generation('generation-writer-required', auto_review=False)
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    response = post_generate(user, project, use_writer_model=False)

    assert response.status_code == 400
    assert 'use_writer_model' in response.data
    assert GenerationTask.objects.filter(created_by=user).count() == 0
    dispatch.assert_not_called()


def test_generate_cannot_use_another_users_writer_model(monkeypatch):
    user = User.objects.create_user(username='generation-model-caller', password='test-pass')
    project = Project.objects.create(name='generation-model-caller project', owner=user)
    setup_generation('generation-model-owner', auto_review=False)
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    response = post_generate(user, project)

    assert response.status_code == 400
    assert response.data['error'] == '未找到可用的测试用例编写模型配置'
    assert GenerationTask.objects.filter(created_by=user).count() == 0
    dispatch.assert_not_called()


@pytest.mark.parametrize(('field', 'invalid_value'), [('project', 0), ('output_mode', 'invalid')])
def test_generate_rejects_invalid_project_and_output_mode(monkeypatch, field, invalid_value):
    user, project, *_ = setup_generation('generation-invalid-request', auto_review=False)
    payload = {
        'title': 'Invalid request',
        'requirement_text': 'A mocked requirement.',
        'project': project.pk,
        'output_mode': 'stream',
    }
    payload[field] = invalid_value
    request = APIRequestFactory().post(
        '/api/requirement-analysis/testcase-generation/generate/', payload, format='json'
    )
    force_authenticate(request, user=user)
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    response = GenerationTaskViewSet.as_view({'post': 'generate'})(request)

    assert response.status_code == 400
    assert field in response.data
    assert GenerationTask.objects.filter(created_by=user).count() == 0
    dispatch.assert_not_called()


def test_generation_admission_enforces_two_active_tasks_per_user(monkeypatch):
    user, project, *_ = setup_generation('generation-admission', auto_review=False)
    other_user, other_project, *_ = setup_generation('generation-admission-other', auto_review=False)
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    first = post_generate(user, project)
    second = post_generate(user, project)
    rejected = post_generate(user, project)
    other_user_response = post_generate(other_user, other_project)

    assert [first.status_code, second.status_code, rejected.status_code, other_user_response.status_code] == [
        201, 201, 429, 201,
    ]
    assert GenerationTask.objects.filter(
        created_by=user,
        status__in=('pending', 'generating', 'reviewing', 'revising'),
    ).count() == 2
    assert dispatch.call_count == 3


def test_worker_fails_unparseable_output_and_preserves_raw_generation(monkeypatch):
    relations = setup_generation('generation-unparseable-output', auto_review=False)
    task = make_task(relations[0], relations, output_mode='complete')
    raw_output = 'Provider returned prose without any test case.'

    async def writer(_worker_task):
        return raw_output

    monkeypatch.setattr(AIModelService, 'generate_test_cases', writer)

    result = run_worker(task)

    task.refresh_from_db()
    assert result == {'task_id': task.pk, 'status': 'failed', 'stage': 'writer'}
    assert task.status == 'failed'
    assert task.completed_at is None
    assert task.generated_test_cases == raw_output
    assert task.final_test_cases == raw_output
    assert '无法解析为测试用例' in task.error_message


@pytest.mark.parametrize(
    ('raw_output', 'message'),
    [
        ('TC-001: A heading without steps or expected result\n', 'steps'),
        (
            '| Case ID | Title | Preconditions | Steps | Expected result | Priority |\n'
            '| --- | --- | --- | --- | --- | --- |\n'
            '| TC-002 | Blank steps | A user exists |   | A message appears | P2 |\n',
            'steps',
        ),
        (
            '| Case ID | Title | Preconditions | Steps | Expected result | Priority |\n'
            '| --- | --- | --- | --- | --- | --- |\n'
            '| TC-003 | Blank expected | A user exists | Submit invalid input |   | P2 |\n',
            'expected_result',
        ),
    ],
)
def test_worker_fails_unexecutable_cases_and_preserves_raw_generation(monkeypatch, raw_output, message):
    relations = setup_generation(
        f'generation-incomplete-{hashlib.sha1(raw_output.encode()).hexdigest()[:8]}',
        auto_review=False,
    )
    task = make_task(relations[0], relations, output_mode='complete')

    async def writer(_worker_task):
        return raw_output

    monkeypatch.setattr(AIModelService, 'generate_test_cases', writer)

    result = run_worker(task)

    task.refresh_from_db()
    assert result == {'task_id': task.pk, 'status': 'failed', 'stage': 'writer'}
    assert task.status == 'failed'
    assert task.completed_at is None
    assert task.generated_test_cases == raw_output
    assert task.final_test_cases == raw_output
    assert message in task.error_message


def test_broker_dispatch_failure_is_persisted_and_reported(monkeypatch):
    user, project, *_ = setup_generation('generation-dispatch-failure', auto_review=False)
    dispatch = Mock(side_effect=RuntimeError('broker unavailable'))
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    response = post_generate(user, project)

    assert response.status_code == 503
    task = GenerationTask.objects.get(created_by=user)
    assert task.status == 'failed'
    assert 'Celery dispatch failed: broker unavailable' == task.error_message
    assert task.celery_task_id
    dispatch.assert_called_once()


def test_long_running_mock_provider_does_not_block_generation_config_api(monkeypatch):
    user, project, *_ = setup_generation('generation-long-provider', is_admin=True, auto_review=False)
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    provider_started = threading.Event()
    release_provider = threading.Event()

    async def slow_writer(task, callback=None):
        provider_started.set()
        while not release_provider.is_set():
            await asyncio.sleep(0.01)
        if callback:
            await callback(CASE_TEXT)
        return CASE_TEXT

    monkeypatch.setattr(AIModelService, 'generate_test_cases_stream', slow_writer)
    response = post_generate(user, project)
    assert response.status_code == 201
    task = GenerationTask.objects.get(task_id=response.data['task_id'])
    assert not provider_started.is_set()

    worker_result = []
    worker = threading.Thread(target=lambda: worker_result.append(run_worker(task)))
    worker.start()
    try:
        assert provider_started.wait(timeout=2)
        request = APIRequestFactory().get('/api/requirement-analysis/generation-config/active/')
        force_authenticate(request, user=user)
        config_response = GenerationConfigViewSet.as_view({'get': 'active'})(request)
        assert config_response.status_code == 200
    finally:
        release_provider.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert worker_result == [{'task_id': task.pk, 'status': 'completed'}]
    task.refresh_from_db()
    assert task.status == 'completed'


@pytest.mark.parametrize(
    ('auto_review_at_creation', 'auto_review_at_execution'),
    [(True, False), (False, True)],
)
def test_worker_review_policy_uses_task_configuration_snapshot(
    monkeypatch, auto_review_at_creation, auto_review_at_execution
):
    user, project, *_relations, generation_config = setup_generation(
        f'generation-review-snapshot-{auto_review_at_creation}',
        auto_review=auto_review_at_creation,
    )
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)
    response = post_generate(user, project)
    task = GenerationTask.objects.get(task_id=response.data['task_id'])
    assert bool(task.reviewer_model_config_id and task.reviewer_prompt_config_id) is auto_review_at_creation
    assert bool(response.data['task']['reviewer_model_config']) is auto_review_at_creation

    generation_config.enable_auto_review = auto_review_at_execution
    generation_config.save(update_fields=['enable_auto_review'])
    review_calls = []

    async def writer(_task, callback=None):
        await callback(CASE_TEXT)
        return CASE_TEXT

    async def reviewer(_task, generated, callback=None):
        review_calls.append(generated)
        feedback = 'Review is complete.'
        await callback(feedback)
        return feedback

    async def revision(_task, generated, feedback, callback=None):
        await callback(REVISED_CASE_TEXT)
        return REVISED_CASE_TEXT

    monkeypatch.setattr(AIModelService, 'generate_test_cases_stream', writer)
    monkeypatch.setattr(AIModelService, 'review_test_cases_stream', reviewer)
    monkeypatch.setattr(AIModelService, 'revise_test_cases_based_on_review', revision)

    result = run_worker(task)

    task.refresh_from_db()
    assert result['status'] == 'completed'
    assert len(review_calls) == int(auto_review_at_creation)
    if auto_review_at_creation:
        assert task.review_feedback == 'Review is complete.'
        assert task.final_test_cases == REVISED_CASE_TEXT
    else:
        assert task.review_feedback == ''
        assert task.final_test_cases == CASE_TEXT


def test_worker_persists_progress_buffers_and_stage_transitions(monkeypatch):
    relations = setup_generation('generation-progress', auto_review=True)
    user = relations[0]
    task = make_task(user, relations)
    observed_states = []

    async def state():
        return await sync_to_async(
            lambda: GenerationTask.objects.values_list('status', 'progress').get(pk=task.pk),
            thread_sensitive=True,
        )()

    async def writer(worker_task, callback=None):
        observed_states.append(await state())
        await callback(CASE_TEXT)
        return CASE_TEXT

    async def reviewer(worker_task, generated, callback=None):
        observed_states.append(await state())
        feedback = 'Review is complete.'
        await callback(feedback)
        return feedback

    async def revision(worker_task, generated, feedback, callback=None):
        observed_states.append(await state())
        revised = REVISED_CASE_TEXT
        await callback(revised)
        return revised

    monkeypatch.setattr(AIModelService, 'generate_test_cases_stream', writer)
    monkeypatch.setattr(AIModelService, 'review_test_cases_stream', reviewer)
    monkeypatch.setattr(AIModelService, 'revise_test_cases_based_on_review', revision)

    result = run_worker(task)

    task.refresh_from_db()
    assert result['status'] == 'completed'
    assert observed_states == [('generating', 30), ('reviewing', 70), ('revising', 85)]
    assert task.status == 'completed'
    assert task.progress == 100
    assert task.stream_buffer == CASE_TEXT
    assert task.stream_position == len(CASE_TEXT)
    assert task.review_feedback == 'Review is complete.'
    assert task.final_test_cases == REVISED_CASE_TEXT


def test_writer_provider_failure_retries_once_and_resets_partial_stream(monkeypatch):
    relations = setup_generation('generation-retry', auto_review=False)
    user = relations[0]
    task = make_task(user, relations)
    attempts = 0

    async def writer(worker_task, callback=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await callback('partial output')
            raise RuntimeError('temporary provider failure')
        await callback(CASE_TEXT)
        return CASE_TEXT

    monkeypatch.setattr(AIModelService, 'generate_test_cases_stream', writer)
    monkeypatch.setattr('apps.requirement_analysis.generation_worker.PROVIDER_RETRY_BACKOFF_SECONDS', 0)
    monkeypatch.setattr('apps.requirement_analysis.generation_worker.MAX_PROVIDER_RETRIES', 1)

    result = run_worker(task)

    task.refresh_from_db()
    assert result['status'] == 'completed'
    assert attempts == MAX_PROVIDER_RETRIES + 1
    assert task.stream_buffer == CASE_TEXT
    assert task.stream_position == len(CASE_TEXT)


@pytest.mark.parametrize('timeout_stage', ['writer', 'reviewer', 'revision'])
def test_stage_timeouts_are_bounded_and_have_explicit_fallbacks(monkeypatch, timeout_stage):
    relations = setup_generation(
        f'generation-timeout-{timeout_stage}',
        auto_review=timeout_stage != 'writer',
        review_timeout=1,
    )
    user = relations[0]
    task = make_task(user, relations, output_mode='complete')
    monkeypatch.setattr('apps.requirement_analysis.generation_worker.WRITER_STAGE_TIMEOUT_SECONDS', 0.01)
    monkeypatch.setattr('apps.requirement_analysis.generation_worker.MAX_REVIEW_STAGE_TIMEOUT_SECONDS', 0.01)
    monkeypatch.setattr('apps.requirement_analysis.generation_worker.MAX_PROVIDER_RETRIES', 0)

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.05)
        return CASE_TEXT

    async def generated(*args, **kwargs):
        return CASE_TEXT

    async def reviewed(*args, **kwargs):
        return 'Review feedback.'

    if timeout_stage == 'writer':
        monkeypatch.setattr(AIModelService, 'generate_test_cases', slow)
    else:
        monkeypatch.setattr(AIModelService, 'generate_test_cases', generated)
        if timeout_stage == 'reviewer':
            monkeypatch.setattr(AIModelService, 'review_test_cases', slow)
        else:
            monkeypatch.setattr(AIModelService, 'review_test_cases', reviewed)
            monkeypatch.setattr(AIModelService, 'revise_test_cases_based_on_review', slow)

    result = run_worker(task)

    task.refresh_from_db()
    if timeout_stage == 'writer':
        assert result == {'task_id': task.pk, 'status': 'failed', 'stage': 'writer'}
        assert task.status == 'failed'
        assert 'writer stage timed out' in task.error_message
    elif timeout_stage == 'reviewer':
        assert result['status'] == 'completed'
        assert task.status == 'completed'
        assert 'reviewer stage timed out' in task.review_feedback
        assert task.final_test_cases
    else:
        assert result['status'] == 'completed'
        assert task.status == 'completed'
        assert 'revision stage timed out' in task.review_feedback
        assert task.final_test_cases


def test_cancellation_during_provider_call_fences_all_later_writes(monkeypatch):
    relations = setup_generation('generation-cancel', auto_review=False)
    user = relations[0]
    task = make_task(user, relations)

    async def cancel_then_return(worker_task, callback=None):
        await sync_to_async(
            lambda: GenerationTask.objects.filter(pk=task.pk).update(status='cancelled'),
            thread_sensitive=True,
        )()
        return CASE_TEXT

    monkeypatch.setattr(AIModelService, 'generate_test_cases_stream', cancel_then_return)

    result = run_worker(task)

    task.refresh_from_db()
    assert result['status'] == 'superseded'
    assert task.status == 'cancelled'
    assert task.generated_test_cases == ''
    assert task.progress == 30


def test_same_task_redelivery_restarts_work_and_fences_the_lost_worker(monkeypatch):
    relations = setup_generation('generation-redelivery', auto_review=False)
    user = relations[0]
    task = make_task(user, relations, output_mode='complete')
    provider_started = threading.Event()
    release_lost_worker = threading.Event()
    lost_worker_result = []

    async def writer(worker_task):
        if threading.current_thread().name == 'lost-generation-worker':
            provider_started.set()
            while not release_lost_worker.is_set():
                await asyncio.sleep(0.01)
            return CASE_TEXT.replace('Generated case', 'Stale worker output')
        return CASE_TEXT.replace('Generated case', 'Redelivered worker output')

    monkeypatch.setattr(AIModelService, 'generate_test_cases', writer)
    lost_worker = threading.Thread(
        target=lambda: lost_worker_result.append(run_worker(task)),
        name='lost-generation-worker',
    )
    lost_worker.start()
    try:
        assert provider_started.wait(timeout=2)
        task.refresh_from_db()
        assert task.status == 'generating'
        assert task.worker_claim_id

        redelivery_result = run_worker(task)
        assert redelivery_result == {'task_id': task.pk, 'status': 'completed'}
    finally:
        release_lost_worker.set()
        lost_worker.join(timeout=5)

    assert not lost_worker.is_alive()
    assert lost_worker_result == [{'task_id': task.pk, 'status': 'superseded'}]
    task.refresh_from_db()
    assert task.status == 'completed'
    assert task.final_test_cases == CASE_TEXT.replace('Generated case', 'Redelivered worker output')
    assert task.worker_claim_id == ''


def test_stale_worker_recovery_requeues_with_a_new_fencing_id(monkeypatch, settings):
    relations = setup_generation('generation-recovery', auto_review=False)
    user = relations[0]
    task = make_task(user, relations, status='generating', claim_id='dead-worker-claim')
    GenerationTask.objects.filter(pk=task.pk).update(
        updated_at=timezone.now() - timedelta(seconds=MIN_GENERATION_STALE_AFTER_SECONDS + 60),
        stream_buffer='partial output',
        stream_position=14,
        generated_test_cases='partial output',
    )
    settings.REQUIREMENT_GENERATION_STALE_AFTER_SECONDS = 60
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    recovery = recover_stale_generation_tasks.run()

    task.refresh_from_db()
    assert recovery == {'requeued': 1, 'failed': 0}
    assert task.status == 'pending'
    assert task.progress == 0
    assert task.stream_buffer == ''
    assert task.generated_test_cases == ''
    assert task.celery_task_id != 'dead-worker-claim'
    dispatch.assert_called_once_with(
        args=(task.pk, task.celery_task_id),
        task_id=task.celery_task_id,
        queue='celery',
        retry=False,
    )
    assert generate_test_case_task.run(task.pk, 'dead-worker-claim')['status'] == 'not_claimed'


def test_recovery_floor_does_not_reclaim_a_live_provider_deadline(monkeypatch, settings):
    relations = setup_generation('generation-recovery-floor', auto_review=False)
    user = relations[0]
    task = make_task(user, relations, status='generating', claim_id='slow-worker-claim')
    GenerationTask.objects.filter(pk=task.pk).update(updated_at=timezone.now() - timedelta(minutes=2))
    settings.REQUIREMENT_GENERATION_STALE_AFTER_SECONDS = 60
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    recovery = recover_stale_generation_tasks.run()

    task.refresh_from_db()
    assert recovery == {'requeued': 0, 'failed': 0}
    assert task.status == 'generating'
    dispatch.assert_not_called()


def test_documented_scheduler_invokes_generation_recovery(monkeypatch):
    command = ScheduledTasksCommand()
    recovery = Mock(return_value=0)
    monkeypatch.setattr(command, 'consume_retry_outbox', lambda: 0)
    monkeypatch.setattr(command, 'recover_stale_generation_tasks', recovery)
    monkeypatch.setattr(command, 'reconcile_execution_claims', lambda: 0)
    monkeypatch.setattr(command, 'dispatch_pending_notifications', lambda: 0)
    monkeypatch.setattr(command, 'reconcile_performance_callbacks', lambda: 0)
    monkeypatch.setattr(command, 'schedule_api_tasks', lambda: 0)
    monkeypatch.setattr(command, 'schedule_ui_tasks', lambda: 0)
    monkeypatch.setattr(command, 'schedule_app_tasks', lambda: 0)
    monkeypatch.setattr(command, 'schedule_perf_tasks', lambda: 0)

    command.handle(interval=1, lease_seconds=30, once=True)

    recovery.assert_called_once()
