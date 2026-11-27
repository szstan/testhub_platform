"""Project write authorization must only inspect validated identifiers."""
import uuid
from unittest.mock import Mock

import pytest
from rest_framework.test import APIClient

from apps.projects.models import Project, ProjectMember
from apps.requirement_analysis.models import TestCaseGenerationTask as GenerationTask
from apps.requirement_analysis.tasks import generate_test_case_task
from apps.requirement_analysis.tests.test_generation_worker import setup_generation
from apps.users.models import User


pytestmark = pytest.mark.django_db(transaction=True)
GENERATE_URL = '/api/requirement-analysis/testcase-generation/generate/'


@pytest.mark.parametrize('raw_project', ['1e9999', '-1e9999', '"not-an-id"', '[]', '{}', 'null', 'true', '"1.5"'])
def test_invalid_project_input_returns_400_before_orm_or_dispatch(raw_project, monkeypatch):
    user = User.objects.create_user(username='permission-input-user', password='test-pass')
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.generic(
        'POST', GENERATE_URL,
        '{"title":"synthetic","requirement_text":"synthetic requirement","project":' + raw_project + '}',
        content_type='application/json',
    )

    assert response.status_code == 400
    assert 'project' in response.json()
    assert GenerationTask.objects.count() == 0
    dispatch.assert_not_called()


@pytest.mark.parametrize('raw_payload', ['[]', '"not-an-object"', 'true'])
def test_non_object_generation_body_returns_400(raw_payload, monkeypatch):
    user = User.objects.create_user(username='permission-body-user', password='test-pass')
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.generic('POST', GENERATE_URL, raw_payload, content_type='application/json')

    assert response.status_code == 400
    assert GenerationTask.objects.count() == 0
    dispatch.assert_not_called()


@pytest.mark.parametrize('representation', ['integer', 'string', 'decimal-string'])
def test_normalized_project_ids_still_require_viewer_write_authorization(representation, monkeypatch):
    owner = User.objects.create_user(username='permission-project-owner', password='test-pass')
    viewer = User.objects.create_user(username='permission-project-viewer', password='test-pass')
    project = Project.objects.create(name='Viewer project', owner=owner)
    ProjectMember.objects.create(project=project, user=viewer, role='viewer')
    project_value = project.pk
    if representation == 'string':
        project_value = str(project.pk)
    elif representation == 'decimal-string':
        project_value = f'{project.pk}.0'
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)
    client = APIClient()
    client.force_authenticate(user=viewer)

    response = client.post(GENERATE_URL, {
        'title': 'Not writable', 'requirement_text': 'Synthetic requirement', 'project': project_value,
    }, format='json')

    assert response.status_code == 403
    assert GenerationTask.objects.count() == 0
    dispatch.assert_not_called()


def test_valid_normalized_project_id_preserves_generation_admission(monkeypatch):
    relations = setup_generation('permission-normalized-owner', auto_review=False)
    user, project = relations[:2]
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post(GENERATE_URL, {
        'title': 'Valid admission', 'requirement_text': 'Synthetic requirement',
        'project': f'{project.pk}.0', 'output_mode': 'complete',
    }, format='json')

    assert response.status_code == 201
    task = GenerationTask.objects.get(task_id=response.json()['task_id'])
    assert task.project_id == project.pk
    assert task.writer_model_config_id == relations[2].pk
    dispatch.assert_called_once()


def test_creator_can_recover_identical_submission_after_becoming_project_viewer(monkeypatch):
    relations = setup_generation('permission-replay-creator', auto_review=False)
    creator, project = relations[:2]
    owner = User.objects.create_user(username='permission-replay-owner', password='test-pass')
    project.owner = owner
    project.save(update_fields=['owner'])
    member = ProjectMember.objects.create(project=project, user=creator, role='tester')
    dispatch = Mock(return_value=None)
    monkeypatch.setattr(generate_test_case_task, 'apply_async', dispatch)
    client = APIClient()
    client.force_authenticate(user=creator)
    payload = {
        'title': 'Existing submission', 'requirement_text': 'Synthetic requirement',
        'project': project.pk, 'client_request_id': str(uuid.uuid4()), 'output_mode': 'complete',
    }
    created = client.post(GENERATE_URL, payload, format='json')
    assert created.status_code == 201
    member.role = 'viewer'
    member.save(update_fields=['role'])

    replay = client.post(GENERATE_URL, payload, format='json')
    another = client.post(GENERATE_URL, {**payload, 'client_request_id': str(uuid.uuid4())}, format='json')

    assert replay.status_code == 200
    assert replay.json()['replayed'] is True
    assert replay.json()['task_id'] == created.json()['task_id']
    assert another.status_code == 403
    assert GenerationTask.objects.count() == 1
    dispatch.assert_called_once()
