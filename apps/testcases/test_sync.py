from unittest.mock import Mock, patch

import pytest
from django.test import override_settings

from apps.projects.models import Project
from apps.testcases.models import TestCase, TestCaseSourceBinding, TestCaseSyncProfile
from apps.testcases.sync import TestCaseSyncError, _gitlab_files, _token, sync_profile
from apps.users.models import User


@pytest.mark.django_db
def test_github_tests_as_code_sync_is_idempotent_and_records_source():
    user = User.objects.create_user(username='sync-owner', password='pass')
    project = Project.objects.create(name='Sync Project', owner=user)
    profile = TestCaseSyncProfile.objects.create(
        project=project,
        name='github-tests',
        provider='github',
        repository_url='https://github.com/acme/shop',
        branch='main',
        path='tests',
        created_by=user,
    )
    tree = Mock(status_code=200)
    tree.json.return_value = {
        'sha': 'commit-1',
        'tree': [{'type': 'blob', 'path': 'tests/login.yml', 'sha': 'blob-1'}],
    }
    raw = Mock(status_code=200, content=b'id: checkout.login\ntitle: Login\ntype: api\nexpected_result: ok\n')

    with patch('apps.testcases.sync.validate_outbound_url', side_effect=lambda url, allow_private=False: url), patch('apps.testcases.sync.requests.get', side_effect=[tree, raw]):
        summary = sync_profile(profile)

    assert summary.created == 1
    assert summary.updated == 0
    case = TestCase.objects.get(project=project, external_ref='checkout.login')
    binding = TestCaseSourceBinding.objects.get(testcase=case)
    assert binding.source_path == 'tests/login.yml'
    assert binding.source_commit == 'commit-1'

    tree_again = Mock(status_code=200)
    tree_again.json.return_value = tree.json.return_value
    raw_again = Mock(status_code=200, content=raw.content)
    with patch('apps.testcases.sync.validate_outbound_url', side_effect=lambda url, allow_private=False: url), patch('apps.testcases.sync.requests.get', side_effect=[tree_again, raw_again]):
        repeated = sync_profile(profile)

    assert repeated.created == 0
    assert repeated.updated == 1
    assert TestCase.objects.filter(project=project, external_ref='checkout.login').count() == 1


@pytest.mark.django_db
@override_settings(TESTCASE_SYNC_SECRET_ALIASES={'gitlab': 'TESTHUB_GITLAB_TOKEN'})
def test_token_ref_is_server_allowlisted_alias(monkeypatch):
    user = User.objects.create_user(username='sync-secret-owner', password='pass')
    project = Project.objects.create(name='Sync Secret Project', owner=user)
    profile = TestCaseSyncProfile.objects.create(
        project=project,
        name='gitlab-tests',
        provider='gitlab',
        repository_url='https://gitlab.com/acme/shop',
        token_env_ref='gitlab',
        created_by=user,
    )
    monkeypatch.setenv('TESTHUB_GITLAB_TOKEN', 'gitlab-secret')

    assert _token(profile) == 'gitlab-secret'
    profile.token_env_ref = 'DJANGO_SECRET_KEY'
    with pytest.raises(TestCaseSyncError, match='secret alias'):
        _token(profile)


@pytest.mark.django_db
@override_settings(TESTCASE_SYNC_PROVIDER_HOSTS={'gitlab': ['gitlab.com']})
def test_gitlab_api_base_must_follow_provider_host_policy():
    user = User.objects.create_user(username='sync-host-owner', password='pass')
    project = Project.objects.create(name='Sync Host Project', owner=user)
    profile = TestCaseSyncProfile.objects.create(
        project=project,
        name='gitlab-host-policy',
        provider='gitlab',
        repository_url='https://gitlab.com/acme/shop',
        api_base_url='https://attacker.example/api/v4',
        created_by=user,
    )
    with patch('apps.testcases.sync.validate_outbound_url', side_effect=lambda url, allow_private=False: url):
        with pytest.raises(TestCaseSyncError, match='主机不在服务端允许列表'):
            _gitlab_files(profile, 'gitlab-secret')


@pytest.mark.django_db
@override_settings(TESTCASE_SYNC_PROVIDER_HOSTS={'gitlab': ['gitlab.com']})
def test_gitlab_sync_keeps_same_provider_api_base():
    user = User.objects.create_user(username='sync-gitlab-owner', password='pass')
    project = Project.objects.create(name='Sync GitLab Project', owner=user)
    profile = TestCaseSyncProfile.objects.create(
        project=project,
        name='gitlab-safe-host',
        provider='gitlab',
        repository_url='https://gitlab.com/acme/shop',
        api_base_url='https://gitlab.com/api/v4',
        created_by=user,
    )
    tree = Mock(status_code=200)
    tree.json.return_value = [{'type': 'blob', 'path': 'tests/login.yml', 'id': 'blob-1'}]
    with patch(
        'apps.testcases.sync.validate_outbound_url',
        side_effect=lambda url, allow_private=False: url,
    ), patch('apps.testcases.sync.requests.get', return_value=tree) as request:
        _, files = _gitlab_files(profile, 'gitlab-secret')

    assert files[0][2].startswith('https://gitlab.com/api/v4/')
    assert request.call_args.kwargs['headers']['PRIVATE-TOKEN'] == 'gitlab-secret'
