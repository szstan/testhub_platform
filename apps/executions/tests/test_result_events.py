import hashlib
import pytest
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.executions.models import ExecutionArtifact, ExecutionEvent, ExecutionJob
from apps.executions.result_events import ResultEventIngestor
from apps.executions.state_machine import ExecutionStatus
from apps.projects.models import Project
from apps.quality_intelligence.models import QualityFailureCluster
from apps.reports.models import TestReport as LegacyReport
from apps.users.models import User


@pytest.fixture
def result_job(db):
    owner = User.objects.create_user(username='result-owner', password='pass')
    outsider = User.objects.create_user(username='result-outsider', password='pass')
    project = Project.objects.create(name='Result Project', owner=owner)
    job = ExecutionJob.objects.create(project=project, source_type='api', requested_by=owner)
    return owner, outsider, project, job


@pytest.mark.django_db
def test_result_events_are_idempotent_redacted_and_terminal(result_job):
    owner, _outsider, _project, job = result_job
    first = ResultEventIngestor.ingest(
        job,
        'run_started',
        {'attempt': 1, 'password': 'hidden', 'environment': 'test'},
        idempotency_key='run-start-1',
    )
    replay = ResultEventIngestor.ingest(
        job,
        'run_started',
        {'attempt': 1, 'password': 'hidden', 'environment': 'test'},
        idempotency_key='run-start-1',
    )
    assert first.created is True
    assert replay.created is False
    assert ExecutionEvent.objects.count() == 1
    assert ExecutionEvent.objects.get().payload['password'] == '[REDACTED]'

    ResultEventIngestor.ingest(
        job,
        'step_finished',
        {'attempt': 1, 'case_key': 'case-1', 'step_key': 'step-1', 'status': 'passed'},
        idempotency_key='step-1',
    )
    finished = ResultEventIngestor.ingest(
        job,
        'run_finished',
        {'attempt': 1, 'status': 'passed', 'summary': {'passed': 1}},
        idempotency_key='run-finish-1',
    )
    job.refresh_from_db()
    assert finished.status == ExecutionStatus.PASSED
    assert job.status == ExecutionStatus.PASSED
    report = LegacyReport.objects.get(execution_job=job)
    assert report.summary == {'passed': 1}


@pytest.mark.django_db
def test_final_failure_event_projects_cluster_when_quality_is_enabled(result_job):
    _owner, _outsider, _project, job = result_job
    with override_settings(QUALITY_INTELLIGENCE_ENABLED=True):
        ResultEventIngestor.ingest(
            job,
            'run_started',
            {'attempt': 1},
            idempotency_key='cluster-start',
        )
        ResultEventIngestor.ingest(
            job,
            'step_finished',
            {
                'attempt': 1,
                'case_key': 'checkout.login',
                'step_key': 'test',
                'status': 'failed',
                'error_type': 'assertion',
                'error_message': 'expected 200 actual 500',
            },
            idempotency_key='cluster-step',
        )
        ResultEventIngestor.ingest(
            job,
            'run_finished',
            {'attempt': 1, 'status': 'failed', 'error_message': 'failed'},
            idempotency_key='cluster-finish',
        )
    assert QualityFailureCluster.objects.filter(
        source_type='EXECUTION',
        project_id=str(job.project_id),
        test_key='checkout.login',
    ).exists()


@pytest.mark.django_db
def test_result_event_api_is_project_scoped(result_job):
    owner, outsider, project, job = result_job
    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.post(
        f'/api/executions/jobs/{job.id}/events/',
        {
            'event_type': 'run_started',
            'payload': {'attempt': 1, 'token': 'secret'},
            'idempotency_key': 'api-event-1',
        },
        format='json',
    )
    assert response.status_code == 201
    assert client.get('/api/executions/events/').data['count'] == 1

    client.force_authenticate(user=outsider)
    assert client.get('/api/executions/events/').data['count'] == 0
    assert client.post(
        f'/api/executions/jobs/{job.id}/events/',
        {'event_type': 'run_started', 'payload': {}, 'idempotency_key': 'outsider'},
        format='json',
    ).status_code == 404


@pytest.mark.django_db
def test_expired_artifact_cleanup_is_dry_run_by_default(result_job):
    _owner, _outsider, _project, job = result_job
    attempt, _created = job.create_attempt()
    artifact = ExecutionArtifact.objects.create(
        attempt=attempt,
        uri='artifact://expired.log',
        retention_until=timezone.now() - timedelta(minutes=1),
    )
    assert artifact.uri_hash == hashlib.sha256(artifact.uri.encode('utf-8')).hexdigest()
    output = StringIO()
    call_command('cleanup_execution_artifacts', stdout=output)
    assert 'DRY RUN' in output.getvalue()
    assert ExecutionArtifact.objects.filter(pk=artifact.pk).exists()
    call_command('cleanup_execution_artifacts', '--apply', stdout=StringIO())
    assert not ExecutionArtifact.objects.filter(pk=artifact.pk).exists()
