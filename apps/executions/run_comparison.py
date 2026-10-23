"""Deterministic run comparison and release quality-gate evaluation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.db import transaction

from apps.testcases.models import TestCase

from .models import ExecutionJob, ExecutionQualityGateEvaluation, ExecutionStepResult
from .result_events import redact_result_payload


TERMINAL_FAILURES = {"failed", "blocked"}


def _case_results(job: ExecutionJob) -> dict[str, dict[str, Any]]:
    attempt_id = job.final_attempt_id
    if not attempt_id:
        attempt = job.attempts.filter(is_final=True).order_by("-attempt_number").first()
        attempt_id = attempt.pk if attempt else None
    if not attempt_id:
        return {}
    rows = ExecutionStepResult.objects.filter(attempt_id=attempt_id).order_by("case_key", "step_key")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = result.setdefault(row.case_key, {
            "case_key": row.case_key,
            "status": row.status,
            "error_message": row.error_message,
            "duration_ms": row.duration_ms,
        })
        if row.status in TERMINAL_FAILURES or current["status"] not in TERMINAL_FAILURES:
            current.update({
                "status": row.status,
                "error_message": row.error_message,
                "duration_ms": row.duration_ms,
            })
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
        and current_rows.get(key, {}).get("status") not in TERMINAL_FAILURES
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
        if value["status"] in {"pending", "skipped", "blocked"}
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


def evaluate_release_gate(
    job: ExecutionJob,
    *,
    baseline: ExecutionJob | None = None,
    max_failed: int = 0,
    max_unmatched: int = 0,
    evaluated_by=None,
) -> dict[str, Any]:
    if max_failed < 0 or max_unmatched < 0:
        raise ValueError("门禁阈值不能为负数")
    summary = redact_result_payload(job.result or {})
    if not isinstance(summary, dict):
        summary = {"value": summary}
    diff = compare_jobs(job, baseline) if baseline is not None else None
    failures: list[dict[str, Any]] = []
    failed_count = int(summary.get("failed", 0) or 0)
    unmatched = int(summary.get("unmatched", 0) or 0)
    if failed_count > max_failed:
        failures.append({"metric": "failed", "actual": failed_count, "limit": max_failed})
    if unmatched > max_unmatched:
        failures.append({"metric": "unmatched", "actual": unmatched, "limit": max_unmatched})
    if diff and diff.get("new_failures"):
        failures.append({"metric": "new_failures", "actual": len(diff["new_failures"]), "limit": 0})

    result = redact_result_payload({
        "configured": True,
        "passed": not failures,
        "failures": failures,
        "summary": summary,
        "diff": diff or {},
        "thresholds": {"max_failed": max_failed, "max_unmatched": max_unmatched},
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
                "attempt": job.final_attempt,
                "profile": job.profile,
                "stage": "release",
                "passed": not failures,
                "summary": result.get("summary", {}),
                "failures": failures,
                "reason": "passed" if not failures else "release_gate_failed",
                "evaluated_by": evaluated_by,
            },
        )
        job.quality_gate_result = result
        job.save(update_fields=["quality_gate_result", "updated_at"])
    result["evaluation_id"] = evaluation.pk
    return result
