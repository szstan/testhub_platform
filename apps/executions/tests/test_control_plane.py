import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from apps.executions.models import ExecutionJob, ExecutionQualityGateEvaluation, ExecutionSourceBinding
from apps.projects.models import Project
from apps.users.models import User


@pytest.mark.django_db
def test_control_plane_metrics_are_scoped_and_report_readiness():
    owner = User.objects.create_user(username='control-owner', password='pass')
    outsider = User.objects.create_user(username='control-outsider', password='pass')
    project = Project.objects.create(name='Control Project', owner=owner)
    ExecutionSourceBinding.objects.create(
        project=project,
        source_type='api',
        source_id='api:suite:42',
        source_project_id='legacy-api-project',
        created_by=owner,
    )
    job = ExecutionJob.objects.create(
        project=project,
        source_type='api',
        source_id='api:suite:42',
        requested_by=owner,
        result={'unmatched': 0},
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    with override_settings(EXECUTION_DISPATCH_ROUTES={'api': 'apps.executions.tasks.execute_api_source'}):
        response = client.get('/api/executions/metrics/?project=%s' % project.pk)
    assert response.status_code == 200
    assert response.data['recent_runs'][0]['id'] == job.pk
    assert response.data['route_readiness']['api'] is True
    assert response.data['enabled_bindings']['api'] == 1
    assert 'integration_health' in response.data
    assert response.data['scale']['execution_jobs'] == 1


    client.force_authenticate(user=outsider)
    forbidden = client.get('/api/executions/metrics/?project=%s' % project.pk)
    assert forbidden.status_code == 200
    assert forbidden.data['total_jobs'] == 0
    assert forbidden.data['recent_runs'] == []


@pytest.mark.django_db
def test_release_gate_persists_declared_policy_snapshot():
    owner = User.objects.create_user(username='policy-owner', password='pass')
    project = Project.objects.create(name='Policy Project', owner=owner)
    job = ExecutionJob.objects.create(
        project=project,
        source_type='ci',
        source_id='ci-run-1',
        requested_by=owner,
        result={'failed': 0, 'unmatched': 0},
    )
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post(
        f'/api/executions/jobs/{job.pk}/release-gate/',
        {
            'max_failed': 0,
            'max_unmatched': 0,
            'policy_version': 'release-2026.10',
            'policy_snapshot': {'mode': 'blocking', 'owner': 'qa'},
        },
        format='json',
    )
    assert response.status_code == 200
    assert response.data['policy_version'] == 'release-2026.10'
    assert response.data['policy_snapshot']['declared']['mode'] == 'blocking'
    evaluation = ExecutionQualityGateEvaluation.objects.get(pk=response.data['evaluation_id'])
    assert evaluation.policy_version == 'release-2026.10'
    assert evaluation.policy_snapshot['declared']['owner'] == 'qa'

    replay = client.post(
        f'/api/executions/jobs/{job.pk}/release-gate/',
        {
            'max_failed': 0,
            'max_unmatched': 0,
            'policy_version': 'release-2026.10',
            'policy_snapshot': {'mode': 'blocking', 'owner': 'qa'},
        },
        format='json',
    )
    assert replay.status_code == 200
    assert replay.data['evaluation_id'] == evaluation.pk
    assert ExecutionQualityGateEvaluation.objects.filter(job=job, stage='release').count() == 1
