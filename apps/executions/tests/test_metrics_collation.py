"""Behavior regressions for feature-gated metrics and real MySQL identifier collations."""
import re

import pytest
from django.db import connection
from django.db.models import CharField
from django.db.models.functions import Cast
from django.db.utils import OperationalError
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.executions.models import ExecutionJob
from apps.projects.models import Project
from apps.quality_intelligence.models import (
    QualityEvidence, QualityFailure, QualityFailureCluster, QualityRun,
    QualityTestProfile,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def metric_scope():
    owner = User.objects.create_user(username='metrics-hotfix-owner', password='fixture-only')
    outsider = User.objects.create_user(username='metrics-hotfix-outsider', password='fixture-only')
    project = Project.objects.create(name='Metrics owned project', owner=owner)
    foreign = Project.objects.create(name='Metrics foreign project', owner=outsider)
    ExecutionJob.objects.create(project=project, requested_by=owner, source_type='manual')
    ExecutionJob.objects.create(project=foreign, requested_by=outsider, source_type='manual')
    client = APIClient()
    client.force_authenticate(owner)
    return owner, outsider, project, foreign, client


def profile(project, *, domain='PROJECT', key='flaky', project_id=None):
    return QualityTestProfile.objects.create(
        source_type='API', project_type=domain,
        project_id=str(project.pk) if project_id is None else project_id,
        test_key=key, reliability_status='FLAKY',
    )


def quality_run(project, key):
    return QualityRun.objects.create(
        source_type='API', source_scope='CASE', project_type='PROJECT',
        project_id=str(project.pk), source_execution_id=key, source_fingerprint=key,
    )


@override_settings(QUALITY_INTELLIGENCE_ENABLED=False)
def test_disabled_quality_metrics_are_unavailable_without_querying_quality_data(metric_scope):
    _owner, _outsider, project, foreign, client = metric_scope
    profile(project)
    profile(foreign)
    quality_run(project, 'owned-quality')
    quality_run(foreign, 'foreign-quality')
    tables = [model._meta.db_table for model in (
        QualityTestProfile, QualityRun, QualityEvidence, QualityFailure, QualityFailureCluster,
    )]
    with CaptureQueriesContext(connection) as queries:
        response = client.get('/api/executions/metrics/', {'project': project.pk})
    assert response.status_code == 200
    assert response.data['quality_intelligence_enabled'] is False
    assert response.data['total_jobs'] == 1
    assert response.data['flaky_profiles'] == 0
    assert all(response.data['scale'][key] == 0 for key in (
        'quality_runs', 'quality_evidence', 'quality_failures',
        'quality_failure_clusters', 'unmapped_quality_runs', 'unmapped_quality_clusters',
    ))
    assert not any(table in query['sql'] for table in tables for query in queries)


@override_settings(QUALITY_INTELLIGENCE_ENABLED=True)
@pytest.mark.parametrize('filtered', [False, True])
def test_enabled_metrics_preserve_exact_project_identity_and_legacy_namespaces(metric_scope, filtered):
    _owner, _outsider, project, foreign, client = metric_scope
    profile(project)
    profile(foreign)
    profile(project, domain='API', key='same-number-native')
    profile(project, domain='EXECUTION', key='legacy-namespace')
    profile(project, key='noncanonical-number', project_id='0'+str(project.pk))
    quality_run(project, 'owned-quality')
    quality_run(foreign, 'foreign-quality')
    response = client.get('/api/executions/metrics/', {'project': project.pk} if filtered else {})
    assert response.status_code == 200, response.data
    assert response.data['quality_intelligence_enabled'] is True
    assert response.data['flaky_profiles'] == 1
    assert response.data['scale']['quality_runs'] == 1
    assert response.data['total_jobs'] == 1
    assert all(row['project'] == project.pk for row in response.data['recent_runs'])


@override_settings(QUALITY_INTELLIGENCE_ENABLED=True)
def test_metrics_empty_and_foreign_scopes_do_not_disclose_counts(metric_scope):
    owner, _outsider, _project, foreign, client = metric_scope
    profile(foreign)
    quality_run(foreign, 'foreign-quality')
    response = client.get('/api/executions/metrics/', {'project': foreign.pk})
    assert response.status_code == 200
    assert response.data['total_jobs'] == response.data['flaky_profiles'] == 0
    assert response.data['scale']['quality_runs'] == 0
    assert response.data['recent_runs'] == []
    no_project = Project.objects.create(name='Empty metrics scope', owner=owner)
    response = client.get('/api/executions/metrics/', {'project': no_project.pk})
    assert response.status_code == 200
    assert response.data['total_jobs'] == response.data['flaky_profiles'] == 0
    anonymous = APIClient().get('/api/executions/metrics/')
    assert anonymous.status_code == 401


@override_settings(QUALITY_INTELLIGENCE_ENABLED=True)
def test_inaccessible_project_does_not_report_other_accessible_quality(metric_scope):
    _owner, _outsider, project, foreign, client = metric_scope
    quality_run(project, 'owned-quality-not-in-requested-scope')
    response = client.get('/api/executions/metrics/', {'project': foreign.pk})
    assert response.status_code == 200
    assert response.data['quality_intelligence_enabled'] is True
    assert all(response.data['scale'][key] == 0 for key in (
        'quality_runs', 'quality_evidence', 'quality_failures',
        'quality_failure_clusters', 'unmapped_quality_runs', 'unmapped_quality_clusters',
    ))


@pytest.mark.django_db(transaction=True)
@override_settings(QUALITY_INTELLIGENCE_ENABLED=True)
def test_real_mysql_mixed_collation_metrics_regression():
    if connection.vendor != 'mysql':
        pytest.skip('Requires the disposable real MySQL test database, not a mocked vendor')
    if connection.mysql_version < (8, 0):
        pytest.skip('The regression uses MySQL8 utf8mb4_0900_ai_ci')
    database = str(connection.settings_dict['NAME'])
    assert database.startswith(('test_', 'testhub_ui_hotfix_')), 'Never alter live staging tables'
    table = QualityTestProfile._meta.db_table
    field = QualityTestProfile._meta.get_field('project_id')
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute('SELECT @@character_set_connection, @@collation_connection')
        original_charset, original_connection_collation = cursor.fetchone()
        cursor.execute(
            'SELECT CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS '
            'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s',
            [table, field.column],
        )
        original_field_charset, original_field_collation = cursor.fetchone()
    for name in [original_charset, original_connection_collation,
                 original_field_charset, original_field_collation]:
        assert re.fullmatch(r'[A-Za-z0-9_]+', name)
    nullability = 'NULL' if field.null else 'NOT NULL'
    definition = field.db_type(connection)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET NAMES utf8mb4 COLLATE utf8mb4_0900_ai_ci')
            cursor.execute(
                f'ALTER TABLE {quote(table)} MODIFY {quote(field.column)} {definition} '
                f'CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci {nullability}'
            )
        owner = User.objects.create_user(username='mysql-metrics-owner', password='fixture-only')
        project = Project.objects.create(name='MySQL actual collation regression', owner=owner)
        profile(project)
        legacy_query = QualityTestProfile.objects.filter(
            project_type='PROJECT', reliability_status='FLAKY',
            project_id__in=Project.objects.filter(pk=project.pk).annotate(
                scope_id=Cast('pk', CharField()),
            ).values('scope_id'),
        )
        with pytest.raises(OperationalError) as legacy_error:
            legacy_query.count()
        assert legacy_error.value.args[0] == 1267
        client = APIClient()
        client.force_authenticate(owner)
        for params in [{}, {'project': project.pk}]:
            response = client.get('/api/executions/metrics/', params)
            assert response.status_code == 200, response.data
            assert response.data['quality_intelligence_enabled'] is True
            assert response.data['flaky_profiles'] == 1
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                f'ALTER TABLE {quote(table)} MODIFY {quote(field.column)} {definition} '
                f'CHARACTER SET {original_field_charset} COLLATE {original_field_collation} {nullability}'
            )
            cursor.execute(f'SET NAMES {original_charset} COLLATE {original_connection_collation}')
