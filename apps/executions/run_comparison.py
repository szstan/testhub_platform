"""Deterministic run comparison and release quality-gate evaluation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from django.db import transaction
from django.db.models import OuterRef, Subquery

from .gate_policy import (
    build_effective_policy,
    evaluate_metric_rules,
    finite_json_value,
    policy_version_for_snapshot,
)
from .models import ExecutionAttempt, ExecutionJob, ExecutionQualityGateEvaluation, ExecutionStepResult
from .result_events import redact_result_payload


TERMINAL_STATUSES = {"passed", "succeeded", "completed", "failed", "blocked", "cancelled", "unsupported"}
SUCCESS_STATUSES = {"passed", "succeeded", "completed"}
BASELINE_STATUSES = SUCCESS_STATUSES | {"failed"}
TERMINAL_FAILURES = {"failed", "blocked"}
UNEXECUTED_STATUSES = {"pending", "running", "skipped", "blocked", "cancelled", "unsupported"}


def _status_family(value: Any) -> str:
    status = str(value or "").strip().lower()
    return "success" if status in SUCCESS_STATUSES else status


def _is_matrix_job(job: ExecutionJob) -> bool:
    return bool(
        getattr(job, "dataset_id", None)
        or getattr(job, "dataset_snapshot", None)
        or job.variants.exists()
    )


def _matrix_aggregate_status(attempts: list[ExecutionAttempt]) -> str:
    statuses = {str(attempt.status or "").strip().lower() for attempt in attempts}
    if "blocked" in statuses:
        return "blocked"
    if "failed" in statuses:
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    if "unsupported" in statuses:
        return "unsupported"
    return "passed"


def _has_failed_matrix_gate_proof(
    job: ExecutionJob,
    representative: ExecutionAttempt,
    final_result: Mapping[str, Any],
) -> bool:
    gate_result = getattr(job, "quality_gate_result", None)
    if (
        not getattr(job, "profile_id", None)
        or not isinstance(gate_result, Mapping)
        or gate_result.get("configured") is not True
        or gate_result.get("passed") is not False
        or not isinstance(gate_result.get("failures"), list)
        or not gate_result["failures"]
        or not isinstance(gate_result.get("summary"), Mapping)
        or dict(gate_result["summary"]) != dict(final_result)
    ):
        return False

    policy_snapshot = gate_result.get("policy_snapshot")
    policy_version = str(gate_result.get("policy_version") or "").strip()
    if (
        not isinstance(policy_snapshot, Mapping)
        or policy_snapshot.get("schema") != "quality-gate/v1"
        or policy_snapshot.get("profile_id") != job.profile_id
        or policy_snapshot.get("policy_version") != policy_version
        or not policy_version
    ):
        return False

    evaluations = job.quality_gate_evaluations.filter(
        profile_id=job.profile_id,
        attempt_id=representative.pk,
        stage="result",
        passed=False,
        policy_version=policy_version,
    ).only("summary", "failures", "policy_snapshot")
    return any(
        evaluation.summary == dict(final_result)
        and evaluation.failures == gate_result["failures"]
        and evaluation.policy_snapshot == dict(policy_snapshot)
        for evaluation in evaluations
    )


def _final_attempts(job: ExecutionJob) -> list[ExecutionAttempt] | None:
    status = str(getattr(job, "final_status", "") or "").strip().lower()
    job_status = str(getattr(job, "status", "") or "").strip().lower()
    if status not in TERMINAL_STATUSES or status != job_status:
        return None

    if _is_matrix_job(job):
        variants = list(
            job.variants.only("id", "job_id", "ordinal", "variant_key", "status", "result", "error_type", "error_message")
            .order_by("ordinal", "pk")
        )
        dataset_snapshot = getattr(job, "dataset_snapshot", None)
        final_result = getattr(job, "final_result", None)
        snapshot_variants = final_result.get("variants") if isinstance(final_result, Mapping) else None
        if (
            not variants
            or not isinstance(dataset_snapshot, Mapping)
            or dataset_snapshot.get("variant_count") != len(variants)
            or not isinstance(final_result, Mapping)
            or final_result.get("count") != len(variants)
            or not isinstance(snapshot_variants, list)
            or len(snapshot_variants) != len(variants)
        ):
            return None

        latest_attempt_ids = job.variants.order_by().annotate(
            latest_attempt_id=Subquery(
                ExecutionAttempt.objects.filter(job_id=job.pk, variant_id=OuterRef("pk"))
                .order_by("-attempt_number", "-pk")
                .values("pk")[:1]
            )
        ).values_list("latest_attempt_id", flat=True)
        latest_by_variant = {
            attempt.variant_id: attempt
            for attempt in ExecutionAttempt.objects.filter(job_id=job.pk, pk__in=latest_attempt_ids)
            .only("id", "job_id", "variant_id", "status", "result", "error_type", "error_message", "is_final")
            .order_by("variant_id")
        }
        variant_ids = {variant.pk for variant in variants}
        if set(latest_by_variant) != variant_ids:
            return None

        current = []
        for variant, snapshot in zip(variants, snapshot_variants):
            attempt = latest_by_variant[variant.pk]
            attempt_status = str(attempt.status or "").strip().lower()
            if (
                attempt.job_id != job.pk
                or attempt.variant_id != variant.pk
                or attempt_status not in TERMINAL_STATUSES
                or attempt_status != str(variant.status or "").strip().lower()
                or attempt.result != variant.result
                or attempt.error_type != variant.error_type
                or attempt.error_message != variant.error_message
                or not isinstance(snapshot, Mapping)
                or snapshot.get("variant_key") != variant.variant_key
                or snapshot.get("ordinal") != variant.ordinal
                or str(snapshot.get("status") or "").strip().lower() != attempt_status
                or snapshot.get("result") != attempt.result
                or snapshot.get("error_type") != attempt.error_type
                or snapshot.get("error_message") != attempt.error_message
            ):
                return None
            current.append(attempt)

        aggregate_status = _matrix_aggregate_status(current)
        snapshot_status = str(final_result.get("status") or "").strip().lower()
        if snapshot_status not in TERMINAL_STATUSES or _status_family(snapshot_status) != _status_family(aggregate_status):
            return None
        final_attempt_id = getattr(job, "final_attempt_id", None)
        representative = next((item for item in current if item.pk == final_attempt_id), None)
        if representative is None or not representative.is_final:
            return None
        if _status_family(status) != _status_family(aggregate_status):
            if not (
                status == "blocked"
                and aggregate_status == "passed"
                and _has_failed_matrix_gate_proof(job, representative, final_result)
            ):
                return None
        return current

    attempt_id = getattr(job, "final_attempt_id", None)
    if attempt_id:
        attempt = ExecutionAttempt.objects.filter(pk=attempt_id, job=job, is_final=True).first()
    else:
        attempt = job.attempts.filter(is_final=True).order_by("-attempt_number").first()
    if attempt is None:
        return None
    attempt_status = str(attempt.status or "").strip().lower()
    if attempt_status not in TERMINAL_STATUSES or attempt_status != status:
        return None
    return [attempt]


def _final_attempt(job: ExecutionJob) -> ExecutionAttempt | None:
    attempts = _final_attempts(job)
    if not attempts:
        return None
    final_attempt_id = getattr(job, "final_attempt_id", None)
    if final_attempt_id:
        return next((attempt for attempt in attempts if attempt.pk == final_attempt_id), None)
    return attempts[0] if not attempts[0].variant_id else None


def _case_results(job: ExecutionJob) -> dict[str, dict[str, Any]]:
    attempts = _final_attempts(job)
    if not attempts:
        return {}

    variant_positions = {
        attempt.variant_id: index
        for index, attempt in enumerate(attempt for attempt in attempts if attempt.variant_id)
    }
    expected_coverage = (1 << len(variant_positions)) - 1
    rows = ExecutionStepResult.objects.filter(
        attempt_id__in=[attempt.pk for attempt in attempts]
    ).order_by("attempt_id", "case_key", "step_key").values_list(
        "case_key", "status", "error_message", "duration_ms", "attempt__variant_id",
    ).iterator(chunk_size=2000)

    summaries: dict[str, dict[str, Any]] = {}
    for case_key, raw_status, error_message, duration_ms, variant_id in rows:
        status = str(raw_status or "").lower()
        summary = summaries.setdefault(case_key, {
            "failed": None,
            "blocked": None,
            "incomplete": None,
            "last": None,
            "coverage": 0,
        })
        representative = (status, error_message, duration_ms)
        if status == "failed" and summary["failed"] is None:
            summary["failed"] = representative
        elif status == "blocked" and summary["blocked"] is None:
            summary["blocked"] = representative
        if status not in SUCCESS_STATUSES and summary["incomplete"] is None:
            summary["incomplete"] = representative
        summary["last"] = representative
        if variant_id in variant_positions:
            summary["coverage"] |= 1 << variant_positions[variant_id]

    result: dict[str, dict[str, Any]] = {}
    is_matrix = bool(variant_positions)
    for case_key, summary in summaries.items():
        if summary["failed"] is not None:
            status, error_message, duration_ms = summary["failed"]
        elif summary["blocked"] is not None:
            status, error_message, duration_ms = summary["blocked"]
        elif is_matrix and summary["coverage"] != expected_coverage:
            representative = summary["incomplete"] or summary["last"]
            _representative_status, error_message, duration_ms = representative
            status = "skipped"
        elif summary["incomplete"] is not None:
            status, error_message, duration_ms = summary["incomplete"]
        else:
            status, error_message, duration_ms = summary["last"]
        result[case_key] = {
            "case_key": case_key,
            "status": status,
            "error_message": error_message,
            "duration_ms": duration_ms,
        }
    return result


def compare_jobs(current: ExecutionJob, baseline: ExecutionJob) -> dict[str, Any]:
    if current.project_id != baseline.project_id:
        raise ValueError("只能比较同一项目的执行任务")
    current_rows = _case_results(current)
    baseline_rows = _case_results(baseline)
    current_keys = set(current_rows)
    baseline_keys = set(baseline_rows)

    new_failures = sorted(
        key for key in current_keys
        if current_rows[key]["status"] in TERMINAL_FAILURES
        and baseline_rows.get(key, {}).get("status") not in TERMINAL_FAILURES
    )
    fixed_failures = sorted(
        key for key in baseline_keys
        if baseline_rows[key]["status"] in TERMINAL_FAILURES
        and current_rows.get(key, {}).get("status") in SUCCESS_STATUSES
    )
    unchanged_failures = sorted(
        key for key in current_keys & baseline_keys
        if current_rows[key]["status"] in TERMINAL_FAILURES
        and baseline_rows[key]["status"] in TERMINAL_FAILURES
    )
    added_cases = sorted(current_keys - baseline_keys)
    removed_cases = sorted(baseline_keys - current_keys)
    unexecuted = sorted(
        key for key, value in current_rows.items()
        if value["status"] in UNEXECUTED_STATUSES
        or value["status"] not in SUCCESS_STATUSES | TERMINAL_FAILURES
    )

    return redact_result_payload({
        "current_job_id": current.pk,
        "baseline_job_id": baseline.pk,
        "current_status": current.final_status or current.status,
        "baseline_status": baseline.final_status or baseline.status,
        "current_total": len(current_rows),
        "baseline_total": len(baseline_rows),
        "new_failures": new_failures,
        "fixed_failures": fixed_failures,
        "unchanged_failures": unchanged_failures,
        "added_cases": added_cases,
        "removed_cases": removed_cases,
        "unexecuted": unexecuted,
        "failure_count": len(new_failures) + len(unchanged_failures),
    })


def _trusted_final_evidence(
    job: ExecutionJob,
    *,
    require_success: bool,
) -> tuple[ExecutionAttempt | None, dict[str, Any] | None, list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    status = str(getattr(job, "final_status", "") or "").strip().lower()
    job_status = str(getattr(job, "status", "") or "").strip().lower()

    if status not in TERMINAL_STATUSES:
        failures.append({"metric": "final_evidence", "reason": "non_terminal_final_status"})
    elif job_status != status:
        failures.append({"metric": "final_evidence", "reason": "job_final_status_mismatch"})

    is_matrix = _is_matrix_job(job)
    attempt = _final_attempt(job)
    if attempt is None:
        failures.append({"metric": "final_evidence", "reason": "missing_final_attempt"})
    else:
        attempt_status = str(attempt.status or "").strip().lower()
        if not attempt.is_final:
            failures.append({"metric": "final_evidence", "reason": "attempt_not_final"})
        if attempt_status not in TERMINAL_STATUSES or (not is_matrix and attempt_status != status):
            failures.append({"metric": "final_evidence", "reason": "attempt_final_status_mismatch"})

    final_result = getattr(job, "final_result", None)
    if not isinstance(final_result, Mapping) or not final_result:
        failures.append({"metric": "final_evidence", "reason": "missing_final_result"})
        final_result = None
    else:
        final_result = dict(final_result)

    if status in TERMINAL_STATUSES:
        allowed_statuses = SUCCESS_STATUSES if require_success else BASELINE_STATUSES
        if status not in allowed_statuses:
            reason = "final_status_not_successful" if require_success else "baseline_status_not_comparable"
            failures.append({"metric": "final_evidence", "reason": reason})

    return attempt, final_result, failures


def _release_policy(
    job: ExecutionJob,
    *,
    baseline: ExecutionJob | None,
    effective_policy: Mapping[str, Any] | None,
    policy_error: str,
    policy_version: str,
    policy_snapshot: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    snapshot: dict[str, Any] = {
        "schema": "release-gate/v2",
        "profile_id": job.profile_id,
        "baseline_job_id": getattr(baseline, "pk", None),
    }
    if effective_policy is not None:
        snapshot["effective_rules"] = effective_policy["rules"]
        snapshot["thresholds"] = {
            "max_failed": effective_policy["max_failed"],
            "max_unmatched": effective_policy["max_unmatched"],
        }
    else:
        snapshot["policy_error"] = policy_error or "invalid_policy"
    if policy_snapshot:
        snapshot["declared"] = redact_result_payload(dict(policy_snapshot))
    version = policy_version_for_snapshot(snapshot, policy_version)
    snapshot["policy_version"] = version
    return version, snapshot


def evaluate_release_gate(
    job: ExecutionJob,
    *,
    baseline: ExecutionJob | None = None,
    max_failed: int = 0,
    max_unmatched: int = 0,
    policy_version: str = "",
    policy_snapshot: Mapping[str, Any] | None = None,
    metric_thresholds: Mapping[str, Any] | None = None,
    evaluated_by=None,
) -> dict[str, Any]:
    profile_gate = getattr(job.profile, "quality_gate", {}) if job.profile_id else {}
    policy_error = ""
    effective_policy: dict[str, Any] | None
    try:
        effective_policy = build_effective_policy(
            profile_gate,
            metric_thresholds,
            max_failed=max_failed,
            max_unmatched=max_unmatched,
        )
    except (TypeError, ValueError) as exc:
        effective_policy = None
        policy_error = str(exc)

    resolved_policy_version, resolved_policy_snapshot = _release_policy(
        job,
        baseline=baseline,
        effective_policy=effective_policy,
        policy_error=policy_error,
        policy_version=policy_version,
        policy_snapshot=policy_snapshot,
    )

    attempt, summary, evidence_failures = _trusted_final_evidence(job, require_success=True)
    baseline_attempt = None
    baseline_summary = None
    baseline_failures: list[dict[str, Any]] = []
    if baseline is not None:
        baseline_attempt, baseline_summary, baseline_failures = _trusted_final_evidence(
            baseline,
            require_success=False,
        )

    failures: list[dict[str, Any]] = []
    if policy_error:
        failures.append({"metric": "__policy__", "reason": "invalid_policy", "detail": policy_error})
    failures.extend(evidence_failures)
    if baseline is not None and baseline_failures:
        failures.extend({"metric": "baseline", "reason": item["reason"]} for item in baseline_failures)

    diff = None
    if summary is not None and baseline is not None and baseline_summary is not None and not baseline_failures:
        diff = compare_jobs(job, baseline)
        if diff.get("new_failures"):
            failures.append({"metric": "new_failures", "actual": len(diff["new_failures"]), "limit": 0})

    if effective_policy is not None and summary is not None:
        rule_failures = evaluate_metric_rules(
            effective_policy["rules"],
            summary,
            baseline_summary=baseline_summary if baseline_attempt is not None and not baseline_failures else None,
        )
        for item in rule_failures:
            metric = item.get("metric")
            if metric not in {"failed", "unmatched"}:
                item["metric"] = f"metric:{metric}"
            failures.append(item)

    safe_summary = redact_result_payload(finite_json_value(summary or {}))
    safe_diff = redact_result_payload(diff or {})
    effective_rules = effective_policy["rules"] if effective_policy is not None else {}
    result = redact_result_payload({
        "configured": True,
        "passed": not failures,
        "failures": failures,
        "summary": safe_summary,
        "diff": safe_diff,
        "thresholds": {
            "max_failed": effective_policy["max_failed"],
            "max_unmatched": effective_policy["max_unmatched"],
        } if effective_policy is not None else {},
        "metric_thresholds": effective_rules,
        "effective_rules": effective_rules,
        "policy_version": resolved_policy_version,
        "policy_snapshot": resolved_policy_snapshot,
        "exit_code": 0 if not failures else 1,
    })
    evaluation_key = hashlib.sha256(json.dumps({
        "job_id": job.pk,
        "baseline_job_id": getattr(baseline, "pk", None),
        "result": result,
    }, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    with transaction.atomic():
        evaluation, _created = ExecutionQualityGateEvaluation.objects.get_or_create(
            evaluation_key=evaluation_key,
            defaults={
                "job": job,
                "attempt": attempt,
                "profile": job.profile,
                "stage": "release",
                "passed": not failures,
                "summary": result.get("summary", {}),
                "failures": failures,
                "policy_version": resolved_policy_version,
                "policy_snapshot": resolved_policy_snapshot,
                "reason": "passed" if not failures else "release_gate_failed",
                "evaluated_by": evaluated_by,
            },
        )
        job.quality_gate_result = result
        job.save(update_fields=["quality_gate_result", "updated_at"])
    result["evaluation_id"] = evaluation.pk
    return result
