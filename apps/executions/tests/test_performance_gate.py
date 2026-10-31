import pytest

from apps.executions.models import ExecutionAttempt, ExecutionJob, ExecutionProfile
from apps.executions.run_comparison import evaluate_release_gate
from apps.projects.models import Project
from apps.users.models import User


def _final_job(project, user, *, source_id, result, status="passed", profile=None):
    job = ExecutionJob.objects.create(
        project=project,
        profile=profile,
        source_type="performance",
        source_id=source_id,
        requested_by=user,
        status=status,
        final_status=status,
        result=result,
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
    return job


@pytest.mark.django_db
def test_release_gate_fails_closed_on_metric_threshold_and_baseline_delta():
    user = User.objects.create_user(username="metric-gate-owner", password="pass")
    project = Project.objects.create(name="Metric Gate Project", owner=user)
    baseline = _final_job(
        project,
        user,
        source_id="performance:scenario:1:baseline",
        result={"failed": 0, "unmatched": 0, "metrics": {"p95": 100, "error_rate": 1}},
    )
    current = _final_job(
        project,
        user,
        source_id="performance:scenario:1:current",
        result={"failed": 0, "unmatched": 0, "metrics": {"p95": 130, "error_rate": 2}},
    )

    gate = evaluate_release_gate(
        current,
        baseline=baseline,
        metric_thresholds={
            "p95": {"max": 120, "max_delta_pct": 20},
            "error_rate": {"max": 1},
        },
        evaluated_by=user,
    )
    assert gate["passed"] is False
    assert any(item["metric"] == "metric:p95" for item in gate["failures"])
    assert any(item["metric"] == "metric:error_rate" for item in gate["failures"])


@pytest.mark.django_db
def test_release_gate_missing_metric_fails_closed():
    user = User.objects.create_user(username="missing-metric-owner", password="pass")
    project = Project.objects.create(name="Missing Metric Project", owner=user)
    job = _final_job(
        project,
        user,
        source_id="performance:scenario:2",
        result={"failed": 0, "unmatched": 0, "metrics": {}},
    )
    gate = evaluate_release_gate(
        job,
        metric_thresholds={"p99": {"max": 500}},
        evaluated_by=user,
    )
    assert gate["passed"] is False
    assert {"metric": "metric:p99", "reason": "missing_metric"} in gate["failures"]


@pytest.mark.django_db
def test_release_gate_rejects_non_finite_actual_metric():
    user = User.objects.create_user(username="nonfinite-actual-owner", password="pass")
    project = Project.objects.create(name="Nonfinite Actual Project", owner=user)
    job = _final_job(
        project,
        user,
        source_id="performance:nonfinite-actual",
        result={"failed": 0, "unmatched": 0, "metrics": {"latency": 100}},
    )
    job.final_result = {"failed": 0, "unmatched": 0, "metrics": {"latency": float("inf")}}
    gate = evaluate_release_gate(job, metric_thresholds={"latency": {"max": 100}})

    assert gate["passed"] is False
    assert {"metric": "metric:latency", "reason": "invalid_metric"} in gate["failures"]
    assert gate["summary"]["metrics"]["latency"] == "Infinity"


@pytest.mark.django_db
def test_release_gate_zero_baseline_change_fails_delta_rule():
    user = User.objects.create_user(username="zero-baseline-owner", password="pass")
    project = Project.objects.create(name="Zero Baseline Project", owner=user)
    baseline = _final_job(
        project,
        user,
        source_id="performance:zero:baseline",
        result={"failed": 0, "unmatched": 0, "metrics": {"error_rate": 0}},
    )
    current = _final_job(
        project,
        user,
        source_id="performance:zero:current",
        result={"failed": 0, "unmatched": 0, "metrics": {"error_rate": 50}},
    )
    gate = evaluate_release_gate(
        current,
        baseline=baseline,
        metric_thresholds={"error_rate": {"max_delta_pct": 5}},
    )
    assert gate["passed"] is False
    assert {
        "metric": "metric:error_rate",
        "reason": "undefined_zero_baseline_delta",
    } in gate["failures"]


@pytest.mark.django_db
def test_release_policy_merges_profile_and_request_rules_strictly():
    user = User.objects.create_user(username="strict-policy-owner", password="pass")
    project = Project.objects.create(name="Strict Policy Project", owner=user)
    profile = ExecutionProfile.objects.create(
        project=project,
        name="strict-profile",
        quality_gate={"pass_rate": {"min": 95}, "p95": {"max": 200}},
        created_by=user,
    )
    job = _final_job(
        project,
        user,
        source_id="performance:strict:current",
        profile=profile,
        result={"failed": 0, "unmatched": 0, "metrics": {"pass_rate": 94, "p95": 90}},
    )
    gate = evaluate_release_gate(
        job,
        max_failed=2,
        max_unmatched=3,
        metric_thresholds={"pass_rate": {"min": 90}, "p95": {"max": 100}},
    )
    assert gate["passed"] is False
    assert {"min": 95.0} == {
        "min": gate["effective_rules"]["pass_rate"]["min"]
    }
    assert gate["effective_rules"]["p95"]["max"] == 100.0
    assert gate["policy_snapshot"]["effective_rules"] == gate["effective_rules"]
    assert any(item["metric"] == "metric:pass_rate" and item.get("min") == 95.0 for item in gate["failures"])


@pytest.mark.django_db
def test_release_policy_rejects_contradictory_profile_and_request_rules():
    user = User.objects.create_user(username="conflict-policy-owner", password="pass")
    project = Project.objects.create(name="Conflicting Policy Project", owner=user)
    profile = ExecutionProfile.objects.create(
        project=project,
        name="conflicting-profile",
        quality_gate={"pass_rate": {"min": 95}},
        created_by=user,
    )
    job = _final_job(
        project,
        user,
        source_id="performance:conflict:current",
        profile=profile,
        result={"failed": 0, "unmatched": 0, "metrics": {"pass_rate": 100}},
    )
    gate = evaluate_release_gate(job, metric_thresholds={"pass_rate": {"max": 90}})
    assert gate["passed"] is False
    assert any(item["metric"] == "__policy__" and item["reason"] == "invalid_policy" for item in gate["failures"])


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["pending", "running", "failed", "cancelled", "blocked"])
def test_release_gate_requires_successful_terminal_final_evidence(status):
    user = User.objects.create_user(username=f"evidence-{status}-owner", password="pass")
    project = Project.objects.create(name=f"Evidence {status} Project", owner=user)
    result = {"failed": 0, "unmatched": 0}
    job = ExecutionJob.objects.create(
        project=project,
        source_type="performance",
        source_id=f"performance:evidence:{status}",
        requested_by=user,
        status=status,
        final_status=status if status in {"failed", "cancelled", "blocked"} else "",
        result={},
        final_result=result if status in {"failed", "cancelled", "blocked"} else {},
    )
    if status in {"failed", "cancelled", "blocked"}:
        attempt = ExecutionAttempt.objects.create(
            job=job,
            attempt_number=1,
            status=status,
            result=result,
            is_final=True,
        )
        job.final_attempt = attempt
        job.save(update_fields=["final_attempt", "updated_at"])

    gate = evaluate_release_gate(job)
    assert gate["passed"] is False
    assert gate["exit_code"] == 1
    assert any(item["metric"] == "final_evidence" for item in gate["failures"])


@pytest.mark.django_db
def test_release_gate_requires_explicit_failure_and_unmatched_counts():
    user = User.objects.create_user(username="missing-count-owner", password="pass")
    project = Project.objects.create(name="Missing Count Project", owner=user)
    job = _final_job(
        project,
        user,
        source_id="performance:missing-counts",
        result={"unmatched": 0},
    )
    gate = evaluate_release_gate(job)
    assert gate["passed"] is False
    assert {"metric": "failed", "reason": "missing_metric"} in gate["failures"]
