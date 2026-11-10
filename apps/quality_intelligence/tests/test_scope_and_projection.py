from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.apps import apps
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api_testing.models import ApiProject
from apps.executions.models import ExecutionAttempt, ExecutionJob
from apps.projects.models import Project
from apps.quality_intelligence.models import (
    QualityBaseline, QualityFailure, QualityFailureCluster, QualityQuarantine,
    QualityRun, QualityRunItem, QualityTestProfile,
)
from apps.quality_intelligence.permissions import quality_project_filter
from apps.quality_intelligence.services.clusters import sync_failure_clusters
from apps.quality_intelligence.services.flakiness import calculate_flakiness
from apps.quality_intelligence.services.profiles import refresh_profiles
from apps.quality_intelligence.services.unified import sync_unified_attempt
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def colliding_projects():
    unified_owner = User.objects.create_user(username='namespace-unified-owner')
    legacy_owner = User.objects.create_user(username='namespace-legacy-owner')
    project = Project.objects.create(pk=60101, name='Unified private project', owner=unified_owner)
    legacy = ApiProject.objects.create(pk=60101, name='Independent API project', owner=legacy_owner)
    job = ExecutionJob.objects.create(project=project, source_type='api', source_id='private-case', requested_by=unified_owner)
    return unified_owner, legacy_owner, project, legacy, job


def run_item(project_id, *, key='private-case', domain='PROJECT', outcome='FAIL', env='staging', finished=None, execution_id=None):
    run = QualityRun.objects.create(
        source_type='API', source_scope='CASE', project_type=domain, project_id=str(project_id),
        source_execution_id=execution_id or f'run-{QualityRun.objects.count()}',
        environment_fingerprint='staging', finished_at=finished,
    )
    item = QualityRunItem.objects.create(
        run=run, test_key=key, source_item_type='CASE', outcome=outcome,
        environment_fingerprint=env, name=key,
    )
    return run, item


def failure(run, item, signature='same-failure'):
    return QualityFailure.objects.create(
        run=run, item=item, dedupe_key=f'failure-{item.pk}-{signature}',
        signature=signature, taxonomy_code='PRODUCT', message='fixture failure',
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_unified_projection_is_only_visible_to_unified_project_owner(colliding_projects):
    owner, outsider, project, _legacy, job = colliding_projects
    attempt = ExecutionAttempt.objects.create(job=job, attempt_number=1, status='passed', is_final=True)
    projected = sync_unified_attempt(job, attempt, 'passed', {'message': 'unified-private-summary'})
    assert projected.project_type == 'PROJECT'
    profile = QualityTestProfile.objects.get(project_type='PROJECT', project_id=str(project.pk))
    quarantine = QualityQuarantine.objects.create(profile=profile, reason='fixture', starts_at=timezone.now())
    baseline = QualityBaseline.objects.create(
        source_type='API', project_type='PROJECT', project_id=str(project.pk), metric_key='latency',
        profile=profile, value=12,
    )
    for path in [
        f'runs/{projected.pk}/', f'profiles/{profile.pk}/', f'profiles/{profile.pk}/history/',
        f'quarantines/{quarantine.pk}/', f'baselines/{baseline.pk}/',
    ]:
        assert client_for(owner).get(f'/api/quality-intelligence/{path}').status_code == 200
        assert client_for(outsider).get(f'/api/quality-intelligence/{path}').status_code == 404
    assert client_for(outsider).get('/api/quality-intelligence/runs/').data['count'] == 0
    assert client_for(outsider).get('/api/quality-intelligence/profiles/').data['count'] == 0
    # Baseline creation must use the requested domain for authorization too.
    payload = {'source_type': 'API', 'project_type': 'PROJECT', 'project_id': str(project.pk), 'metric_key': 'p95', 'value': 4}
    assert client_for(outsider).post('/api/quality-intelligence/baselines/', payload, format='json').status_code == 404
    assert client_for(owner).post('/api/quality-intelligence/baselines/', payload, format='json').status_code == 201


def test_legacy_namespace_stays_independent_of_colliding_global_project(colliding_projects):
    owner, legacy_owner, _project, legacy, _job = colliding_projects
    run, _item = run_item(legacy.pk, domain='API')
    wrong_domain, _ = run_item(legacy.pk, domain='UI')
    assert list(quality_project_filter(QualityRun.objects.all(), legacy_owner)) == [run]
    assert not quality_project_filter(QualityRun.objects.all(), owner).filter(pk__in=[run.pk, wrong_domain.pk]).exists()


def test_migration_splits_profiles_and_retains_ambiguous_quarantine(colliding_projects):
    owner, legacy_owner, project, _legacy, job = colliding_projects
    run, item = run_item(project.pk, domain='API', execution_id=f'unified:{job.pk}')
    run.provenance = {'source': 'unified_execution', 'job_id': job.pk}
    run.save(update_fields=['provenance'])
    old_profile = QualityTestProfile.objects.create(
        source_type='API', project_type='API', project_id=str(project.pk), test_key=item.test_key,
        environment_fingerprint='staging', metadata={'source': 'unified_execution'},
    )
    quarantine = QualityQuarantine.objects.create(profile=old_profile, reason='old ambiguous approval', starts_at=timezone.now())
    orphan, _ = run_item(project.pk, domain='API', key='orphan', execution_id='unified:999999999')
    orphan.provenance = {'source': 'unified_execution', 'job_id': 999999999}
    orphan.save(update_fields=['provenance'])
    migration = import_module('apps.quality_intelligence.migrations.0007_project_namespaces_and_cluster_index')
    migration.repair_unified_namespaces(apps, SimpleNamespace(connection=connection))
    migration.repair_unified_namespaces(apps, SimpleNamespace(connection=connection))
    run.refresh_from_db()
    orphan.refresh_from_db()
    old_profile.refresh_from_db()
    quarantine.refresh_from_db()
    assert run.project_type == 'PROJECT'
    assert orphan.project_type == 'UNRESOLVED_UNIFIED'
    assert old_profile.project_type.startswith('UNRESOLVED_PROFILE:')
    assert quarantine.profile_id == old_profile.pk
    assert QualityTestProfile.objects.filter(project_type='PROJECT', test_key=item.test_key).count() == 1
    assert not quality_project_filter(QualityRun.objects.all(), legacy_owner).filter(pk=run.pk).exists()
    assert quality_project_filter(QualityRun.objects.all(), owner).filter(pk=run.pk).exists()


def test_profile_window_uses_latest_twenty_raw_rows_before_skip_exclusion():
    run, _ = run_item('1', key='window', outcome='FAIL')
    for index in range(50):
        QualityRunItem.objects.create(
            run=run, test_key='window', attempt=index + 2,
            outcome='PASS' if index < 35 else 'SKIP', environment_fingerprint='staging',
        )
    all_items = list(run.items.all())
    expected = calculate_flakiness(all_items, test_key='window', environment_fingerprint='staging')
    profiles = refresh_profiles(run, all_items)
    profile = profiles[('window', 'staging')]
    assert expected['eligible_attempts'] == 5
    assert profile.eligible_attempts == expected['eligible_attempts']
    assert profile.fail_count == expected['fail_count'] == 0
    assert profile.metadata['window_count'] == 20


def test_profile_batch_has_one_bounded_history_select_and_deduplicates_attempts():
    run, _ = run_item('1', key='batch-0')
    rows = [QualityRunItem(run=run, test_key=f'batch-{index}', attempt=attempt, outcome='PASS', environment_fingerprint='staging')
            for index in range(30) for attempt in range(2, 5)]
    QualityRunItem.objects.bulk_create(rows)
    items = list(run.items.all())
    with CaptureQueriesContext(connection) as queries:
        profiles = refresh_profiles(run, items)
    history = [q['sql'] for q in queries if 'ROW_NUMBER()' in q['sql']]
    assert len(profiles) == 30
    assert len(history) == 1
    assert '<= 20' in history[0]
    # Batch operations may split UPDATE parameters, but no query is emitted per attempt/profile.
    assert len(queries) < 15


def test_cluster_lifecycle_is_event_time_monotonic_and_unknown_environment_inherits():
    now = timezone.now()
    failed, item = run_item('1', key='timed', finished=now, env='unknown')
    failure(failed, item)
    sync_failure_clusters(failed)
    cluster = QualityFailureCluster.objects.get()
    assert cluster.environment_fingerprint == 'staging'
    old_pass, _ = run_item('1', key='timed', outcome='PASS', finished=now - timedelta(hours=1), env='unknown')
    sync_failure_clusters(old_pass)
    cluster.refresh_from_db()
    assert cluster.resolved_at is None
    new_pass, _ = run_item('1', key='timed', outcome='PASS', finished=now + timedelta(hours=2), env='unknown')
    sync_failure_clusters(new_pass)
    old_failure, item2 = run_item('1', key='timed', finished=now - timedelta(hours=2), env='unknown')
    failure(old_failure, item2)
    sync_failure_clusters(old_failure)
    cluster.refresh_from_db()
    assert cluster.resolved_at == new_pass.finished_at
    assert cluster.last_failure_run_id == failed.pk
    assert cluster.first_seen_at == old_failure.finished_at
    assert cluster.last_seen_at == failed.finished_at
    sync_failure_clusters(old_failure)
    cluster.refresh_from_db()
    assert cluster.occurrence_count == 2


def test_pass_resolution_and_occurrence_counts_are_batched():
    failed, first = run_item('1', key='case-0')
    failure(failed, first)
    for index in range(1, 30):
        item = QualityRunItem.objects.create(run=failed, test_key=f'case-{index}', outcome='FAIL', environment_fingerprint='staging')
        failure(failed, item)
    with CaptureQueriesContext(connection) as failure_queries:
        sync_failure_clusters(failed)
    counts = [q['sql'] for q in failure_queries if 'COUNT(' in q['sql'] and 'quality_failures' in q['sql']]
    assert len(counts) == 1
    passed, _ = run_item('1', key='case-0', outcome='PASS')
    QualityRunItem.objects.bulk_create([
        QualityRunItem(run=passed, test_key=f'case-{index}', outcome='PASS', environment_fingerprint='staging')
        for index in range(1, 30)
    ])
    with CaptureQueriesContext(connection) as pass_queries:
        sync_failure_clusters(passed)
    assert QualityFailureCluster.objects.filter(resolved_at__isnull=False).count() == 30
    assert len(pass_queries) < 10


def test_cluster_resolved_filter_and_profile_search(colliding_projects):
    owner, _outsider, project, _legacy, _job = colliding_projects
    now = timezone.now()
    failed, item = run_item(project.pk, key='resolved-filter', finished=now)
    failure(failed, item)
    sync_failure_clusters(failed)
    passed, _ = run_item(project.pk, key='resolved-filter', outcome='PASS', finished=now + timedelta(seconds=1))
    sync_failure_clusters(passed)
    client = client_for(owner)
    assert client.get('/api/quality-intelligence/failure-clusters/', {'resolved': 'true'}).data['count'] == 1
    assert client.get('/api/quality-intelligence/failure-clusters/', {'resolved': 'false'}).data['count'] == 0
    assert client.get('/api/quality-intelligence/failure-clusters/', {'resolved': 'invalid'}).status_code == 400
    profile = refresh_profiles(failed, [item])[('resolved-filter', 'staging')]
    result = client.get('/api/quality-intelligence/profiles/', {'profile_id': profile.pk, 'search': 'resolved-filter'})
    assert result.status_code == 200
    assert result.data['count'] == 1


def test_profile_history_inherits_run_environment_for_old_unknown_items():
    run, item = run_item('1', key='old-environment', env='unknown', outcome='PASS')
    profile = refresh_profiles(run, [item])[('old-environment', 'staging')]
    assert profile.eligible_attempts == 1
    assert profile.pass_count == 1


def test_migration_preserves_real_legacy_history_and_does_not_copy_approval(colliding_projects):
    _owner, legacy_owner, project, _legacy, job = colliding_projects
    unified_run, unified_item = run_item(project.pk, domain='API', outcome='FAIL', execution_id=f'unified:{job.pk}')
    unified_run.provenance = {'source': 'unified_execution', 'job_id': job.pk}
    unified_run.save(update_fields=['provenance'])
    legacy_run, _ = run_item(project.pk, domain='API', outcome='PASS', key=unified_item.test_key)
    old_profile = QualityTestProfile.objects.create(
        source_type='API', project_type='API', project_id=str(project.pk), test_key=unified_item.test_key,
        environment_fingerprint='staging', fail_count=1, metadata={'source': 'unified_execution'},
    )
    quarantine = QualityQuarantine.objects.create(profile=old_profile, reason='legacy approval', starts_at=timezone.now())
    migration = import_module('apps.quality_intelligence.migrations.0007_project_namespaces_and_cluster_index')
    migration.repair_unified_namespaces(apps, SimpleNamespace(connection=connection))
    old_profile.refresh_from_db()
    assert old_profile.project_type == 'API'
    assert old_profile.pass_count == 1
    assert old_profile.fail_count == 0
    assert QualityQuarantine.objects.get(pk=quarantine.pk).profile_id == old_profile.pk
    rebuilt = QualityTestProfile.objects.get(project_type='PROJECT', test_key=unified_item.test_key)
    assert rebuilt.fail_count == 1
    assert not rebuilt.quarantines.exists()
    assert list(quality_project_filter(QualityRun.objects.all(), legacy_owner)) == [legacy_run]
