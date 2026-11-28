from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APIClient

from apps.projects.models import Project, ProjectMember
from apps.testcases.models import TestCase, TestCaseImportRecord, TestCaseSyncProfile
from apps.testsuites.models import TestSuite, TestSuiteCase
from apps.users.models import User
from apps.versions.models import Version


pytestmark = pytest.mark.django_db


def test_viewer_reads_cases_but_cannot_create_update_delete_or_move_them():
    owner = User.objects.create_user(username='case-access-owner')
    viewer = User.objects.create_user(username='case-access-viewer', phone='5555555555')
    writer = User.objects.create_user(username='case-access-writer')
    project = Project.objects.create(name='Case access', owner=owner)
    target_owner = User.objects.create_user(username='case-target-owner')
    target = Project.objects.create(name='Target', owner=target_owner)
    ProjectMember.objects.create(project=project, user=viewer, role='viewer')
    ProjectMember.objects.create(project=project, user=writer, role='tester')
    ProjectMember.objects.create(project=target, user=writer, role='viewer')
    case = TestCase.objects.create(
        project=project, title='Private phone', expected_result='ok', author=owner,
    )
    foreign_version = Version.objects.create(name='Foreign version', created_by=owner)
    foreign_version.projects.add(target)

    viewer_client = APIClient()
    viewer_client.force_authenticate(viewer)
    assert viewer_client.get(f'/api/testcases/{case.pk}/').status_code == 200
    detail = viewer_client.get(f'/api/testcases/{case.pk}/')
    assert 'phone' not in detail.data['author']
    assert 'is_staff' not in detail.data['author']
    assert viewer_client.patch(
        f'/api/testcases/{case.pk}/', {'title': 'Viewer edit'}, format='json',
    ).status_code == 403
    assert viewer_client.delete(f'/api/testcases/{case.pk}/').status_code == 403
    denied_create = viewer_client.post('/api/testcases/', {
        'project_id': project.pk, 'title': 'Viewer create', 'expected_result': 'ok',
    }, format='json')
    assert denied_create.status_code == 403

    writer_client = APIClient()
    writer_client.force_authenticate(writer)
    denied_move = writer_client.patch(
        f'/api/testcases/{case.pk}/', {'project_id': target.pk}, format='json',
    )
    assert denied_move.status_code == 403
    case.refresh_from_db()
    assert case.project_id == project.pk

    denied_version = writer_client.post('/api/testcases/', {
        'project_id': project.pk, 'version_ids': [foreign_version.pk],
        'title': 'Foreign version reference', 'expected_result': 'ok',
    }, format='json')
    assert denied_version.status_code == status.HTTP_400_BAD_REQUEST
    assert not TestCase.objects.filter(title='Foreign version reference').exists()

    created = writer_client.post('/api/testcases/', {
        'project_id': project.pk, 'title': 'Writer create', 'expected_result': 'ok',
    }, format='json')
    assert created.status_code == status.HTTP_201_CREATED, created.data
    updated = writer_client.patch(
        f'/api/testcases/{case.pk}/', {'title': 'Writer update'}, format='json',
    )
    assert updated.status_code == status.HTTP_200_OK
    assert writer_client.delete(f'/api/testcases/{case.pk}/').status_code == 204


def test_import_and_sync_profile_mutations_require_project_write_access(django_capture_on_commit_callbacks):
    owner = User.objects.create_user(username='job-access-owner')
    viewer = User.objects.create_user(username='job-access-viewer')
    writer = User.objects.create_user(username='job-access-writer')
    project = Project.objects.create(name='Job access', owner=owner)
    ProjectMember.objects.create(project=project, user=viewer, role='viewer')
    ProjectMember.objects.create(project=project, user=writer, role='tester')
    profile = TestCaseSyncProfile.objects.create(
        project=project, name='repo', provider='github',
        repository_url='https://github.com/example/cases', created_by=owner,
    )

    viewer_client = APIClient()
    viewer_client.force_authenticate(viewer)
    upload = SimpleUploadedFile('cases.xlsx', b'fake spreadsheet', content_type='application/octet-stream')
    denied_import = viewer_client.post('/api/testcases/import-records/', {
        'project_id': project.pk, 'file': upload,
    }, format='multipart')
    assert denied_import.status_code == status.HTTP_403_FORBIDDEN
    assert not TestCaseImportRecord.objects.exists()
    assert viewer_client.patch(
        f'/api/testcases/sync-profiles/{profile.pk}/', {'branch': 'viewer'}, format='json',
    ).status_code == 403
    assert viewer_client.delete(f'/api/testcases/sync-profiles/{profile.pk}/').status_code == 403
    assert viewer_client.post(f'/api/testcases/sync-profiles/{profile.pk}/sync/').status_code == 403

    writer_client = APIClient()
    writer_client.force_authenticate(writer)
    import_file = SimpleUploadedFile('cases.xlsx', b'fake spreadsheet', content_type='application/octet-stream')
    with patch(
        'apps.testcases.views.import_testcases_from_excel.delay',
        return_value=SimpleNamespace(id='import-job'),
    ):
        allowed_import = writer_client.post('/api/testcases/import-records/', {
            'project_id': project.pk, 'file': import_file,
        }, format='multipart')
    assert allowed_import.status_code == status.HTTP_201_CREATED, allowed_import.data

    allowed_patch = writer_client.patch(
        f'/api/testcases/sync-profiles/{profile.pk}/', {'branch': 'writer'}, format='json',
    )
    assert allowed_patch.status_code == 200
    with patch('apps.testcases.views.sync_testcase_profile.apply_async') as enqueue:
        with django_capture_on_commit_callbacks(execute=True):
            queued = writer_client.post(f'/api/testcases/sync-profiles/{profile.pk}/sync/')
    assert queued.status_code == status.HTTP_202_ACCEPTED
    assert queued.data['status'] == 'queued'
    enqueue.assert_called_once()
    profile.refresh_from_db()
    assert profile.sync_status == 'queued'
    assert profile.sync_task_id == queued.data['task_id']
    polled = writer_client.get(f'/api/testcases/sync-profiles/{profile.pk}/')
    assert polled.status_code == 200
    assert polled.data['sync_status'] == 'queued'
    assert polled.data['sync_task_id'] == queued.data['task_id']


@pytest.mark.django_db
def test_case_cannot_move_projects_while_suite_references_it():
    owner = User.objects.create_user(username='case-move-owner')
    writer = User.objects.create_user(username='case-move-writer')
    project = Project.objects.create(name='Case move source', owner=owner)
    target = Project.objects.create(name='Case move target', owner=owner)
    ProjectMember.objects.create(project=project, user=writer, role='tester')
    ProjectMember.objects.create(project=target, user=writer, role='tester')
    testcase = TestCase.objects.create(
        project=project, title='Suite-referenced case', expected_result='ok', author=owner,
    )
    suite = TestSuite.objects.create(project=project, name='Source suite', author=owner)
    TestSuiteCase.objects.create(testsuite=suite, testcase=testcase, order=0)
    client = APIClient()
    client.force_authenticate(writer)

    response = client.patch(
        f'/api/testcases/{testcase.pk}/', {'project_id': target.pk}, format='json',
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    testcase.refresh_from_db()
    assert testcase.project_id == project.pk
    assert suite.testsuitecase_set.get().testcase_id == testcase.pk
