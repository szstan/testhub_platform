import pytest
from rest_framework.test import APIClient

from apps.executions.models import ExecutionArtifact, ExecutionAttempt, ExecutionJob, ExecutionStepResult
from apps.projects.models import Project
from apps.users.models import User


@pytest.mark.django_db
@pytest.mark.parametrize('resource', ['attempts', 'step-results', 'artifacts'])
def test_execution_resource_lists_have_scoped_serializers_and_working_pagination(resource):
    owner = User.objects.create_user(username='resource-owner', password='pass')
    project = Project.objects.create(name='Resource project', owner=owner)
    job = ExecutionJob.objects.create(project=project)
    attempt = ExecutionAttempt.objects.create(job=job, attempt_number=1)
    step = ExecutionStepResult.objects.create(attempt=attempt, case_key='case', step_key='step')
    artifact = ExecutionArtifact.objects.create(job=job, attempt=attempt, uri='urn:testhub:artifact')
    expected = {'attempts': attempt.pk, 'step-results': step.pk, 'artifacts': artifact.pk}[resource]
    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get(f'/api/executions/{resource}/', {'page_size': 1})
    assert response.status_code == 200, response.data
    assert response.data['count'] == 1
    assert response.data['results'][0]['id'] == expected

    outsider = User.objects.create_user(username='resource-outsider', password='pass')
    client.force_authenticate(user=outsider)
    empty = client.get(f'/api/executions/{resource}/')
    assert empty.status_code == 200
    assert empty.data['count'] == 0
    assert client.get(f'/api/executions/{resource}/{expected}/').status_code == 404
