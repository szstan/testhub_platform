import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.projects.models import Project, ProjectMember
from apps.testcases.models import TestCase
from apps.users.models import User
from apps.versions.models import Version


pytestmark = pytest.mark.django_db


def test_shared_version_response_hides_projects_and_cases_outside_actor_scope():
    actor = User.objects.create_user(username='version-reader', phone='4444444444')
    other_owner = User.objects.create_user(username='version-other-owner')
    project = Project.objects.create(name='Visible project', owner=other_owner)
    other_project = Project.objects.create(name='Hidden project', owner=other_owner)
    ProjectMember.objects.create(project=project, user=actor, role='viewer')
    version = Version.objects.create(
        name='Shared release', description='release', created_by=other_owner,
    )
    version.projects.set([project, other_project])
    TestCase.objects.create(
        project=project, title='Visible case', expected_result='ok', author=other_owner,
    ).versions.add(version)
    TestCase.objects.create(
        project=other_project, title='Hidden case', expected_result='ok', author=other_owner,
    ).versions.add(version)

    client = APIClient()
    client.force_authenticate(actor)
    detail = client.get(f'/api/versions/{version.pk}/')
    assert detail.status_code == status.HTTP_200_OK
    assert detail.data['projects'] == [{'id': project.pk, 'name': project.name}]
    assert detail.data['testcases_count'] == 1
    assert 'phone' not in detail.data['created_by']
    assert 'is_staff' not in detail.data['created_by']

    project_versions = client.get(f'/api/versions/projects/{project.pk}/versions/')
    assert project_versions.status_code == status.HTTP_200_OK
    assert project_versions.data[0]['projects'] == [{'id': project.pk, 'name': project.name}]
    assert project_versions.data[0]['testcases_count'] == 1


def test_version_writes_require_writer_on_every_attached_project():
    owner = User.objects.create_user(username='version-writer-owner')
    actor = User.objects.create_user(username='version-writer')
    other_owner = User.objects.create_user(username='version-other')
    project = Project.objects.create(name='Writable', owner=owner)
    other_project = Project.objects.create(name='Not writable', owner=other_owner)
    ProjectMember.objects.create(project=project, user=actor, role='tester')
    shared = Version.objects.create(name='Shared', created_by=owner)
    shared.projects.set([project, other_project])
    local = Version.objects.create(name='Local', created_by=owner)
    local.projects.add(project)

    client = APIClient()
    client.force_authenticate(actor)

    forbidden_create = client.post('/api/versions/', {
        'name': 'Cross project', 'project_ids': [project.pk, other_project.pk],
    }, format='json')
    assert forbidden_create.status_code == status.HTTP_403_FORBIDDEN
    assert not Version.objects.filter(name='Cross project').exists()

    allowed_create = client.post('/api/versions/', {
        'name': 'Writer release', 'project_ids': [project.pk],
    }, format='json')
    assert allowed_create.status_code == status.HTTP_201_CREATED, allowed_create.data
    created = Version.objects.get(pk=allowed_create.data['id'])

    assert client.patch(f'/api/versions/{shared.pk}/', {'name': 'Changed'}, format='json').status_code == 403
    shared.refresh_from_db()
    assert shared.name == 'Shared'
    assert client.patch(f'/api/versions/{created.pk}/', {'name': 'Updated release'}, format='json').status_code == 200
    assert client.delete(f'/api/versions/{local.pk}/').status_code == 204


def test_viewer_cannot_create_versions():
    owner = User.objects.create_user(username='version-owner')
    viewer = User.objects.create_user(username='version-viewer')
    project = Project.objects.create(name='Viewer project', owner=owner)
    ProjectMember.objects.create(project=project, user=viewer, role='viewer')
    client = APIClient()
    client.force_authenticate(viewer)

    response = client.post('/api/versions/', {
        'name': 'Denied release', 'project_ids': [project.pk],
    }, format='json')
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert not Version.objects.filter(name='Denied release').exists()
