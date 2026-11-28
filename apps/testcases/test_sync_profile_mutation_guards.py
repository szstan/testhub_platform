from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.testcases.models import TestCase, TestCaseSourceBinding, TestCaseSyncProfile
from apps.testcases.views import TestCaseSyncProfileDetailView
from apps.users.models import User


pytestmark = pytest.mark.django_db


@pytest.fixture
def guarded_profile():
    owner = User.objects.create_user(username='sync-delete-guard-owner')
    project = Project.objects.create(name='Guarded sync project', owner=owner)
    profile = TestCaseSyncProfile.objects.create(
        project=project, name='Guarded source', provider='github',
        repository_url='https://github.com/example/cases', created_by=owner,
        sync_result={'created': 1},
    )
    case = TestCase.objects.create(project=project, author=owner, title='Previously imported')
    binding = TestCaseSourceBinding.objects.create(
        testcase=case, profile=profile, external_ref=case.external_ref, source_path='case.yaml',
    )
    client = APIClient()
    client.force_authenticate(owner)
    return profile, case, binding, client


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_active_sync_profile_cannot_be_deleted(guarded_profile, state):
    profile, case, binding, client = guarded_profile
    profile.sync_status = state
    profile.save(update_fields=['sync_status'])
    response = client.delete(f'/api/testcases/sync-profiles/{profile.pk}/')
    assert response.status_code == 400, response.data
    assert TestCaseSyncProfile.objects.filter(pk=profile.pk).exists()
    assert TestCaseSourceBinding.objects.filter(pk=binding.pk).exists()
    assert TestCase.objects.filter(pk=case.pk).exists()
    profile.refresh_from_db()
    assert profile.sync_result == {'created': 1}


@pytest.mark.parametrize('operation', ['update', 'delete'])
def test_mutation_rechecks_worker_state_under_current_profile_lock(guarded_profile, operation):
    profile, _case, binding, client = guarded_profile
    stale = TestCaseSyncProfile.objects.get(pk=profile.pk)
    TestCaseSyncProfile.objects.filter(pk=profile.pk).update(sync_status='running', sync_task_id='live-worker')
    with patch.object(TestCaseSyncProfileDetailView, 'get_object', return_value=stale):
        if operation == 'update':
            response = client.patch(f'/api/testcases/sync-profiles/{profile.pk}/', {'branch': 'wrong-branch'}, format='json')
        else:
            response = client.delete(f'/api/testcases/sync-profiles/{profile.pk}/')
    assert response.status_code == 400, response.data
    profile.refresh_from_db()
    assert profile.sync_status == 'running'
    assert profile.sync_task_id == 'live-worker'
    assert profile.branch != 'wrong-branch'
    assert TestCaseSourceBinding.objects.filter(pk=binding.pk).exists()


def test_finished_profile_can_be_deleted_without_deleting_imported_cases(guarded_profile):
    profile, case, binding, client = guarded_profile
    profile.sync_status = 'completed'
    profile.save(update_fields=['sync_status'])
    response = client.delete(f'/api/testcases/sync-profiles/{profile.pk}/')
    assert response.status_code == 204
    assert not TestCaseSyncProfile.objects.filter(pk=profile.pk).exists()
    assert not TestCaseSourceBinding.objects.filter(pk=binding.pk).exists()
    assert TestCase.objects.filter(pk=case.pk).exists()
