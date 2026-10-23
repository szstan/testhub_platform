from unittest.mock import Mock, patch

import pytest

from apps.projects.models import Project
from apps.testcases.models import TestCase, TestCaseSourceBinding, TestCaseSyncProfile
from apps.testcases.sync import sync_profile
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
