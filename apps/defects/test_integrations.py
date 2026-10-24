from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIClient
from django.urls import reverse

from apps.defects.models import Defect, GitHubIssueIntegration
from apps.projects.models import Project
from apps.users.models import User


@pytest.mark.django_db
def test_push_defect_to_github_is_idempotent_and_redacts_token(monkeypatch):
    user = User.objects.create_user(username='github-defect-owner', password='pass')
    project = Project.objects.create(name='GitHub Defect Project', owner=user)
    defect = Defect.objects.create(
        project=project,
        title='Checkout fails',
        description='Card flow fails',
        expected_result='success',
        actual_result='500',
        reporter=user,
    )
    GitHubIssueIntegration.objects.create(
        project=project,
        repository_url='https://github.com/acme/shop',
        token_env_ref='TESTHUB_GITHUB_TOKEN',
        created_by=user,
    )
    monkeypatch.setenv('TESTHUB_GITHUB_TOKEN', 'secret-token')
    response = Mock(status_code=201)
    response.json.return_value = {'number': 42, 'html_url': 'https://github.com/acme/shop/issues/42'}

    client = APIClient()
    client.force_authenticate(user=user)
    endpoint = reverse('defects-push-github', kwargs={'pk': defect.id})
    with patch('apps.defects.integrations.validate_outbound_url', side_effect=lambda url, allow_private=False: url), patch('apps.defects.integrations.requests.post', return_value=response) as call:
        first = client.post(endpoint, {}, format='json')
    assert first.status_code == 201, first.data
    defect.refresh_from_db()
    assert defect.external_key == '42'
    assert call.call_args.kwargs['headers']['Authorization'] == 'Bearer secret-token'

    second = client.post(endpoint, {}, format='json')
    assert second.status_code == 200
    assert second.data['created'] is False
