import pytest
from rest_framework.test import APIClient

from apps.requirement_analysis.models import AIModelConfig, GenerationConfig, PromptConfig
from apps.users.models import User


pytestmark = pytest.mark.django_db


def create_model(user, role, name, *, active=True):
    return AIModelConfig.objects.create(
        name=name,
        model_type='other',
        role=role,
        api_key='',
        base_url='https://example.invalid/v1',
        model_name=f'mock-{name}',
        is_active=active,
        created_by=user,
    )


def create_prompt(user, prompt_type, name, *, active=True):
    return PromptConfig.objects.create(
        name=name,
        prompt_type=prompt_type,
        content='mock prompt',
        is_active=active,
        created_by=user,
    )


def check_config(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client.get('/api/requirement-analysis/config/check/')


def test_config_status_uses_active_review_policy_and_ignores_unneeded_disabled_configs():
    user = User.objects.create_user(username='config-status-off', password='StrongPass123')
    create_model(user, 'writer', 'visible-writer')
    create_model(user, 'writer', 'old-disabled-writer', active=False)
    create_prompt(user, 'writer', 'writer-prompt')
    create_prompt(user, 'writer', 'old-disabled-writer-prompt', active=False)
    create_model(user, 'reviewer', 'old-disabled-reviewer', active=False)
    create_prompt(user, 'reviewer', 'old-disabled-reviewer-prompt', active=False)
    GenerationConfig.objects.create(name='No automatic review', enable_auto_review=False, is_active=True)

    response = check_config(user)

    assert response.status_code == 200
    assert response.json()['overall_status'] == 'enabled'
    assert response.json()['reviewer_model']['required'] is False
    assert response.json()['reviewer_prompt']['required'] is False
    assert response.json()['generation_config']['enable_auto_review'] is False
    assert response.json()['writer_model']['name'] == 'visible-writer'
    assert response.json()['writer_model']['id'] is not None
    assert 'visible-writer' in response.content.decode('utf-8')


def test_config_status_does_not_treat_another_users_model_as_available():
    caller = User.objects.create_user(username='config-status-caller', password='StrongPass123')
    other_user = User.objects.create_user(username='config-status-other', password='StrongPass123')
    create_model(other_user, 'writer', 'private-writer-secret')
    create_prompt(caller, 'writer', 'writer-prompt')
    GenerationConfig.objects.create(name='No automatic review', enable_auto_review=False, is_active=True)

    response = check_config(caller)

    assert response.status_code == 200
    assert response.json()['overall_status'] == 'not_configured'
    assert response.json()['writer_model']['configured'] is False
    assert response.json()['writer_model']['enabled'] is False
    assert response.json()['writer_model']['name'] is None
    assert 'private-writer-secret' not in response.content.decode('utf-8')


def test_config_status_requires_reviewer_when_auto_review_is_enabled():
    user = User.objects.create_user(username='config-status-review', password='StrongPass123')
    create_model(user, 'writer', 'writer')
    create_prompt(user, 'writer', 'writer-prompt')
    GenerationConfig.objects.create(name='Automatic review', enable_auto_review=True, is_active=True)

    response = check_config(user)

    assert response.status_code == 200
    assert response.json()['overall_status'] == 'not_configured'
    assert response.json()['reviewer_model']['required'] is True
    assert response.json()['reviewer_prompt']['required'] is True


def test_missing_generation_config_uses_shared_auto_review_default():
    user = User.objects.create_user(username='config-status-default', password='StrongPass123')
    create_model(user, 'writer', 'writer')
    create_prompt(user, 'writer', 'writer-prompt')

    response = check_config(user)

    assert response.status_code == 200
    assert response.json()['overall_status'] == 'not_configured'
    assert response.json()['reviewer_model']['required'] is True
    assert response.json()['generation_config']['required'] is False
    assert response.json()['generation_config']['default_output_mode'] == 'stream'
    assert response.json()['generation_config']['enable_auto_review'] is True


def test_public_config_status_keeps_authentication_contract_without_revealing_model_names():
    admin = User.objects.create_superuser(
        username='config-status-admin', email='config-status-admin@example.test', password='StrongPass123'
    )
    create_model(admin, 'writer', 'publicly-selected-admin-writer')
    create_prompt(admin, 'writer', 'writer-prompt')
    GenerationConfig.objects.create(name='No automatic review', enable_auto_review=False, is_active=True)

    response = check_config()

    assert response.status_code == 200
    assert response.json()['overall_status'] == 'enabled'
    assert response.json()['writer_model']['enabled'] is True
    assert response.json()['writer_model']['name'] is None
    assert 'publicly-selected-admin-writer' not in response.content.decode('utf-8')
