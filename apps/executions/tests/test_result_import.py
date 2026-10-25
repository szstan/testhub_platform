import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.executions.models import ExecutionArtifact, ExecutionJob, ExecutionStepResult
from apps.executions.result_import import ResultImportError, import_results, parse_allure, parse_junit
from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.users.models import User


@pytest.fixture
def execution_fixture(db):
    user = User.objects.create_user(username='result-import-owner', password='pass')
    project = Project.objects.create(name='Result Import Project', owner=user)
    testcase = TestCase.objects.create(
        project=project,
        external_ref='checkout.login',
        title='Login case',
        expected_result='success',
        author=user,
    )
    return user, project, testcase


def test_junit_and_allure_parsers_extract_stable_keys():
    junit = b'''<testsuite><testcase classname="Checkout" name="Login" time="0.2"><properties><property name="external_ref" value="checkout.login"/></properties></testcase><testcase name="Skipped"><skipped/></testcase></testsuite>'''
    parsed_junit = parse_junit(junit)
    assert parsed_junit[0].case_key == 'checkout.login'
    assert parsed_junit[0].status == 'passed'
    assert parsed_junit[1].status == 'skipped'

    parsed_allure = parse_allure(json.dumps([{
        'name': 'Login',
        'fullName': 'Checkout Login',
        'testCaseId': 'checkout.login',
        'status': 'failed',
        'statusDetails': {'message': 'assertion failed'},
        'start': 1000,
        'stop': 1125,
    }]).encode())
    assert parsed_allure[0].case_key == 'checkout.login'
    assert parsed_allure[0].status == 'failed'
    assert parsed_allure[0].duration_ms == 125


@pytest.mark.django_db
def test_result_import_is_idempotent_and_links_external_ref(execution_fixture):
    user, project, testcase = execution_fixture
    payload = b'''<testsuite><testcase classname="Checkout" name="Login" time="0.2"><properties><property name="external_ref" value="checkout.login"/></properties></testcase><testcase name="Missing"/></testsuite>'''

    first = import_results(
        project=project,
        payload=payload,
        result_format='junit',
        external_run_id='run-001',
        requested_by=user,
        build_reference='build-001',
        filename='results.xml',
    )
    assert first.created is True
    assert first.total == 2
    assert first.matched == 1
    assert first.unmatched == 1
    assert first.passed == 2
    assert ExecutionJob.objects.count() == 1
    assert ExecutionArtifact.objects.count() == 1
    assert ExecutionStepResult.objects.filter(case_key=testcase.external_ref, status='passed').exists()

    replay = import_results(
        project=project,
        payload=payload,
        result_format='junit',
        external_run_id='run-001',
        requested_by=user,
        build_reference='build-001',
        filename='results.xml',
    )
    assert replay.created is False
    assert ExecutionJob.objects.count() == 1
    assert ExecutionStepResult.objects.count() == 2


@pytest.mark.django_db
def test_result_import_hashes_long_idempotency_keys(execution_fixture):
    user, project, _ = execution_fixture
    classname = 'C' * 180
    name = 'N' * 180
    payload = f'<testsuite><testcase classname="{classname}" name="{name}" /></testsuite>'.encode()

    summary = import_results(
        project=project,
        payload=payload,
        result_format='junit',
        external_run_id='github-' + ('r' * 180),
        requested_by=user,
    )

    assert summary.created is True
    step = ExecutionStepResult.objects.get(attempt__job_id=summary.job.id)
    assert len(step.idempotency_key) <= 128
    assert step.idempotency_key.startswith('result-step:')


@pytest.mark.django_db
def test_result_import_rejects_changed_payload_and_reuses_legacy_key(execution_fixture):
    user, project, _ = execution_fixture
    first_payload = b'<testsuite><testcase name="one"/></testsuite>'
    changed_payload = b'<testsuite><testcase name="two"/></testsuite>'
    first = import_results(
        project=project,
        payload=first_payload,
        result_format='junit',
        external_run_id='payload-run',
        requested_by=user,
    )
    with pytest.raises(ResultImportError):
        import_results(
            project=project,
            payload=changed_payload,
            result_format='junit',
            external_run_id='payload-run',
            requested_by=user,
        )

    legacy_key = 'result-import:junit:legacy-run'
    legacy = ExecutionJob.objects.create(
        project=project,
        idempotency_key=legacy_key,
        source_type='ci',
        source_id='legacy-run',
        requested_by=user,
        result={'total': 1, 'passed': 1, 'failed': 0, 'skipped': 0, 'matched': 0, 'unmatched': 1},
    )
    replay = import_results(
        project=project,
        payload=first_payload,
        result_format='junit',
        external_run_id='legacy-run',
        requested_by=user,
    )
    assert replay.created is False
    assert replay.job.id == legacy.id


@pytest.mark.django_db
@pytest.mark.django_db
def test_execution_metrics_reports_ci_association_and_gate_rate(execution_fixture):
    user, project, _ = execution_fixture
    payload = b'<testsuite><testcase name="one"/></testsuite>'
    import_results(
        project=project,
        payload=payload,
        result_format='junit',
        external_run_id='metrics-run',
        requested_by=user,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get('/api/executions/metrics/', {'project': project.id})
    assert response.status_code == 200
    assert response.data['ci_jobs'] == 1
    assert response.data['ci_association_rate'] == 0
    assert response.data['unmatched_results'] == 1
    assert response.data['total_jobs'] == 1


def test_result_import_api_requires_project_and_file(execution_fixture):
    user, project, _ = execution_fixture
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post('/api/executions/import-results/', {
        'project_id': project.id,
        'format': 'junit',
        'external_run_id': 'run-api-001',
        'file': SimpleUploadedFile('results.xml', b'<testsuite><testcase name="one"/></testsuite>', content_type='application/xml'),
    }, format='multipart')
    assert response.status_code == 201, response.data
    assert response.data['total'] == 1
    assert response.data['created'] is True
    status_response = client.get(f"/api/executions/jobs/{response.data['job_id']}/ci-status/")
    assert status_response.status_code == 200
    assert status_response.data['terminal'] is True
    assert status_response.data['exit_code'] == 0
