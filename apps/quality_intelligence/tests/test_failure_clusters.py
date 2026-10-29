import pytest
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.quality_intelligence.models import (
    QualityFailure,
    QualityFailureClusterAudit,
    QualityRun,
    QualityRunItem,
)
from apps.quality_intelligence.services.clusters import sync_failure_clusters
from apps.users.models import User


@pytest.fixture
def clustered_runs(db):
    owner = User.objects.create_user(username='cluster-owner', password='pass')
    outsider = User.objects.create_user(username='cluster-outsider', password='pass')
    project = Project.objects.create(name='Cluster Project', owner=owner)
    runs = []
    for index in (1, 2):
        run = QualityRun.objects.create(
            source_type='API',
            source_scope='SUITE',
            source_execution_id=f'cluster-run-{index}',
            project_type='PROJECT',
            project_id=str(project.pk),
            environment_fingerprint='staging',
        )
        item = QualityRunItem.objects.create(
            run=run,
            source_item_type='CASE',
            test_key='checkout.login',
            name='Checkout login',
            outcome='FAIL',
            environment_fingerprint='staging',
        )
        QualityFailure.objects.create(
            run=run,
            item=item,
            dedupe_key=f'failure-{index}',
            taxonomy_code='PRODUCT',
            initial_taxonomy_code='PRODUCT',
            signature='same-signature',
            message='expected 200 actual 500',
        )
        runs.append(run)
    return owner, outsider, project, runs


@pytest.mark.django_db
def test_failure_clusters_are_cross_run_idempotent_and_project_scoped(clustered_runs):
    owner, outsider, project, runs = clustered_runs
    sync_failure_clusters(runs[0])
    sync_failure_clusters(runs[1])

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get('/api/quality-intelligence/failure-clusters/')
    assert response.status_code == 200
    assert response.data['count'] == 1
    cluster_id = response.data['results'][0]['id']
    assert response.data['results'][0]['occurrence_count'] == 2

    owner_response = client.post(
        f'/api/quality-intelligence/failure-clusters/{cluster_id}/owner/',
        {'owner_id': owner.pk},
        format='json',
    )
    assert owner_response.status_code == 200

    defect_response = client.post(
        f'/api/quality-intelligence/failure-clusters/{cluster_id}/create-defect/',
        {'title': 'Checkout login regression'},
        format='json',
    )
    assert defect_response.status_code == 201
    defect_id = defect_response.data['defect']
    duplicate = client.post(
        f'/api/quality-intelligence/failure-clusters/{cluster_id}/create-defect/',
        {'title': 'Duplicate should be ignored'},
        format='json',
    )
    assert duplicate.status_code == 200
    assert duplicate.data['defect'] == defect_id
    assert QualityFailureClusterAudit.objects.filter(cluster_id=cluster_id).count() >= 2

    client.force_authenticate(user=outsider)
    assert client.get('/api/quality-intelligence/failure-clusters/').data['count'] == 0
    assert client.get(f'/api/quality-intelligence/failure-clusters/{cluster_id}/').status_code == 404


@pytest.mark.django_db
def test_failure_cluster_marks_later_pass_as_fixed(clustered_runs):
    owner, _outsider, project, runs = clustered_runs
    sync_failure_clusters(runs[0])
    sync_failure_clusters(runs[1])
    passed_run = QualityRun.objects.create(
        source_type='API',
        source_scope='SUITE',
        source_execution_id='cluster-fixed-run',
        project_type='PROJECT',
        project_id=str(project.pk),
        environment_fingerprint='staging',
    )
    QualityRunItem.objects.create(
        run=passed_run,
        source_item_type='CASE',
        test_key='checkout.login',
        name='Checkout login',
        outcome='PASS',
        environment_fingerprint='staging',
    )
    sync_failure_clusters(passed_run)
    client = APIClient()
    client.force_authenticate(user=owner)
    cluster = client.get('/api/quality-intelligence/failure-clusters/').data['results'][0]
    assert cluster['resolved_at'] is not None
    assert cluster['last_fixed_run'] == passed_run.pk
