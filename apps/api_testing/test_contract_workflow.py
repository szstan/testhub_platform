"""Regressions for the browser's contract job and project-permission workflow."""
from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIClient

from apps.api_testing.models import OpenApiContractSpec
from apps.executions.models import ExecutionAttempt, ExecutionJob
from apps.projects.models import Project, ProjectMember
from apps.users.models import User


@pytest.fixture
def contract_workspace(db):
    owner = User.objects.create_user(username='contract-page-owner', password='test-password')
    outsider = User.objects.create_user(username='contract-page-outsider', password='test-password')
    project = Project.objects.create(name='Contract page project', owner=owner)
    spec = OpenApiContractSpec.objects.create(
        project=project, name='Browser contract', version='1', created_by=owner,
        document={
            'openapi': '3.0.3',
            'servers': [{'url': 'https://api.example.test'}],
            'paths': {'/items': {'get': {'operationId': 'listItems', 'responses': {'200': {'description': 'ok'}}}}},
        },
        document_hash='test-hash',
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    return client, owner, outsider, project, spec


@pytest.mark.django_db
@pytest.mark.parametrize('flags', [{'is_staff': True}, {'is_superuser': True}])
def test_admin_can_create_and_edit_contracts_in_visible_nonmember_projects(contract_workspace, flags):
    client, owner, _, project, spec = contract_workspace
    admin = User.objects.create_user(username='contract-page-admin', password='test-password', **flags)
    assert not project.members.filter(pk=admin.pk).exists()
    assert project.owner_id == owner.pk
    client.force_authenticate(user=admin)

    projects = client.get('/api/projects/all/')
    assert projects.status_code == 200
    assert project.pk in {row['id'] for row in projects.data}
    assert client.get(f'/api/api-testing/openapi-contracts/{spec.pk}/').status_code == 200
    created = client.post('/api/api-testing/openapi-contracts/', {
        'project': project.pk, 'name': 'Admin-managed contract', 'version': '1', 'document': spec.document,
    }, format='json')
    assert created.status_code == 201, created.data
    updated = client.patch(f'/api/api-testing/openapi-contracts/{spec.pk}/', {
        'project': project.pk, 'name': 'Edited by administrator', 'version': '2',
    }, format='json')
    assert updated.status_code == 200, updated.data
    spec.refresh_from_db()
    assert spec.name == 'Edited by administrator'
    assert spec.project_id == project.pk


@pytest.mark.django_db
def test_contract_write_scope_still_rejects_outsiders_and_accepts_members(contract_workspace):
    client, _, outsider, project, spec = contract_workspace
    client.force_authenticate(user=outsider)
    payload = {'project': project.pk, 'name': 'New contract', 'version': '1', 'document': spec.document}
    assert client.post('/api/api-testing/openapi-contracts/', payload, format='json').status_code == 400
    assert client.patch(f'/api/api-testing/openapi-contracts/{spec.pk}/', {'name': 'denied'}, format='json').status_code == 404
    ProjectMember.objects.create(project=project, user=outsider, role='tester')
    assert client.post('/api/api-testing/openapi-contracts/', payload, format='json').status_code == 201
    assert client.patch(f'/api/api-testing/openapi-contracts/{spec.pk}/', {'name': 'member edit'}, format='json').status_code == 200


@pytest.mark.django_db
def test_contract_job_filter_applies_before_pagination_and_rejects_any_attempt(contract_workspace):
    client, owner, outsider, project, _ = contract_workspace
    eligible = ExecutionJob.objects.create(project=project, requested_by=owner, source_type='manual')
    ExecutionJob.objects.bulk_create([
        ExecutionJob(project=project, requested_by=owner, source_type='manual', status='passed')
        for _ in range(105)
    ])
    for state in ('queued', 'running', 'cancelled', 'blocked', 'unsupported'):
        ExecutionJob.objects.create(project=project, requested_by=owner, status=state)
    already_attempted = ExecutionJob.objects.create(project=project, requested_by=owner)
    ExecutionAttempt.objects.create(job=already_attempted, attempt_number=1, status='pending')
    finalized = ExecutionJob.objects.create(project=project, requested_by=owner)
    final_attempt = ExecutionAttempt.objects.create(job=finalized, attempt_number=1, status='passed')
    finalized.final_attempt = final_attempt
    finalized.save(update_fields=['final_attempt'])
    other_project = Project.objects.create(name='Other contract project', owner=outsider)
    ExecutionJob.objects.create(project=other_project, requested_by=outsider)

    response = client.get('/api/executions/jobs/', {
        'project': project.pk, 'contract_eligible': 'true', 'page_size': 1, 'ordering': '-created_at',
    })
    assert response.status_code == 200, response.data
    assert response.data['count'] == 1
    assert [row['id'] for row in response.data['results']] == [eligible.pk]
    assert response.data['results'][0]['attempts_count'] == 0
    unfiltered = client.get('/api/executions/jobs/', {'project': project.pk, 'contract_eligible': 'false'})
    assert unfiltered.status_code == 200
    assert unfiltered.data['count'] > 100
    invalid = client.get('/api/executions/jobs/', {'contract_eligible': 'not-a-boolean'})
    assert invalid.status_code == 400
    assert 'contract_eligible' in invalid.data


@pytest.mark.django_db
def test_fresh_browser_contract_job_can_be_created_selected_and_executed_once(contract_workspace):
    client, _, _, project, spec = contract_workspace
    payload = {
        'project': project.pk, 'source_type': 'manual', 'source_id': f'openapi-contract:{spec.pk}',
        'trigger': 'manual', 'idempotency_key': 'contract-browser-create-request',
    }
    created = client.post('/api/executions/jobs/', payload, format='json')
    assert created.status_code == 201, created.data
    assert created.data['status'] == 'pending'
    assert created.data['attempts_count'] == 0
    replay = client.post('/api/executions/jobs/', payload, format='json')
    assert replay.status_code == 200
    assert replay.data['id'] == created.data['id']
    assert ExecutionJob.objects.filter(project=project).count() == 1
    ready = client.get('/api/executions/jobs/', {'project': project.pk, 'contract_eligible': 'true'})
    assert [row['id'] for row in ready.data['results']] == [created.data['id']]

    with patch('apps.api_testing.contract_runner.guarded_request', return_value=Mock(status_code=200)) as outbound:
        executed = client.post(f'/api/api-testing/openapi-contracts/{spec.pk}/execute/', {
            'job_id': created.data['id'], 'kinds': ['positive'], 'limit': 1,
        }, format='json')
        assert executed.status_code == 201, executed.data
        assert executed.data['status'] == 'passed'
        repeated = client.post(f'/api/api-testing/openapi-contracts/{spec.pk}/execute/', {
            'job_id': created.data['id'], 'kinds': ['positive'], 'limit': 1,
        }, format='json')
        assert repeated.status_code == 409
        assert outbound.call_count == 1
    consumed = client.get('/api/executions/jobs/', {'project': project.pk, 'contract_eligible': 'true'})
    assert consumed.data['count'] == 0
