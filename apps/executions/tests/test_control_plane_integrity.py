from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.executions.models import (
    ExecutionArtifact, ExecutionAttempt, ExecutionFailureReview, ExecutionJob,
    ExecutionJobVariant, ExecutionQualityGateEvaluation, ExecutionSourceBinding,
    ExecutionStepResult, RequirementTestCaseLink,
)
from apps.executions.policies import preserve_final_result
from apps.projects.models import Project
from apps.quality_intelligence.models import QualityEvidence, QualityRun, QualityTestProfile
from apps.requirement_analysis.models import BusinessRequirement, RequirementAnalysis, RequirementDocument
from apps.testcases.models import TestCase as CaseModel
from apps.users.models import User
from apps.versions.models import Version

pytestmark = pytest.mark.django_db


@pytest.fixture
def control():
    owner = User.objects.create_user(username='control-integrity', password='pass')
    project = Project.objects.create(name='Control integrity', owner=owner)
    version = Version.objects.create(name='integrity-v1', created_by=owner)
    version.projects.add(project)
    client = APIClient()
    client.force_authenticate(user=owner)
    document = RequirementDocument.objects.create(
        project=project, title='Requirements', document_type='txt', uploaded_by=owner,
    )
    analysis = RequirementAnalysis.objects.create(document=document)
    requirement = BusinessRequirement.objects.create(
        analysis=analysis, requirement_id='REQ-1', requirement_name='Complete evidence',
        requirement_type='functional', requirement_level='high', module='core',
        description='All linked cases must pass', acceptance_criteria='all final evidence',
    )
    cases = [CaseModel.objects.create(
        project=project, title=f'Case {index}', author=owner,
    ) for index in range(2)]
    for case in cases:
        case.versions.add(version)
        RequirementTestCaseLink.objects.create(requirement=requirement, test_case=case)
    return locals()


def _final_job(control, steps, *, version=None, finalized=True):
    outcome = 'failed' if any(value in {'failed', 'blocked', 'error'} for _key, value in steps) else 'passed'
    job = ExecutionJob.objects.create(
        project=control['project'], requested_by=control['owner'],
        source_type='api', version=version or control['version'],
        status=outcome if finalized else 'running',
    )
    attempt = ExecutionAttempt.objects.create(job=job, attempt_number=1, status=outcome)
    for index, (case_key, status) in enumerate(steps):
        ExecutionStepResult.objects.create(
            attempt=attempt, case_key=case_key, step_key=f'step-{index}', status=status,
        )
    if finalized:
        preserve_final_result(job, attempt, outcome, {})
    return job, attempt


def _coverage(control, **params):
    response = control['client'].get('/api/executions/builds/coverage/', {
        'project': control['project'].pk, **params,
    })
    assert response.status_code == 200, response.data
    return response.data


def test_coverage_missing_case_never_counts_as_all_passed(control):
    first, _second = control['cases']
    _final_job(control, [(first.external_ref, 'passed')])
    data = _coverage(control)
    assert data['linked_requirements'] == 1
    assert data['executed_requirements'] == 1
    assert data['passed_requirements'] == 0
    assert data['passed_requirement_ids'] == []


def test_coverage_aggregates_every_step_in_the_latest_final_attempt(control):
    first, second = control['cases']
    _final_job(control, [
        (first.external_ref, 'failed'), (first.external_ref, 'passed'),
        (second.external_ref, 'passed'),
    ])
    data = _coverage(control)
    assert data['passed_requirements'] == 0
    assert data['failed_requirement_ids'] == [control['requirement'].pk]
    _final_job(control, [(case.external_ref, 'passed') for case in control['cases']])
    assert _coverage(control)['passed_requirements'] == 1


def test_coverage_ignores_unfinalized_and_untrusted_attempts(control):
    steps = [(case.external_ref, 'passed') for case in control['cases']]
    job, attempt = _final_job(control, steps, finalized=False)
    attempt.is_final = True
    attempt.save(update_fields=['is_final'])
    assert _coverage(control)['executed_requirements'] == 0
    job.status = 'passed'
    job.save(update_fields=['status'])
    preserve_final_result(job, attempt, 'passed', {})
    # A late historical attempt cannot replace the immutable final attempt.
    newer = ExecutionAttempt.objects.create(job=job, attempt_number=2, status='failed', is_final=True)
    ExecutionStepResult.objects.create(
        attempt=newer, case_key=control['cases'][0].external_ref, step_key='late', status='failed',
    )
    data = _coverage(control)
    assert data['passed_requirements'] == 1
    assert data['failed_requirements'] == 0


def test_coverage_scopes_case_links_and_final_results_to_the_selected_version(control):
    first, second = control['cases']
    other = Version.objects.create(name='integrity-v2', created_by=control['owner'])
    other.projects.add(control['project'])
    second.versions.set([other])
    _final_job(control, [(first.external_ref, 'passed'), (second.external_ref, 'passed')], version=other)
    assert _coverage(control, version=control['version'].pk)['passed_requirements'] == 0
    assert _coverage(control, version=other.pk)['passed_requirements'] == 1
    _final_job(control, [(first.external_ref, 'passed')])
    data = _coverage(control, version=control['version'].pk)
    assert data['total_requirements'] == 1
    assert data['linked_requirements'] == 1
    assert data['passed_requirements'] == 1


@pytest.mark.parametrize('second_status', ['passed', 'failed', 'missing'])
def test_matrix_coverage_uses_every_current_variant_and_requires_evidence_for_each(control, second_status):
    outcome = 'failed' if second_status == 'failed' else 'passed'
    job = ExecutionJob.objects.create(project=control['project'], version=control['version'], status=outcome)
    attempts = []
    for index in range(2):
        variant = ExecutionJobVariant.objects.create(job=job, variant_key=str(index), ordinal=index)
        attempt = ExecutionAttempt.objects.create(
            job=job, variant=variant, attempt_number=index + 1, status='passed',
        )
        attempts.append(attempt)
        for case_index, case in enumerate(control['cases']):
            if index == 1 and case_index == 0 and second_status == 'missing':
                continue
            ExecutionStepResult.objects.create(
                attempt=attempt, case_key=case.external_ref, step_key='case',
                status=second_status if index == 1 and case_index == 0 else 'passed',
            )
    preserve_final_result(job, attempts[-1], outcome, {})
    assert not ExecutionAttempt.objects.get(pk=attempts[0].pk).is_final
    data = _coverage(control)
    assert data['passed_requirements'] == int(second_status == 'passed')
    assert data['failed_requirements'] == int(second_status == 'failed')


def test_failure_analysis_get_performs_no_cluster_projection_or_database_write(control):
    job, _ = _final_job(control, [(control['cases'][0].external_ref, 'failed')])
    with (
        patch('apps.quality_intelligence.services.clusters.sync_execution_job_clusters') as sync,
        CaptureQueriesContext(connection) as queries,
    ):
        response = control['client'].get(f'/api/executions/jobs/{job.pk}/failure-analysis/')
    assert response.status_code == 200
    assert response.data['failure_count'] == 1
    sync.assert_not_called()
    assert not any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries)


def test_metrics_aggregate_without_loading_job_history_or_large_json(control):
    project = control['project']
    blob = 'unused-result-data' * 1000
    jobs = ExecutionJob.objects.bulk_create([
        ExecutionJob(
            project=project, source_type='ci', source_id=f'ci-{index}',
            result={'unmatched': index % 2, 'unused_blob': blob},
            environment_snapshot={'unused': blob}, final_result={'unused': blob},
        ) for index in range(240)
    ])
    for index in range(3):
        ExecutionQualityGateEvaluation.objects.create(job=jobs[index], stage='release', passed=index < 2)
        ExecutionFailureReview.objects.create(
            job=jobs[index], case_key=str(index), signature=str(index), category='PRODUCT',
        )
    for domain in ('PROJECT', 'API', 'EXECUTION'):
        QualityTestProfile.objects.create(
            source_type='API', project_type=domain, project_id=str(project.pk),
            test_key='flaky', reliability_status='FLAKY',
        )
    with (
        patch.object(ExecutionJob, 'from_db', wraps=ExecutionJob.from_db) as from_db,
        CaptureQueriesContext(connection) as queries,
    ):
        response = control['client'].get('/api/executions/metrics/', {'project': project.pk})
    assert response.status_code == 200, response.data
    data = response.data
    assert data['total_jobs'] == data['ci_jobs'] == 240
    assert data['unmatched_results'] == 120
    assert data['ci_association_rate'] == 50
    assert data['gate_evaluations'] == 3
    assert data['gate_pass_rate'] == 66.67
    assert data['failure_categories'] == {'PRODUCT': 3}
    assert data['flaky_profiles'] == 1
    assert len(data['recent_runs']) == 10
    assert from_db.call_count <= 11
    for call in from_db.call_args_list:
        assert not {'result', 'final_result', 'environment_snapshot'} & set(call.args[1])
    # Job scopes are DB subqueries rather than Python-expanded ID lists.
    assert all(len(query['sql']) < 30000 for query in queries)


def test_paginated_job_list_batches_attempt_counts_and_binding_lookups(control):
    project = control['project']
    ExecutionSourceBinding.objects.create(
        project=project, source_type='api', source_id='api:suite:42', source_project_id='native-api',
    )
    jobs = ExecutionJob.objects.bulk_create([
        ExecutionJob(project=project, source_type='api', source_id='api:suite:42')
        for _ in range(20)
    ])
    for job in jobs:
        ExecutionAttempt.objects.create(job=job, attempt_number=1)
    with override_settings(EXECUTION_DISPATCH_ROUTES={'api': 'apps.executions.tasks.execute_api_source'}):
        with CaptureQueriesContext(connection) as small:
            first = control['client'].get('/api/executions/jobs/', {'page_size': 1})
        with CaptureQueriesContext(connection) as large:
            page = control['client'].get('/api/executions/jobs/', {'page_size': 20})
    assert first.status_code == page.status_code == 200
    assert len(large) <= len(small) + 1
    assert len(page.data['results']) == 20
    assert all(row['attempts_count'] == 1 for row in page.data['results'])
    assert all(row['source_binding_status'] == 'ready' for row in page.data['results'])


def _quality_run(project, suffix='one', domain='PROJECT'):
    return QualityRun.objects.create(
        source_type='API', source_scope='CASE', source_execution_id=suffix,
        project_type=domain, project_id=str(project.pk), source_fingerprint=suffix,
    )


@override_settings(QUALITY_INTELLIGENCE_ENABLED=True)
def test_evidence_has_independent_stable_pages_real_totals_and_separate_type_filters(control):
    job = ExecutionJob.objects.create(project=control['project'])
    artifacts = [ExecutionArtifact.objects.create(
        job=job, uri=f'urn:testhub:artifact:{index}', media_type='image/png' if index < 3 else 'application/zip',
    ) for index in range(5)]
    run = _quality_run(control['project'])
    evidence = [QualityEvidence.objects.create(
        run=run, evidence_type='SCREENSHOT' if index < 3 else 'LOG', mime_type='text/plain',
    ) for index in range(5)]
    tied_time = timezone.now() - timedelta(minutes=1)
    ExecutionArtifact.objects.filter(job=job).update(created_at=tied_time)
    QualityEvidence.objects.filter(run=run).update(created_at=tied_time)
    params = {
        'project': control['project'].pk, 'media_type': 'image/png', 'evidence_type': 'LOG',
        'artifact_page': 2, 'artifact_page_size': 2, 'quality_page': 1, 'quality_page_size': 1,
    }
    response = control['client'].get('/api/executions/evidence-explorer/', params)
    assert response.status_code == 200, response.data
    data = response.data
    assert data['counts'] == {'execution_artifacts': 3, 'quality_evidence': 2}
    assert [row['id'] for row in data['execution_artifacts']] == [artifacts[0].pk]
    assert [row['id'] for row in data['quality_evidence']] == [evidence[-1].pk]
    assert data['pagination']['execution_artifacts'] == {
        'total': 3, 'page': 2, 'page_size': 2, 'has_next': False,
    }
    assert data['pagination']['quality_evidence'] == {
        'total': 2, 'page': 1, 'page_size': 1, 'has_next': True,
    }
    params['artifact_page'] = 1
    params['quality_page'] = 2
    page2 = control['client'].get('/api/executions/evidence-explorer/', params).data
    assert [row['id'] for row in page2['execution_artifacts']] == [artifacts[2].pk, artifacts[1].pk]
    assert [row['id'] for row in page2['quality_evidence']] == [evidence[-2].pk]
    assert not page2['pagination']['quality_evidence']['has_next']


@override_settings(QUALITY_INTELLIGENCE_ENABLED=True)
def test_evidence_project_selection_excludes_foreign_namespaces_and_conflicting_legacy_links(control):
    visible_job = ExecutionJob.objects.create(project=control['project'])
    outsider = User.objects.create_user(username='control-hidden', password='pass')
    hidden = Project.objects.create(name='Hidden', owner=outsider)
    hidden_job = ExecutionJob.objects.create(project=hidden)
    hidden_attempt = ExecutionAttempt.objects.create(job=hidden_job, attempt_number=1)
    ExecutionArtifact.objects.create(job=visible_job, attempt=hidden_attempt, uri='urn:testhub:conflicting')
    # Identical numeric project ID in another domain is not project-scoped evidence.
    foreign = _quality_run(control['project'], suffix='foreign', domain='EXECUTION')
    QualityEvidence.objects.create(run=foreign, evidence_type='LOG')
    response = control['client'].get('/api/executions/evidence-explorer/', {'project': control['project'].pk})
    assert response.status_code == 200
    assert response.data['counts'] == {'execution_artifacts': 0, 'quality_evidence': 0}


@pytest.mark.parametrize('path,param', [
    ('metrics', {'days': 'bad'}), ('metrics', {'days': 0}), ('metrics', {'days': 366}),
    ('metrics', {'project': 'not-an-id'}), ('metrics', {'project': 10 ** 100}),
    ('evidence-explorer', {'project': 'bad'}), ('evidence-explorer', {'job': 'bad'}),
    ('evidence-explorer', {'attempt': -1}), ('evidence-explorer', {'quality_run': '1.5'}),
    ('evidence-explorer', {'artifact_page': 'NaN'}), ('evidence-explorer', {'quality_page': 0}),
    ('evidence-explorer', {'page_size': 201}), ('evidence-explorer', {'limit': 'bad'}),
    ('builds/coverage', {'version': 'bad'}), ('builds/coverage', {'project': '1.5'}),
    ('jobs', {'page_size': 'bad'}), ('jobs', {'page_size': 0}),
])
def test_invalid_control_numeric_parameters_return_controlled_4xx(control, path, param):
    params = {'project': control['project'].pk, **param}
    response = control['client'].get(f'/api/executions/{path}/', params)
    assert response.status_code == 400, response.data


@pytest.mark.parametrize('action,payload', [
    ('compare', {'baseline_job_id': 'bad'}),
    ('release-gate', {'baseline_job_id': 'bad'}),
    ('release-gate', {'max_failed': -1}),
    ('release-gate', {'max_unmatched': True}),
])
def test_invalid_control_body_ids_and_thresholds_return_4xx(control, action, payload):
    job = ExecutionJob.objects.create(project=control['project'])
    response = control['client'].post(f'/api/executions/jobs/{job.pk}/{action}/', payload, format='json')
    assert response.status_code == 400, response.data
