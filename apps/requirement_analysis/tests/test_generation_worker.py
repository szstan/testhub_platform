import asyncio
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


CASE_TEXT = 'TC-001: Generated case\n'


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
    _, _, writer_model, reviewer_model, writer_prompt, reviewer_prompt, _ = relations
    return GenerationTask.objects.create(
        task_id=f'TASK_{uuid.uuid4().hex[:10].upper()}',
        title='Worker test task',
        requirement_text='A mocked requirement.',
        status=status,
        output_mode=output_mode,
        writer_model_config=writer_model,
        reviewer_model_config=reviewer_model,
        writer_prompt_config=writer_prompt,
        reviewer_prompt_config=reviewer_prompt,
        created_by=user,
        celery_task_id=claim_id,
    )


def post_generate(user, project, *, output_mode='stream'):
    request = APIRequestFactory().post(
        '/api/requirement-analysis/testcase-generation/generate/',
        {
            'title': 'Queued task',
            'requirement_text': 'A mocked requirement.',
            'project': project.pk,
            'output_mode': output_mode,
        },
        format='json',
    )
    force_authenticate(request, user=user)
    return GenerationTaskViewSet.as_view({'post': 'generate'})(request)


def run_worker(task):
    return generate_test_case_task.run(task.pk, task.celery_task_id)


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
        revised = 'TC-001: Revised case\n'
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
    assert task.final_test_cases == 'TC-001: Revised case\n'


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
            return 'TC-001: stale worker output\n'
        return 'TC-001: redelivered worker output\n'

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
    assert task.final_test_cases == 'TC-001: redelivered worker output\n'
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
