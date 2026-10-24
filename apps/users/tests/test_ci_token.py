from datetime import timedelta
import hashlib
import io

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.executions.models import ExecutionJob
from apps.projects.models import Project
from apps.users.models import CIToken, User


def _token(user, project, raw='th_ci_test_token', expires_at=None):
    return CIToken.objects.create(
        project=project,
        user=user,
        name='test-token',
        token_prefix=raw[:16],
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=expires_at or timezone.now() + timedelta(days=30),
    )


@pytest.mark.django_db
def test_ci_token_authenticates_and_scopes_execution_jobs():
    user = User.objects.create_user(username='ci-token-user', password='pass')
    project = Project.objects.create(name='CI Token Project', owner=user)
    raw = 'th_ci_scope_token'
    _token(user, project, raw)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {raw}')
    response = client.post('/api/executions/jobs/', {
        'project': project.id,
        'source_type': 'ci',
        'source_id': 'ci-run-1',
        'trigger': 'ci',
    }, format='json')
    assert response.status_code == 201, response.data
    assert ExecutionJob.objects.filter(project=project, source_id='ci-run-1').exists()


@pytest.mark.django_db
def test_expired_ci_token_is_rejected():
    user = User.objects.create_user(username='expired-ci-user', password='pass')
    project = Project.objects.create(name='Expired CI Project', owner=user)
    raw = 'th_ci_expired_token'
    _token(user, project, raw, timezone.now() - timedelta(minutes=1))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {raw}')
    response = client.get('/api/executions/jobs/')
    assert response.status_code in {401, 403}


@pytest.mark.django_db
def test_ci_token_rejects_another_project_and_updates_last_used():
    user = User.objects.create_user(username='scoped-ci-user', password='pass')
    project = Project.objects.create(name='Scoped CI Project', owner=user)
    other_project = Project.objects.create(name='Other CI Project', owner=user)
    raw = 'th_ci_project_scope_token'
    token = _token(user, project, raw)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {raw}')
    response = client.post('/api/executions/jobs/', {
        'project': other_project.id,
        'source_type': 'ci',
        'source_id': 'cross-project-run',
        'trigger': 'ci',
    }, format='json')
    assert response.status_code == 403
    client.get('/api/executions/jobs/')
    token.refresh_from_db()
    assert token.last_used_at is not None


@pytest.mark.django_db
def test_ci_token_cannot_access_non_ci_api():
    user = User.objects.create_user(username='restricted-ci-user', password='pass')
    project = Project.objects.create(name='Restricted CI Project', owner=user)
    raw = 'th_ci_restricted_token'
    _token(user, project, raw)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {raw}')
    response = client.get('/api/projects/')
    assert response.status_code in {401, 403}


@pytest.mark.django_db
def test_revoked_ci_token_is_rejected():
    user = User.objects.create_user(username='revoked-ci-user', password='pass')
    project = Project.objects.create(name='Revoked CI Project', owner=user)
    raw = 'th_ci_revoked_token'
    token = _token(user, project, raw)
    token.revoked_at = timezone.now()
    token.save(update_fields=['revoked_at'])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {raw}')
    response = client.get('/api/executions/jobs/')
    assert response.status_code in {401, 403}


@pytest.mark.django_db
def test_ci_token_commands_never_list_plaintext_or_hash():
    user = User.objects.create_user(username='command-ci-user', password='pass')
    project = Project.objects.create(name='Command CI Project', owner=user)
    raw = 'th_ci_command_token'
    token = _token(user, project, raw)
    output = io.StringIO()
    call_command(
        'list_ci_tokens', project_id=project.id, stdout=output
    )
    assert token.token_prefix in output.getvalue()
    assert raw not in output.getvalue()
    assert token.token_hash not in output.getvalue()

    call_command('revoke_ci_token', project_id=project.id, name='test-token', stdout=io.StringIO())
    token.refresh_from_db()
    assert token.revoked_at is not None


@pytest.mark.django_db
def test_create_ci_token_prints_secret_once_and_stores_only_hash():
    user = User.objects.create_user(username='create-command-user', password='pass')
    project = Project.objects.create(name='Create Command Project', owner=user)
    output = io.StringIO()
    call_command(
        'create_ci_token',
        project_id=project.id,
        username=user.username,
        name='github-pilot',
        expires_days=90,
        stdout=output,
    )
    raw = output.getvalue().strip()
    created = CIToken.objects.get(project=project, name='github-pilot')
    assert raw.startswith('th_ci_')
    assert created.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw != created.token_hash
