import pytest

from apps.executions.result_import import import_results
from apps.executions.run_comparison import compare_jobs, evaluate_release_gate
from apps.executions.models import ExecutionQualityGateEvaluation
from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.users.models import User


@pytest.mark.django_db
def test_run_diff_and_release_gate_detect_new_failure():
    user = User.objects.create_user(username='gate-owner', password='pass')
    project = Project.objects.create(name='Gate Project', owner=user)
    TestCase.objects.create(
        project=project,
        external_ref='checkout.login',
        title='Login',
        expected_result='success',
        author=user,
    )
    baseline = import_results(
        project=project,
        payload=b'<testsuite><testcase name="Login"><properties><property name="external_ref" value="checkout.login"/></properties></testcase></testsuite>',
        result_format='junit',
        external_run_id='baseline-1',
        requested_by=user,
    )
    current = import_results(
        project=project,
        payload=b'<testsuite><testcase name="Login"><properties><property name="external_ref" value="checkout.login"/></properties><failure message="broken"/></testcase></testsuite>',
        result_format='junit',
        external_run_id='current-1',
        requested_by=user,
    )

    diff = compare_jobs(current.job, baseline.job)
    assert diff['new_failures'] == ['checkout.login']
    assert diff['fixed_failures'] == []

    gate = evaluate_release_gate(current.job, baseline=baseline.job, evaluated_by=user)
    assert gate['passed'] is False
    assert gate['exit_code'] == 1
    assert gate['policy_version'].startswith('gate-')
    assert gate['policy_snapshot']['thresholds']['max_failed'] == 0
    assert any(item['metric'] == 'failed' for item in gate['failures'])
    assert ExecutionQualityGateEvaluation.objects.filter(job=current.job, stage='release', passed=False, policy_version=gate['policy_version']).exists()
