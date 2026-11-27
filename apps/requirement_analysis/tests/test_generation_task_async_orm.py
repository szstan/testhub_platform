from unittest.mock import Mock

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.projects.models import Project
from apps.requirement_analysis.models import (
    AIModelConfig,
    AIModelService,
    PromptConfig,
    TestCaseGenerationTask as GenerationTask,
)
from apps.requirement_analysis.serializers import TestCaseGenerationTaskSerializer as GenerationTaskSerializer
from apps.requirement_analysis.views import TestCaseGenerationTaskViewSet as GenerationTaskViewSet
from apps.requirement_analysis.tasks import generate_test_case_task
from apps.users.models import User


pytestmark = pytest.mark.django_db(transaction=True)

CASE_OUTPUT = (
    '| 用例编号 | 测试场景 | 前置条件 | 操作步骤 | 预期结果 | 优先级 |\n'
    '| --- | --- | --- | --- | --- | --- |\n'
    '| TC-001 | Async ORM case | A user is registered | Submit valid credentials | Login succeeds | P1 |\n'
)


@pytest.mark.parametrize('output_mode', ['stream', 'complete'])
def test_generation_worker_eager_loads_async_config_relations(monkeypatch, output_mode):
    user = User.objects.create_user(
        username=f'generation-async-{output_mode}',
        password='StrongPass123',
    )
    project = Project.objects.create(name=f'Async ORM {output_mode}', owner=user)
    writer_model = AIModelConfig.objects.create(
        name='Writer test config',
        model_type='other',
        role='writer',
        api_key='',
        base_url='https://example.invalid/v1',
        model_name='writer-test',
        created_by=user,
    )
    reviewer_model = AIModelConfig.objects.create(
        name='Reviewer test config',
        model_type='other',
        role='reviewer',
        api_key='',
        base_url='https://example.invalid/v1',
        model_name='reviewer-test',
        created_by=user,
    )
    PromptConfig.objects.create(
        name='Writer prompt',
        prompt_type='writer',
        content='writer prompt for regression test',
        created_by=user,
    )
    PromptConfig.objects.create(
        name='Reviewer prompt',
        prompt_type='reviewer',
        content='reviewer prompt for regression test',
        created_by=user,
    )

    provider_stream_roles = []
    provider_request_roles = []

    async def fake_provider_stream(config, messages, callback=None, max_tokens=None):
        provider_stream_roles.append(config.role)
        yield CASE_OUTPUT

    async def fake_provider_request(config, messages, max_tokens=None):
        provider_request_roles.append(config.role)
        return {'choices': [{'message': {'content': CASE_OUTPUT}}]}

    monkeypatch.setattr(
        AIModelService,
        'call_openai_compatible_api_stream',
        staticmethod(fake_provider_stream),
    )
    monkeypatch.setattr(
        AIModelService,
        'call_openai_compatible_api',
        staticmethod(fake_provider_request),
    )

    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)

    if output_mode == 'complete':
        original_serializer_save = GenerationTaskSerializer.save

        def save_complete_task(serializer, *args, **kwargs):
            task = original_serializer_save(serializer, *args, **kwargs)
            task.output_mode = 'complete'
            task.save(update_fields=['output_mode'])
            return task

        monkeypatch.setattr(GenerationTaskSerializer, 'save', save_complete_task)

    request = APIRequestFactory().post(
        '/api/requirement-analysis/testcase-generation/generate/',
        {
            'title': f'Async ORM {output_mode}',
            'requirement_text': 'A basic requirement for the async ORM regression.',
            'project': project.pk,
            'output_mode': output_mode,
        },
        format='json',
    )
    force_authenticate(request, user=user)
    response = GenerationTaskViewSet.as_view({'post': 'generate'})(request)

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
    result = generate_test_case_task.run(task.pk, task.celery_task_id)
    assert result['status'] == 'completed'
    task.refresh_from_db()
    assert task.status == 'completed'
    assert task.progress == 100
    assert task.output_mode == output_mode

    if output_mode == 'stream':
        assert provider_stream_roles == ['writer', 'reviewer', 'writer']
        assert provider_request_roles == []
    else:
        assert provider_request_roles == ['writer', 'reviewer']
        assert provider_stream_roles == ['writer']

    assert writer_model.pk == task.writer_model_config_id
    assert reviewer_model.pk == task.reviewer_model_config_id
