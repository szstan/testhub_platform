import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.executions.models import ExecutionArtifact, ExecutionJob, ExecutionStepResult
from apps.executions.result_import import import_results, parse_allure, parse_junit
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
