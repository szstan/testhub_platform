import pytest
from rest_framework.test import APIClient

from apps.requirement_analysis.models import AIModelConfig
from apps.requirement_analysis.tests.test_generation_worker import make_task, setup_generation
from apps.users.models import User


pytestmark = pytest.mark.django_db


CONFIG_FIELDS = (
    'writer_model_config',
    'reviewer_model_config',
    'writer_prompt_config',
    'reviewer_prompt_config',
)


def generation_task_url(task):
    return f'/api/requirement-analysis/testcase-generation/{task.task_id}/'


def test_task_patch_rejects_private_configuration_references_and_preserves_all_fks():
    owner_relations = setup_generation('generation-config-patch-owner', auto_review=True)
    owner, *_ = owner_relations
    task = make_task(owner, owner_relations, output_mode='complete')
    other_relations = setup_generation('generation-config-patch-private', auto_review=True)
    private_targets = {
        'writer_model_config': other_relations[2].pk,
        'reviewer_model_config': other_relations[3].pk,
        'writer_prompt_config': other_relations[4].pk,
        'reviewer_prompt_config': other_relations[5].pk,
    }
    original = {
        field: getattr(task, f'{field}_id')
        for field in CONFIG_FIELDS
    }
    client = APIClient()
    client.force_authenticate(user=owner)

    for field, target_id in private_targets.items():
        response = client.patch(generation_task_url(task), {field: target_id}, format='json')
        assert response.status_code == 400
        task.refresh_from_db()
        assert {name: getattr(task, f'{name}_id') for name in CONFIG_FIELDS} == original


def test_task_patch_rejects_administrator_disabled_models_and_preserves_references():
    owner_relations = setup_generation('generation-config-patch-disabled-owner', auto_review=True)
    owner, _, *_ = owner_relations
    task = make_task(owner, owner_relations, output_mode='complete')
    admin = User.objects.create_superuser(
        username='generation-config-patch-disabled-admin',
        email='generation-config-patch-disabled-admin@example.test',
        password='StrongPass123',
    )
    original_writer_id = task.writer_model_config_id
    original_reviewer_id = task.reviewer_model_config_id
    client = APIClient()
    client.force_authenticate(user=owner)

    for role, field in [('writer', 'writer_model_config'), ('reviewer', 'reviewer_model_config')]:
        disabled = AIModelConfig.objects.create(
            name=f'Disabled {role}',
            model_type='other',
            role=role,
            api_key='',
            base_url='https://example.invalid/v1',
            model_name=f'disabled-{role}',
            is_active=False,
            created_by=admin,
        )
        response = client.patch(generation_task_url(task), {field: disabled.pk}, format='json')
        assert response.status_code == 400
        task.refresh_from_db()
        assert task.writer_model_config_id == original_writer_id
        assert task.reviewer_model_config_id == original_reviewer_id
