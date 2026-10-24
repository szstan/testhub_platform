import pytest
from rest_framework.test import APIClient

from apps.executions.models import ExecutionJob
from apps.projects.models import Project
from apps.users.models import User


@pytest.mark.django_db
def test_task_center_filters_and_timeline_are_project_scoped():
    owner = User.objects.create_user(username='task-center-owner', password='pass')
    outsider = User.objects.create_user(username='task-center-outsider', password='pass')
    project = Project.objects.create(name='Task Center Project', owner=owner)
    job = ExecutionJob.objects.create(
        project=project,
        source_type='performance',
        source_id='perf-1',
        build_reference='build-42',
        requested_by=owner,
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get('/api/executions/jobs/?source_type=performance&ordering=-created_at')
    assert response.status_code == 200
    assert response.data['count'] == 1
    assert response.data['results'][0]['id'] == job.id

    timeline = client.get(f'/api/executions/jobs/{job.id}/timeline/')
    assert timeline.status_code == 200
    assert timeline.data['job']['id'] == job.id
    assert timeline.data['attempts'] == []

    client.force_authenticate(user=outsider)
    assert client.get('/api/executions/jobs/').data['count'] == 0
    assert client.get(f'/api/executions/jobs/{job.id}/timeline/').status_code == 404
