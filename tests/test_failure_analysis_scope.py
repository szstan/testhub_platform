import pytest
from django.utils import timezone

from apps.executions.failure_intelligence import analyze_job_failures
from apps.executions.models import ExecutionAttempt, ExecutionJob, ExecutionJobVariant, ExecutionStepResult
from apps.projects.models import Project
from apps.quality_intelligence.models import QualityTestProfile
from apps.users.models import User

pytestmark = pytest.mark.django_db


def _failed_job(case_keys):
    user = User.objects.create_user(username='failure-scope-owner', password='pass')
    project = Project.objects.create(name='Failure scope project', owner=user)
    job = ExecutionJob.objects.create(
        project=project,
        source_type='api',
        source_id='api:scope-check',
        environment_snapshot={'environment_fingerprint': 'staging'},
        status='failed',
        final_status='failed',
        finalized_at=timezone.now(),
        requested_by=user,
    )
    attempt = ExecutionAttempt.objects.create(
        job=job,
        attempt_number=1,
        status='failed',
        is_final=True,
    )
    for case_key in case_keys:
        ExecutionStepResult.objects.create(
            attempt=attempt,
            case_key=case_key,
            step_key='test',
            status='failed',
            error_type='assertion',
            error_message='Assertion expected 200 actual 500',
        )
    job.final_attempt = attempt
    job.save(update_fields=['final_attempt'])
    return job, project


def _flaky_profile(project, case_key, *, project_type='PROJECT', source_type='API', environment='staging'):
    return QualityTestProfile.objects.create(
        source_type=source_type,
        project_type=project_type,
        project_id=str(project.pk),
        test_key=case_key,
        environment_fingerprint=environment,
        reliability_status='FLAKY',
    )


def test_flaky_cases_match_canonical_project_source_and_environment_scope():
    job, project = _failed_job(['foreign-domain', 'wrong-source', 'wrong-environment', 'exact-scope'])
    _flaky_profile(project, 'foreign-domain', project_type='API')
    _flaky_profile(project, 'wrong-source', source_type='UI')
    _flaky_profile(project, 'wrong-environment', environment='production')
    _flaky_profile(project, 'exact-scope')
    profile_count = QualityTestProfile.objects.count()

    result = analyze_job_failures(job)

    assert QualityTestProfile.objects.count() == profile_count
    assert result['flaky_cases'] == ['exact-scope']
    assert set(result) == {'job_id', 'failure_count', 'clusters', 'flaky_cases', 'classified'}


def test_failure_analysis_uses_only_a_trusted_final_attempt():
    user = User.objects.create_user(username='final-attempt-owner', password='pass')
    project = Project.objects.create(name='Final attempt project', owner=user)
    job = ExecutionJob.objects.create(
        project=project,
        source_type='api',
        status='failed',
        final_status='failed',
        finalized_at=timezone.now(),
        requested_by=user,
    )
    stale = ExecutionAttempt.objects.create(
        job=job,
        attempt_number=1,
        status='failed',
        is_final=False,
    )
    current = ExecutionAttempt.objects.create(
        job=job,
        attempt_number=2,
        status='failed',
        is_final=True,
    )
    ExecutionStepResult.objects.create(
        attempt=stale,
        case_key='stale-case',
        step_key='test',
        status='failed',
        error_message='stale failure',
    )
    ExecutionStepResult.objects.create(
        attempt=current,
        case_key='current-case',
        step_key='test',
        status='failed',
        error_message='current failure',
    )
    job.final_attempt = current
    job.save(update_fields=['final_attempt'])

    result = analyze_job_failures(job)
    assert result['failure_count'] == 1
    assert result['clusters'][0]['cases'] == ['current-case']

    job.final_status = 'passed'
    job.save(update_fields=['final_status'])
    untrusted = analyze_job_failures(job)
    assert untrusted['failure_count'] == 0
    assert untrusted['classified'] is False


def test_matrix_failure_analysis_uses_latest_terminal_attempt_per_variant():
    user = User.objects.create_user(username='matrix-failure-owner', password='pass')
    project = Project.objects.create(name='Matrix failure project', owner=user)
    job = ExecutionJob.objects.create(
        project=project,
        source_type='api',
        status='failed',
        final_status='failed',
        finalized_at=timezone.now(),
        requested_by=user,
    )
    first_variant = ExecutionJobVariant.objects.create(
        job=job, variant_key='first', ordinal=0,
    )
    last_variant = ExecutionJobVariant.objects.create(
        job=job, variant_key='last', ordinal=1,
    )
    historical_first = ExecutionAttempt.objects.create(
        job=job, variant=first_variant, attempt_number=1, status='failed', is_final=False,
    )
    failed_first = ExecutionAttempt.objects.create(
        job=job, variant=first_variant, attempt_number=2, status='failed', is_final=False,
    )
    historical_last = ExecutionAttempt.objects.create(
        job=job, variant=last_variant, attempt_number=3, status='failed', is_final=False,
    )
    passed_representative = ExecutionAttempt.objects.create(
        job=job, variant=last_variant, attempt_number=4, status='passed', is_final=True,
    )
    for attempt, case_key, status, message in (
        (historical_first, 'historical-first', 'failed', 'historical first failure'),
        (failed_first, 'failed-case', 'failed', 'current variant failure'),
        (historical_last, 'historical-last', 'failed', 'historical last failure'),
        (passed_representative, 'passed-case', 'passed', ''),
    ):
        ExecutionStepResult.objects.create(
            attempt=attempt,
            case_key=case_key,
            step_key='test',
            status=status,
            error_message=message,
        )
    job.final_attempt = passed_representative
    job.save(update_fields=['final_attempt'])

    result = analyze_job_failures(job)

    assert result['failure_count'] == 1
    assert result['clusters'][0]['cases'] == ['failed-case']
    assert result['classified'] is True
