from datetime import timedelta
import hashlib

import pytest
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
