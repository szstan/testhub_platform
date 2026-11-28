from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.executions.models import TestCaseRevision
from apps.projects.models import Project
from apps.testcases.models import TestCase, TestCaseSyncProfile
from apps.users.models import User
from apps.versions.models import Version


def owner_client():
    user = User.objects.create_user(username='asset-atomic-owner')
    project = Project.objects.create(name='Atomic project', owner=user)
    client = APIClient()
    client.force_authenticate(user)
    return user, project, client


@pytest.mark.django_db
def test_case_move_validates_retained_versions_or_accepts_explicit_removal():
    owner, project, client = owner_client()
    target = Project.objects.create(name='Other writable project', owner=owner)
    version = Version.objects.create(name='Source-only release', created_by=owner)
    version.projects.add(project)
    case = TestCase.objects.create(project=project, author=owner, title='Version-linked case')
    case.versions.add(version)
    url = f'/api/testcases/{case.pk}/'
    denied = client.patch(url, {'project_id': target.pk}, format='json')
    assert denied.status_code == 400, denied.data
    case.refresh_from_db()
    assert case.project_id == project.pk
    assert list(case.versions.values_list('pk', flat=True)) == [version.pk]
    assert not TestCaseRevision.objects.filter(test_case=case).exists()
    moved = client.patch(url, {'project_id': target.pk, 'version_ids': []}, format='json')
    assert moved.status_code == 200, moved.data
    case.refresh_from_db()
    assert case.project_id == target.pk
    assert not case.versions.exists()
    assert TestCaseRevision.objects.get(test_case=case).snapshot['version_ids'] == []


@pytest.mark.django_db(transaction=True)
def test_sync_publish_is_deferred_until_commit_and_never_published_after_rollback():
    owner, project, client = owner_client()
    profile = TestCaseSyncProfile.objects.create(
        project=project, name='Atomic repository', provider='github',
        repository_url='https://github.com/example/cases', created_by=owner,
    )
    url = f'/api/testcases/sync-profiles/{profile.pk}/sync/'
    with patch('apps.testcases.views.sync_testcase_profile.apply_async') as publish:
        with transaction.atomic():
            response = client.post(url)
            assert response.status_code == 202
            publish.assert_not_called()
        publish.assert_called_once()
        profile.refresh_from_db()
        old_task_id = profile.sync_task_id
        TestCaseSyncProfile.objects.filter(pk=profile.pk).update(sync_status='completed')
        publish.reset_mock()
        with pytest.raises(ValueError, match='rollback'):
            with transaction.atomic():
                assert client.post(url).status_code == 202
                publish.assert_not_called()
                raise ValueError('rollback')
        publish.assert_not_called()
        profile.refresh_from_db()
        assert profile.sync_task_id == old_task_id
        assert profile.sync_status == 'completed'


@pytest.mark.django_db(transaction=True)
def test_sync_deduplicates_live_job_and_reclaims_stale_job_with_new_token():
    owner, project, client = owner_client()
    profile = TestCaseSyncProfile.objects.create(
        project=project, name='Stale repository', provider='github',
        repository_url='https://github.com/example/cases', created_by=owner,
        sync_status='running', sync_task_id='old-task',
    )
    url = f'/api/testcases/sync-profiles/{profile.pk}/sync/'
    with patch('apps.testcases.views.sync_testcase_profile.apply_async') as publish:
        response = client.post(url)
        assert response.status_code == 202
        assert response.data['task_id'] == 'old-task'
        publish.assert_not_called()
        TestCaseSyncProfile.objects.filter(pk=profile.pk).update(
            updated_at=timezone.now() - timedelta(minutes=10),
        )
        reclaimed = client.post(url)
        assert reclaimed.status_code == 202
        assert reclaimed.data['task_id'] != 'old-task'
        publish.assert_called_once()
        profile.refresh_from_db()
        assert profile.sync_task_id == reclaimed.data['task_id']
        assert profile.sync_status == 'queued'


@pytest.mark.django_db(transaction=True)
def test_revision_sequence_serializes_concurrent_parent_updates():
    if not connection.features.has_select_for_update:
        pytest.skip('Concurrent revision row-lock verification requires MySQL or PostgreSQL')
    owner, project, client = owner_client()
    created = client.post('/api/testcases/', {
        'project_id': project.pk, 'title': 'Initial revision', 'expected_result': 'ok',
    }, format='json')
    assert created.status_code == 201
    case_id = created.data['id']
    barrier = Barrier(2)

    def update(title):
        close_old_connections()
        try:
            thread_client = APIClient()
            thread_client.force_authenticate(owner)
            barrier.wait(timeout=10)
            return thread_client.patch(f'/api/testcases/{case_id}/', {'title': title}, format='json').status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(update, ['Concurrent A', 'Concurrent B']))
    assert statuses == [200, 200]
    revisions = list(TestCaseRevision.objects.filter(test_case_id=case_id).order_by('revision'))
    assert [revision.revision for revision in revisions] == [1, 2, 3]
    assert {revision.snapshot['title'] for revision in revisions[1:]} == {'Concurrent A', 'Concurrent B'}
