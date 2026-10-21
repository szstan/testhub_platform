"""Projection bridge from unified execution attempts to QI reliability data."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.quality_intelligence.models import (
    QualityItemOutcome,
    QualityRun,
    QualityRunItem,
    QualityRunStatus,
    QualityTestProfile,
)
from apps.quality_intelligence.services.flakiness import calculate_flakiness
from apps.quality_intelligence.services.redaction import redact_payload

_SOURCE_TYPES = {"api": "API", "ui": "UI", "app": "APP", "performance": "PERF"}
_SUCCESS = {"passed", "succeeded", "completed"}
_FAILURE = {"failed", "blocked", "unsupported"}


def _outcome(status: str) -> str:
    value = str(status or "").lower()
    if value in _SUCCESS:
        return QualityItemOutcome.PASS
    if value in _FAILURE:
        return QualityItemOutcome.FAIL if value == "failed" else QualityItemOutcome.ERROR
    if value in {"cancelled", "cancel_requested"}:
        return QualityItemOutcome.SKIP
    return QualityItemOutcome.INCONCLUSIVE


def _environment(job: Any) -> str:
    snapshot = getattr(job, "environment_snapshot", {}) or {}
    if isinstance(snapshot, Mapping):
        return str(snapshot.get("environment_fingerprint") or snapshot.get("name") or "unknown")[:128]
    return "unknown"


def sync_unified_attempt(job: Any, attempt: Any, status: str, result: Mapping[str, Any] | None = None) -> QualityRun | None:
    """Write a redacted QI run/item projection for one unified attempt.

    The bridge is enabled only with the existing QI rollout flag and never
    mutates source runner records. Replaying the same attempt updates one item.
    """

    if not getattr(settings, "QUALITY_INTELLIGENCE_ENABLED", False):
        return None
    source_type = _SOURCE_TYPES.get(str(getattr(job, "source_type", "")).lower())
    if source_type is None:
        return None
    environment = _environment(job)
    safe_result = redact_payload(dict(result or {}))
    with transaction.atomic():
        run, _created = QualityRun.objects.get_or_create(
            source_type=source_type,
            source_execution_id=f"unified:{job.pk}"[:128],
            defaults={
                "source_scope": "CASE",
                "project_type": source_type,
                "project_id": str(job.project_id),
                "source_asset_id": str(job.source_id or "")[:128],
                "source_asset_name": str(job.source_id or "")[:255],
                "environment_fingerprint": environment,
                "adapter_version": "unified-1.0",
            },
        )
        run.project_type = source_type
        run.project_id = str(job.project_id)
        run.source_status = str(status or "")[:64]
        run.environment_fingerprint = environment
        run.quality_status = QualityRunStatus.FAILED if str(status).lower() in _FAILURE else QualityRunStatus.INGESTED
        run.finished_at = getattr(attempt, "finished_at", None) or timezone.now()
        run.summary = safe_result if isinstance(safe_result, dict) else {"value": safe_result}
        run.provenance = {
            "source": "unified_execution",
            "job_id": job.pk,
            "attempt_id": attempt.pk,
            "attempt_number": attempt.attempt_number,
        }
        run.save(update_fields=[
            "project_type", "project_id", "source_status", "environment_fingerprint",
            "quality_status", "finished_at", "summary", "provenance", "updated_at",
        ])

        step_groups: dict[str, list[Any]] = defaultdict(list)
        for step in attempt.step_results.all():
            step_groups[str(step.case_key or "default")].append(step)
        if not step_groups:
            step_groups[str(getattr(job, "source_id", "") or "default")] = []
        for case_key, steps in step_groups.items():
            step_status = status
            if steps:
                step_status = "failed" if any(str(step.status).lower() in _FAILURE for step in steps) else str(steps[-1].status)
            details = {
                "job_id": job.pk,
                "attempt_id": attempt.pk,
                "attempt_number": attempt.attempt_number,
                "result": safe_result,
                "steps": [redact_payload(getattr(step, "result", {}) or {}) for step in steps],
            }
            QualityRunItem.objects.update_or_create(
                run=run,
                test_key=case_key[:255],
                attempt=attempt.attempt_number,
                defaults={
                    "source_item_type": "unified_execution",
                    "source_item_id": str(job.pk),
                    "name": case_key[:255],
                    "source_status": str(step_status)[:64],
                    "outcome": _outcome(step_status),
                    "environment_fingerprint": environment,
                    "details": details,
                },
            )

        # Update the matching profile deterministically from the retained QI
        # items. This keeps flaky classification independent of an LLM.
        for item in run.items.all():
            candidates = QualityRunItem.objects.filter(
                run__source_type=source_type,
                run__project_type=run.project_type,
                run__project_id=run.project_id,
                test_key=item.test_key,
                environment_fingerprint=environment,
            ).select_related("run").order_by("run__created_at", "id")
            analysis = calculate_flakiness(
                candidates,
                test_key=item.test_key,
                environment_fingerprint=environment,
                source_type=source_type,
                project_type=run.project_type,
                project_id=run.project_id,
            )
            # Persist the deterministic profile projection so quarantine and
            # execution governance can resolve the same flaky state.
            profile, _profile_created = QualityTestProfile.objects.get_or_create(
                source_type=source_type,
                project_type=run.project_type,
                project_id=run.project_id,
                test_key=item.test_key,
                environment_fingerprint=environment,
                defaults={"name": item.name},
            )
            profile.eligible_attempts = int(analysis.get("eligible_attempts", 0))
            profile.pass_count = int(analysis.get("pass_count", 0))
            profile.fail_count = int(analysis.get("fail_count", 0))
            profile.transition_count = int(analysis.get("transition_count", 0))
            profile.flaky_score = float(analysis.get("flaky_score", 0.0))
            profile.reliability_status = str(analysis.get("reliability_status", "INSUFFICIENT_DATA"))
            profile.metadata = redact_payload({"reason": analysis.get("reason", ""), "source": "unified_execution"})
            profile.last_seen_at = run.finished_at
            profile.save(update_fields=[
                "eligible_attempts", "pass_count", "fail_count", "transition_count",
                "flaky_score", "reliability_status", "metadata", "last_seen_at", "updated_at",
            ])
        return run


__all__ = ["sync_unified_attempt"]
