import pytest
from rest_framework.test import APIClient

from apps.executions.models import ExecutionArtifact, ExecutionAttempt, ExecutionJob
from apps.projects.models import Project
from apps.users.models import User


@pytest.mark.django_db
def test_evidence_explorer_is_project_scoped_and_redacts_private_references():
    owner = User.objects.create_user(username='evidence-owner', password='pass')
    outsider = User.objects.create_user(username='evidence-outsider', password='pass')
    project = Project.objects.create(name='Evidence Project', owner=owner)
    job = ExecutionJob.objects.create(project=project, requested_by=owner)
    attempt = ExecutionAttempt.objects.create(job=job, attempt_number=1)
    artifact = ExecutionArtifact.objects.create(
        job=job,
        attempt=attempt,
        uri=r'C:\private\run\trace.zip',
        media_type='application/zip',
        content_hash='abc123',
        metadata={'token': 'secret-value', 'label': 'trace'},
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get(f'/api/executions/evidence-explorer/?project={project.pk}&job={job.pk}')
    assert response.status_code == 200
    assert response.data['counts']['execution_artifacts'] == 1
    row = response.data['execution_artifacts'][0]
    assert row['id'] == artifact.pk
    assert row['uri'] == ''
    assert 'secret-value' not in str(row['metadata'])
    assert row['content_hash'] == 'abc123'

    client.force_authenticate(user=outsider)
    denied = client.get(f'/api/executions/evidence-explorer/?project={project.pk}')
    assert denied.status_code == 404
