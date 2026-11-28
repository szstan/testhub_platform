import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.projects.models import Project, ProjectEnvironment, ProjectMember
from apps.users.models import User


pytestmark = pytest.mark.django_db


def test_environment_writes_follow_project_roles_and_member_output_is_private():
    owner = User.objects.create_user(username='asset-owner', phone='1111111111')
    viewer = User.objects.create_user(username='asset-viewer', phone='2222222222')
    writer = User.objects.create_user(username='asset-writer', phone='3333333333')
    project = Project.objects.create(name='Asset scope', owner=owner)
    ProjectMember.objects.create(project=project, user=viewer, role='viewer')
    ProjectMember.objects.create(project=project, user=writer, role='tester')
    url = f'/api/projects/{project.pk}/environments/'

    viewer_client = APIClient()
    viewer_client.force_authenticate(viewer)
    assert viewer_client.get(url).status_code == status.HTTP_200_OK
    denied = viewer_client.post(url, {
        'name': 'Viewer environment', 'base_url': 'https://example.test',
    }, format='json')
    assert denied.status_code == status.HTTP_403_FORBIDDEN

    writer_client = APIClient()
    writer_client.force_authenticate(writer)
    created = writer_client.post(url, {
        'name': 'Writer environment', 'base_url': 'https://example.test',
    }, format='json')
    assert created.status_code == status.HTTP_201_CREATED, created.data
    assert ProjectEnvironment.objects.filter(project=project).count() == 1

    member_response = viewer_client.get(f'/api/projects/{project.pk}/members/')
    assert member_response.status_code == status.HTTP_200_OK
    serialized_viewer = next(row for row in member_response.data if row['id'] == viewer.pk)
    assert serialized_viewer['role'] == 'viewer'
    assert 'phone' not in serialized_viewer
    assert 'is_staff' not in serialized_viewer

    detail = viewer_client.get(f'/api/projects/{project.pk}/')
    assert detail.status_code == status.HTTP_200_OK
    assert 'phone' not in detail.data['owner']
    assert 'is_staff' not in detail.data['owner']


def test_viewer_cannot_change_project_or_members():
    owner = User.objects.create_user(username='project-manager')
    viewer = User.objects.create_user(username='project-viewer')
    project = Project.objects.create(name='Viewer read only', owner=owner)
    member = ProjectMember.objects.create(project=project, user=viewer, role='viewer')
    client = APIClient()
    client.force_authenticate(viewer)

    assert client.patch(f'/api/projects/{project.pk}/', {'name': 'Changed'}).status_code == 403
    assert client.post(f'/api/projects/{project.pk}/members/add/', {
        'user_id': owner.pk, 'role': 'tester',
    }, format='json').status_code == 403
    assert client.delete(f'/api/projects/{project.pk}/members/{member.pk}/').status_code == 403
