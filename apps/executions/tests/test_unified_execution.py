import pytest
from rest_framework.test import APIClient

from apps.executions.models import (
    ExecutionArtifact,
    ExecutionJob,
    ExecutionStepResult,
)
from apps.executions.state_machine import (
    ErrorType,
    ExecutionStatus,
    InvalidExecutionTransition,
    StepStatus,
)
from apps.projects.models import Project
from apps.users.models import User


@pytest.fixture
def execution_users(db):
    owner = User.objects.create_user(username='execution-owner', password='pass')
    outsider = User.objects.create_user(username='execution-outsider', password='pass')
    project = Project.objects.create(name='Execution Project', owner=owner)
    return owner, outsider, project


def _job_payload(project_id, *, source_id='case-1'):
    return {
        'project': project_id,
        'source_type': 'api',
        'source_id': source_id,
        'trigger': 'manual',
        'environment_snapshot': {'name': 'test'},
        'priority': 'normal',
    }


@pytest.mark.django_db
def test_execution_job_is_idempotent_and_project_scoped(execution_users):
    owner, outsider, project = execution_users
    client = APIClient()
    client.force_authenticate(user=owner)

    first = client.post(
        '/api/executions/jobs/',
        _job_payload(project.id),
        format='json',
        HTTP_IDEMPOTENCY_KEY='job-key-1',
    )
    assert first.status_code == 201
    job_id = first.data['id']
    assert first.data['status'] == ExecutionStatus.PENDING
    assert first.data['execution_supported'] is False

    replay = client.post(
        '/api/executions/jobs/',
        _job_payload(project.id),
        format='json',
        HTTP_IDEMPOTENCY_KEY='job-key-1',
    )
    assert replay.status_code == 200
    assert replay.data['id'] == job_id
    assert ExecutionJob.objects.filter(project=project).count() == 1

    conflict = client.post(
        '/api/executions/jobs/',
        _job_payload(project.id, source_id='different-case'),
        format='json',
        HTTP_IDEMPOTENCY_KEY='job-key-1',
    )
    assert conflict.status_code == 409

    client.force_authenticate(user=outsider)
    assert client.get('/api/executions/jobs/').status_code == 200
    assert client.get('/api/executions/jobs/').data['count'] == 0
    denied = client.post(
        '/api/executions/jobs/',
        _job_payload(project.id, source_id='outsider-case'),
        format='json',
    )
    assert denied.status_code in {403, 400}


def test_execution_state_machine_rejects_terminal_regression():
    job = ExecutionJob(status=ExecutionStatus.PENDING)
    job.transition_to(ExecutionStatus.QUEUED, save=False)
    job.transition_to(ExecutionStatus.RUNNING, save=False)
    job.transition_to(ExecutionStatus.PASSED, save=False)

    with pytest.raises(InvalidExecutionTransition):
        job.transition_to(ExecutionStatus.RUNNING, save=False)


def test_execution_artifact_inherits_job_from_step_result(db):
    owner = User.objects.create_user(username='artifact-owner', password='pass')
    project = Project.objects.create(name='Artifact Project', owner=owner)
    job = ExecutionJob.objects.create(project=project, requested_by=owner)
    attempt, created = job.create_attempt()
    assert created is True
    step = ExecutionStepResult.objects.create(
        attempt=attempt,
        case_key='case-1',
        step_key='step-1',
        status=StepStatus.PASSED,
        error_type=ErrorType.UNKNOWN,
    )
    artifact = ExecutionArtifact.objects.create(
        step_result=step,
        uri='artifact://execution/1/step-1.json',
        content_hash='sha256:test',
    )
    artifact.refresh_from_db()
    assert artifact.job_id == job.id
    assert artifact.attempt_id == attempt.id
    assert artifact.step_result_id == step.id


def test_execution_step_result_has_stable_case_step_identity(db):
    owner = User.objects.create_user(username='step-owner', password='pass')
    project = Project.objects.create(name='Step Project', owner=owner)
    job = ExecutionJob.objects.create(project=project, requested_by=owner)
    attempt, _ = job.create_attempt()
    first = ExecutionStepResult.objects.create(
        attempt=attempt,
        case_key='case-1',
        step_key='step-1',
    )
    assert first.case_key == 'case-1'
    assert first.step_key == 'step-1'
    with pytest.raises(Exception):
        ExecutionStepResult.objects.create(
            attempt=attempt,
            case_key='case-1',
            step_key='step-1',
        )
