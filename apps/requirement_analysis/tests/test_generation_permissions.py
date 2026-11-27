import pytest
from rest_framework.test import APIClient

from apps.projects.models import Project, ProjectMember
from apps.requirement_analysis.models import TestCaseGenerationTask as GenerationTask
from apps.testcases.models import TestCase as SavedTestCase
from apps.users.models import User


pytestmark = pytest.mark.django_db


VALID_OUTPUT = """## AI-001: Login succeeds
### Preconditions
A registered user exists.
### Steps
1. Enter valid credentials.
### Expected result
The user is redirected to the dashboard.
"""


def make_user(username, **kwargs):
    return User.objects.create_user(username=username, password='StrongPass123', **kwargs)


def make_task(creator, project=None):
    return GenerationTask.objects.create(
        task_id=f'TASK-{creator.pk}-{project.pk if project else "legacy"}',
        title='Generated cases',
        requirement_text='Login behavior',
        status='completed',
        output_mode='complete',
        final_test_cases=VALID_OUTPUT,
        project=project,
        created_by=creator,
    )


def task_url(task, suffix=''):
    return f'/api/requirement-analysis/testcase-generation/{task.task_id}/{suffix}'


def test_viewer_can_read_and_get_stream_ticket_but_all_task_writes_are_forbidden():
    owner = make_user('generation-permission-owner')
    project = Project.objects.create(name='Viewer permission project', owner=owner)
    task = make_task(owner, project)
    viewer = make_user('generation-permission-viewer')
    ProjectMember.objects.create(project=project, user=viewer, role='viewer')
    client = APIClient()
    client.force_authenticate(user=viewer)

    assert client.get(task_url(task)).status_code == 200
    assert client.post(task_url(task, 'stream-ticket/'), {}, format='json').status_code == 200

    writes = [
        client.patch(task_url(task), {'title': 'tampered'}, format='json'),
        client.delete(task_url(task)),
        client.post(task_url(task, 'cancel/'), {}, format='json'),
        client.post(task_url(task, 'batch_discard/'), {}, format='json'),
        client.post(task_url(task, 'discard-selected-cases/'), {'case_indices': [0]}, format='json'),
        client.post(task_url(task, 'discard-single-case/'), {'case_index': 0}, format='json'),
        client.post(task_url(task, 'update-test-cases/'), {'final_test_cases': 'tampered'}, format='json'),
        client.post(task_url(task, 'save_to_records/'), {}, format='json'),
        client.post(task_url(task, 'batch_adopt/'), {}, format='json'),
        client.post(task_url(task, 'batch-adopt-selected/'), {'case_ids': ['AI-001']}, format='json'),
    ]

    assert [response.status_code for response in writes] == [403] * len(writes)
    task.refresh_from_db()
    assert GenerationTask.objects.filter(pk=task.pk).exists()
    assert task.title == 'Generated cases'
    assert task.final_test_cases == VALID_OUTPUT
    assert task.status == 'completed'
    assert SavedTestCase.objects.filter(project=project).count() == 0


def test_removed_project_member_keeps_creator_read_access_but_loses_write_access():
    creator = make_user('generation-permission-removed-creator')
    owner = make_user('generation-permission-removed-owner')
    project = Project.objects.create(name='Removed member project', owner=owner)
    ProjectMember.objects.create(project=project, user=creator, role='tester')
    task = make_task(creator, project)
    project.members.remove(creator)
    client = APIClient()
    client.force_authenticate(user=creator)

    assert client.get(task_url(task)).status_code == 200
    response = client.patch(task_url(task), {'title': 'tampered'}, format='json')

    assert response.status_code == 403
    task.refresh_from_db()
    assert task.title == 'Generated cases'


@pytest.mark.parametrize('actor_role', ['tester', 'project-owner', 'staff'])
def test_writable_project_roles_can_update_generation_tasks(actor_role):
    owner = make_user(f'generation-permission-write-owner-{actor_role}')
    project = Project.objects.create(name=f'Writable role project {actor_role}', owner=owner)
    task = make_task(owner, project)
    if actor_role == 'project-owner':
        actor = owner
    elif actor_role == 'tester':
        actor = make_user(f'generation-permission-tester-{actor_role}')
        ProjectMember.objects.create(project=project, user=actor, role='tester')
    else:
        actor = make_user(f'generation-permission-staff-{actor_role}', is_staff=True)

    client = APIClient()
    client.force_authenticate(user=actor)
    response = client.patch(task_url(task), {'title': f'updated by {actor_role}'}, format='json')

    assert response.status_code == 200
    task.refresh_from_db()
    assert task.title == f'updated by {actor_role}'


def test_legacy_unbound_task_is_writable_only_by_creator_or_staff():
    creator = make_user('generation-permission-legacy-creator')
    task = make_task(creator)
    outsider = make_user('generation-permission-legacy-outsider')
    outsider_client = APIClient()
    outsider_client.force_authenticate(user=outsider)

    denied = outsider_client.patch(task_url(task), {'title': 'outsider'}, format='json')
    assert denied.status_code == 403

    creator_client = APIClient()
    creator_client.force_authenticate(user=creator)
    allowed = creator_client.patch(task_url(task), {'title': 'creator update'}, format='json')
    assert allowed.status_code == 200

    task.refresh_from_db()
    assert task.title == 'creator update'

    staff = make_user('generation-permission-legacy-staff', is_staff=True)
    staff_client = APIClient()
    staff_client.force_authenticate(user=staff)
    staff_update = staff_client.patch(task_url(task), {'title': 'staff update'}, format='json')
    assert staff_update.status_code == 200


def test_generate_requires_project_write_permission_and_generic_post_is_disabled(monkeypatch):
    from unittest.mock import Mock

    from apps.requirement_analysis.tasks import generate_test_case_task

    owner = make_user('generation-permission-generate-owner')
    project = Project.objects.create(name='Generate viewer project', owner=owner)
    viewer = make_user('generation-permission-generate-viewer')
    ProjectMember.objects.create(project=project, user=viewer, role='viewer')
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)
    client = APIClient()
    client.force_authenticate(user=viewer)

    denied = client.post('/api/requirement-analysis/testcase-generation/generate/', {
        'title': 'Not allowed',
        'requirement_text': 'Synthetic requirement',
        'project': project.pk,
        'output_mode': 'complete',
    }, format='json')
    generic_create = client.post('/api/requirement-analysis/testcase-generation/', {
        'title': 'Generic bypass',
        'requirement_text': 'Synthetic requirement',
        'project': project.pk,
    }, format='json')

    assert denied.status_code == 403
    assert generic_create.status_code == 405
    assert GenerationTask.objects.count() == 0
    dispatch.assert_not_called()
