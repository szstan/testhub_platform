import pytest
from rest_framework.test import APIClient

from apps.executions.models import ExecutionDataset, ExecutionPluginManifest
from apps.projects.models import Project
from apps.testcases.models import TestCaseSyncProfile as SyncProfile
from apps.users.models import User


@pytest.mark.django_db
@pytest.mark.parametrize('resource', ['sync-profiles', 'datasets', 'plugins'])
def test_management_lists_respect_requested_page_size_and_stable_page_boundaries(resource):
    owner = User.objects.create_user(username='management-paging', password='pass')
    project = Project.objects.create(name='Management paging', owner=owner)
    if resource == 'sync-profiles':
        rows = SyncProfile.objects.bulk_create([
            SyncProfile(project=project, name=f'sync-{index}', provider='github', repository_url='https://example.invalid/repository')
            for index in range(31)
        ])
        endpoint = '/api/testcases/sync-profiles/'
    elif resource == 'datasets':
        rows = ExecutionDataset.objects.bulk_create([
            ExecutionDataset(project=project, name=f'dataset-{index}') for index in range(31)
        ])
        endpoint = '/api/executions/datasets/'
    else:
        rows = ExecutionPluginManifest.objects.bulk_create([
            ExecutionPluginManifest(project=project, name=f'plugin-{index}', version='1', kind='executor')
            for index in range(31)
        ])
        endpoint = '/api/executions/plugins/'
    client = APIClient()
    client.force_authenticate(user=owner)
    first = client.get(endpoint, {'page': 1, 'page_size': 30, 'ordering': 'id'})
    second = client.get(endpoint, {'page': 2, 'page_size': 30, 'ordering': 'id'})
    assert first.status_code == second.status_code == 200
    assert first.data['count'] == second.data['count'] == 31
    assert [row['id'] for row in first.data['results']] == [row.pk for row in rows[:30]]
    assert [row['id'] for row in second.data['results']] == [rows[-1].pk]
