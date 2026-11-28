import pytest
from django.contrib.auth.models import AnonymousUser

from apps.projects.access import user_can_write_project, writable_projects
from apps.projects.models import Project, ProjectMember
from apps.users.models import User


@pytest.mark.django_db
@pytest.mark.parametrize('role,allowed', [
    ('owner', True), ('admin', True), ('developer', True), ('tester', True),
    ('viewer', False),
])
def test_project_write_policy_distinguishes_member_roles(role, allowed):
    owner = User.objects.create_user(username='policy-owner')
    actor = User.objects.create_user(username='policy-member')
    project = Project.objects.create(name='Role policy', owner=owner)
    ProjectMember.objects.create(project=project, user=actor, role=role)
    assert user_can_write_project(actor, project) is allowed
    assert writable_projects(actor).filter(pk=project.pk).exists() is allowed


@pytest.mark.django_db
def test_project_write_policy_keeps_explicit_owner_and_staff_access_and_scope():
    owner = User.objects.create_user(username='policy-direct-owner')
    outsider = User.objects.create_user(username='policy-outsider')
    staff = User.objects.create_user(username='policy-staff', is_staff=True)
    project = Project.objects.create(name='Owned', owner=owner)
    other = Project.objects.create(name='Unrelated', owner=outsider)
    assert user_can_write_project(owner, project)
    assert user_can_write_project(staff, project)
    assert not user_can_write_project(outsider, project)
    assert not user_can_write_project(AnonymousUser(), project)
    assert not user_can_write_project(None, project)
    assert not user_can_write_project(owner, None)
    assert not writable_projects(staff, Project.objects.filter(pk=project.pk)).filter(pk=other.pk).exists()
