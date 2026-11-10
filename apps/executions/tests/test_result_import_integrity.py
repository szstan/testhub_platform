import json

import pytest

from apps.executions.models import ExecutionStepResult
from apps.executions.result_import import ResultImportError, import_results, parse_allure, parse_junit
from apps.projects.models import Project
from apps.testcases.models import TestCase as TestCaseModel
from apps.users.models import User


@pytest.fixture
def import_context(db):
    user = User.objects.create_user(username='result-import-integrity-owner', password='pass')
    project = Project.objects.create(name='Result Import Integrity Project', owner=user)
    testcase = TestCaseModel.objects.create(
        project=project,
        external_ref='checkout.login',
        title='Login case',
        expected_result='success',
        author=user,
    )
    return user, project, testcase


@pytest.mark.django_db
def test_duplicate_external_refs_get_distinct_steps_and_replay_idempotently(import_context):
    user, project, testcase = import_context
    payload = b'''<testsuite>
      <testcase name="Login with valid credentials" time="1.25">
        <properties><property name="external_ref" value="checkout.login"/></properties>
        <failure message="assertion failed"/>
      </testcase>
      <testcase name="Login with locked account" time="2.5">
        <properties><property name="external_ref" value="checkout.login"/></properties>
      </testcase>
    </testsuite>'''

    summary = import_results(
        project=project,
        payload=payload,
        result_format='junit',
        external_run_id='duplicate-ref-run',
        requested_by=user,
    )
    steps = list(ExecutionStepResult.objects.filter(attempt__job=summary.job).order_by('step_key'))

    assert summary.total == 2
    assert summary.matched == 2
    assert summary.unmatched == 0
    assert summary.failed == 1
    assert summary.passed == 1
    assert [step.case_key for step in steps] == [testcase.external_ref, testcase.external_ref]
    assert [step.step_key for step in steps] == ['test', 'test:2']
    assert len({step.idempotency_key for step in steps}) == 2
    assert [(step.status, step.duration_ms) for step in steps] == [('failed', 1250), ('passed', 2500)]

    replay = import_results(
        project=project,
        payload=payload,
        result_format='junit',
        external_run_id='duplicate-ref-run',
        requested_by=user,
    )

    assert replay.created is False
    assert replay.failed == 1
    assert ExecutionStepResult.objects.filter(attempt__job=summary.job).count() == 2


@pytest.mark.parametrize('duration', ['NaN', 'Infinity', '-Infinity'])
def test_junit_rejects_non_finite_seconds(duration):
    payload = f'<testsuite><testcase name="case" time="{duration}"/></testsuite>'.encode()

    with pytest.raises(ResultImportError, match='JUnit testcase time'):
        parse_junit(payload)


def test_junit_duration_is_always_interpreted_as_seconds():
    parsed = parse_junit(b'<testsuite><testcase name="case" time="2.5"/></testsuite>')

    assert parsed[0].duration_ms == 2500


@pytest.mark.parametrize(
    ('start', 'stop'),
    [
        ('invalid', 100),
        (0, float('nan')),
        (100, float('inf')),
        (0, 86_400_001),
        (100, 99),
    ],
)
def test_allure_rejects_invalid_or_overflow_duration(start, stop):
    payload = json.dumps([{'name': 'case', 'start': start, 'stop': stop}]).encode()

    with pytest.raises(ResultImportError, match='Allure'):
        parse_allure(payload)


def test_allure_missing_duration_is_documented_as_none():
    parsed = parse_allure(b'[{"name":"case","start":100}]')

    assert parsed[0].duration_ms is None


@pytest.mark.django_db
def test_import_redacts_error_message_and_free_text_metadata(import_context):
    user, project, _testcase = import_context
    secret_text = 'private-token-92f1'
    private_url = 'https://private.example/path?token=private-token-92f1'
    payload = json.dumps([{
        'name': 'Login failure',
        'fullName': f'failure at {private_url}',
        'testCaseId': 'checkout.login',
        'status': 'failed',
        'statusDetails': {'message': f'request failed: {private_url}; token={secret_text}'},
        'start': 1000,
        'stop': 1250,
    }]).encode()

    summary = import_results(
        project=project,
        payload=payload,
        result_format='allure',
        external_run_id='redaction-run',
        requested_by=user,
    )
    step = ExecutionStepResult.objects.get(attempt__job=summary.job)
    stored_text = f"{step.error_message} {step.result['metadata']['full_name']}"

    assert secret_text not in stored_text
    assert private_url not in stored_text
    assert step.duration_ms == 250
