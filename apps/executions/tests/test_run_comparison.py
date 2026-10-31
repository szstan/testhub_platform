import pytest

from apps.executions.result_import import import_results
from apps.executions.run_comparison import compare_jobs, evaluate_release_gate
from apps.executions.models import (
    ExecutionAttempt,
    ExecutionJob,
    ExecutionQualityGateEvaluation,
    ExecutionStepResult,
)
from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.users.models import User


def _final_job(project, user, *, source_id, status, result):
    job = ExecutionJob.objects.create(
        project=project,
        source_type="manual",
        source_id=source_id,
        requested_by=user,
        status=status,
        final_status=status,
        final_result=result,
    )
    attempt = ExecutionAttempt.objects.create(
        job=job,
        attempt_number=1,
        status=status,
        result=result,
        is_final=True,
    )
    job.final_attempt = attempt
    job.save(update_fields=["final_attempt", "updated_at"])
    return job, attempt


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


@pytest.mark.django_db
def test_imported_final_snapshot_is_trusted_without_finalized_at_and_ignores_mutable_result():
    user = User.objects.create_user(username="legacy-import-owner", password="pass")
    project = Project.objects.create(name="Legacy Import Project", owner=user)
    TestCase.objects.create(
        project=project,
        external_ref="checkout.login",
        title="Login",
        expected_result="success",
        author=user,
    )
    imported = import_results(
        project=project,
        payload=b'<testsuite><testcase name="Login"><properties><property name="external_ref" value="checkout.login"/></properties></testcase></testsuite>',
        result_format="junit",
        external_run_id="legacy-final-snapshot",
        requested_by=user,
    )
    job = imported.job
    assert job.finalized_at is None
    assert job.final_attempt.is_final is True

    job.result = {"failed": 99, "unmatched": 99}
    job.save(update_fields=["result", "updated_at"])
    gate = evaluate_release_gate(job)

    assert gate["passed"] is True
    assert gate["summary"]["failed"] == 0
    assert gate["summary"]["unmatched"] == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("case_statuses", "expected_fixed"),
    [
        (None, False),
        (("skipped",), False),
        (("passed",), True),
        (("passed", "skipped"), False),
    ],
)
def test_missing_or_unexecuted_current_case_is_not_reported_fixed(case_statuses, expected_fixed):
    user = User.objects.create_user(username="fixed-case-owner", password="pass")
    project = Project.objects.create(name="Fixed Case Project", owner=user)
    baseline, baseline_attempt = _final_job(
        project,
        user,
        source_id="baseline-failed-case",
        status="failed",
        result={"failed": 1, "unmatched": 0},
    )
    ExecutionStepResult.objects.create(
        attempt=baseline_attempt,
        case_key="checkout.login",
        step_key="login",
        status="failed",
    )

    current_status = "completed" if case_statuses and "skipped" in case_statuses else "passed"
    current, current_attempt = _final_job(
        project,
        user,
        source_id="current-case",
        status=current_status,
        result={"failed": 0, "unmatched": 0},
    )
    for index, case_status in enumerate(case_statuses or ()):
        ExecutionStepResult.objects.create(
            attempt=current_attempt,
            case_key="checkout.login",
            step_key=f"step-{index}",
            status=case_status,
        )

    diff = compare_jobs(current, baseline)
    assert ("checkout.login" in diff["fixed_failures"]) is expected_fixed
    if case_statuses is None:
        assert diff["removed_cases"] == ["checkout.login"]
    elif "skipped" in case_statuses:
        assert "checkout.login" in diff["unexecuted"]


@pytest.mark.django_db
def test_comparison_ignores_nonterminal_attempt_marked_final():
    user = User.objects.create_user(username="nonterminal-diff-owner", password="pass")
    project = Project.objects.create(name="Nonterminal Diff Project", owner=user)
    baseline, baseline_attempt = _final_job(
        project,
        user,
        source_id="failed-baseline",
        status="failed",
        result={"failed": 1, "unmatched": 0},
    )
    ExecutionStepResult.objects.create(
        attempt=baseline_attempt,
        case_key="checkout.login",
        step_key="login",
        status="failed",
    )
    current = ExecutionJob.objects.create(
        project=project,
        source_type="manual",
        source_id="running-current",
        requested_by=user,
        status="running",
        final_status="running",
    )
    attempt = ExecutionAttempt.objects.create(
        job=current,
        attempt_number=1,
        status="running",
        is_final=True,
    )
    current.final_attempt = attempt
    current.save(update_fields=["final_attempt", "updated_at"])
    ExecutionStepResult.objects.create(
        attempt=attempt,
        case_key="checkout.login",
        step_key="login",
        status="passed",
    )

    diff = compare_jobs(current, baseline)
    assert diff["fixed_failures"] == []
    assert diff["removed_cases"] == ["checkout.login"]
