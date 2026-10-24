"""Execution governance policies shared by adapters, events, and APIs.

The module keeps policy decisions deterministic and records no secret values.  It
is intentionally additive: callers that do not configure a retry policy or a
quality gate retain the existing executor behavior.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (
    ExecutionAISuggestion,
    ExecutionAttempt,
    ExecutionJob,
    ExecutionQualityGateEvaluation,
    ExecutionRetryPolicy,
    ExecutionRetryRequest,
)

def redact_result_payload(value: Any, **kwargs: Any) -> Any:
    from .result_events import redact_result_payload as _redact

    return _redact(value, **kwargs)


class GovernanceConflict(ValueError):
    """Raised when a configured execution governance rule blocks an action."""


class AISuggestionDisabled(GovernanceConflict):
    """Raised when the audited AI feature flag is disabled."""


@dataclass(frozen=True)
class RetryDecision:
    allowed: bool
    reason: str
    attempt_number: int
    max_attempts: int
    policy_id: int | None = None
    automatic: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "attempt_number": self.attempt_number,
            "max_attempts": self.max_attempts,
            "policy_id": self.policy_id,
            "automatic": self.automatic,
        }


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _normalise_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip().lower()] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip().lower() for item in value if str(item).strip()]


def _profile_case_key(job: ExecutionJob, case_key: str = "") -> str:
    if case_key:
        return str(case_key)
    snapshot = job.environment_snapshot or {}
    if isinstance(snapshot, Mapping):
        for key in ("case_key", "test_key", "case_id"):
            if snapshot.get(key):
                return str(snapshot[key])
    profile = getattr(job, "profile", None)
    selection = getattr(profile, "test_selection", {}) if profile else {}
    if isinstance(selection, Mapping):
        for key in ("case_key", "test_key", "case_id"):
            if selection.get(key):
                return str(selection[key])
    return str(job.source_id or "")


def _remember_retry_snapshot(job: ExecutionJob, resolved: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        "policy_id": resolved.get("policy_id"),
        "source": resolved.get("source"),
        "max_attempts": resolved.get("max_attempts", 1),
        "retryable_error_types": list(resolved.get("retryable_error_types", [])),
        "retryable_statuses": list(resolved.get("retryable_statuses", [])),
        "backoff_seconds": resolved.get("backoff_seconds", 0),
        "case_key": resolved.get("case_key", ""),
    }
    if getattr(job, "pk", None) and not getattr(job, "retry_policy_snapshot", None):
        job.retry_policy_snapshot = snapshot
        job.save(update_fields=["retry_policy_snapshot", "updated_at"])
    resolved = dict(resolved)
    resolved["snapshot"] = snapshot
    return resolved


def resolve_retry_policy(job: ExecutionJob, case_key: str = "") -> dict[str, Any]:
    """Resolve one immutable project/profile/case policy snapshot per job."""

    existing_snapshot = getattr(job, "retry_policy_snapshot", {}) or {}
    if isinstance(existing_snapshot, Mapping) and existing_snapshot:
        policy_id = existing_snapshot.get("policy_id")
        return {
            "policy": ExecutionRetryPolicy.objects.filter(pk=policy_id).first() if policy_id else None,
            "policy_id": policy_id,
            "source": existing_snapshot.get("source", "snapshot"),
            "max_attempts": max(1, int(existing_snapshot.get("max_attempts", 1) or 1)),
            "retryable_error_types": _normalise_list(existing_snapshot.get("retryable_error_types")),
            "retryable_statuses": _normalise_list(existing_snapshot.get("retryable_statuses")),
            "backoff_seconds": max(0, int(existing_snapshot.get("backoff_seconds", 0) or 0)),
            "case_key": existing_snapshot.get("case_key", _profile_case_key(job, case_key)),
            "snapshot": dict(existing_snapshot),
        }

    case = _profile_case_key(job, case_key)
    profile_id = getattr(job, "profile_id", None)
    queryset = ExecutionRetryPolicy.objects.filter(project_id=job.project_id, enabled=True)
    candidates = list(queryset.filter(profile_id=profile_id, case_key=case)) if profile_id else []
    if not candidates and profile_id:
        candidates = list(queryset.filter(profile_id=profile_id, case_key=""))
    if not candidates:
        candidates = list(queryset.filter(profile__isnull=True, case_key=case))
    if not candidates:
        candidates = list(queryset.filter(profile__isnull=True, case_key=""))
    policy = candidates[0] if candidates else None

    profile = getattr(job, "profile", None)
    profile_config = getattr(profile, "retry_policy", {}) if profile else {}
    if not isinstance(profile_config, Mapping):
        profile_config = {}

    if policy is not None:
        return _remember_retry_snapshot(job, {
            "policy": policy,
            "policy_id": policy.pk,
            "source": "case" if policy.case_key else ("profile" if policy.profile_id else "project"),
            "max_attempts": max(1, int(policy.max_attempts or 1)),
            "retryable_error_types": _normalise_list(policy.retryable_error_types),
            "retryable_statuses": _normalise_list(policy.retryable_statuses),
            "backoff_seconds": int(policy.backoff_seconds or 0),
            "case_key": case,
        })

    configured_limit = profile_config.get("max_attempts")
    if configured_limit is None and profile_config.get("retry_limit") is not None:
        configured_limit = int(profile_config.get("retry_limit") or 0) + 1
    if configured_limit is None and profile is not None and getattr(profile, "retry_limit", 0):
        configured_limit = int(profile.retry_limit) + 1
    return _remember_retry_snapshot(job, {
        "policy": None,
        "policy_id": None,
        "source": "profile_config" if profile_config or getattr(profile, "retry_limit", 0) else "default",
        "max_attempts": max(1, int(configured_limit or 1)),
        "retryable_error_types": _normalise_list(profile_config.get("retryable_error_types")),
        "retryable_statuses": _normalise_list(profile_config.get("retryable_statuses")),
        "backoff_seconds": max(0, int(profile_config.get("backoff_seconds", 0) or 0)),
        "case_key": case,
    })


def should_retry(
    job: ExecutionJob,
    attempt: ExecutionAttempt | None = None,
    *,
    case_key: str = "",
    automatic: bool = True,
) -> RetryDecision:
    """Return a fail-closed retry decision without mutating execution rows."""

    policy = resolve_retry_policy(job, case_key)
    attempt_number = int(getattr(attempt, "attempt_number", None) or getattr(job, "attempt", 0) or 0)
    if attempt_number < 1:
        attempt_number = 1
    if not automatic:
        # Manual retry remains available for terminal failures for compatibility;
        # the configured cap still prevents unbounded automatic-style retries.
        return RetryDecision(
            allowed=attempt_number < policy["max_attempts"] or policy["source"] == "default",
            reason="MANUAL_RETRY" if attempt_number < policy["max_attempts"] or policy["source"] == "default" else "MAX_ATTEMPTS",
            attempt_number=attempt_number,
            max_attempts=policy["max_attempts"],
            policy_id=policy["policy_id"],
            automatic=False,
        )
    if policy["source"] == "default":
        return RetryDecision(False, "NO_AUTOMATIC_POLICY", attempt_number, policy["max_attempts"], automatic=True)
    if attempt_number >= policy["max_attempts"]:
        return RetryDecision(False, "MAX_ATTEMPTS", attempt_number, policy["max_attempts"], policy["policy_id"])

    status = str(getattr(attempt, "status", getattr(job, "status", "")) or "").lower()
    error_type = str(getattr(attempt, "error_type", getattr(job, "error_type", "")) or "").lower()
    statuses = policy["retryable_statuses"]
    error_types = policy["retryable_error_types"]
    if statuses and status not in statuses:
        return RetryDecision(False, "STATUS_NOT_RETRYABLE", attempt_number, policy["max_attempts"], policy["policy_id"])
    if error_types and error_type not in error_types:
        return RetryDecision(False, "ERROR_NOT_RETRYABLE", attempt_number, policy["max_attempts"], policy["policy_id"])
    return RetryDecision(True, "RETRYABLE", attempt_number, policy["max_attempts"], policy["policy_id"])


def mark_retry_requested(
    attempt: ExecutionAttempt,
    reason: str,
    requested_by: Any = None,
    *,
    policy: Mapping[str, Any] | None = None,
    decision: str = "RETRYABLE",
    automatic: bool = True,
    scheduled_at=None,
) -> ExecutionAttempt:
    """Record policy, mode, decision, and schedule with the retry reason."""

    policy = policy or {}
    attempt.retry_reason = str(reason or "retry requested")[:4000]
    attempt.retry_requested_by = requested_by
    attempt.retry_requested_at = timezone.now()
    attempt.retry_policy_id = policy.get("policy_id")
    attempt.retry_mode = "automatic" if automatic else "manual"
    attempt.retry_decision = str(decision or "")[:64]
    attempt.retry_scheduled_at = scheduled_at
    attempt.is_final = False
    attempt.save(update_fields=[
        "retry_reason", "retry_requested_by", "retry_requested_at", "retry_policy",
        "retry_mode", "retry_decision", "retry_scheduled_at", "is_final", "updated_at",
    ])
    return attempt


def create_retry_request(
    job: ExecutionJob,
    previous_attempt: ExecutionAttempt,
    next_attempt: ExecutionAttempt,
    policy: Mapping[str, Any],
    reason: str,
    *,
    automatic: bool = True,
    requested_by: Any = None,
) -> ExecutionRetryRequest:
    delay = max(0, int(policy.get("backoff_seconds", 0) or 0)) if automatic else 0
    scheduled_at = timezone.now() + timedelta(seconds=delay)
    request_key = f"retry:{job.pk}:{next_attempt.pk}"
    request, _created = ExecutionRetryRequest.objects.get_or_create(
        idempotency_key=request_key,
        defaults={
            "job": job,
            "from_attempt": previous_attempt,
            "to_attempt": next_attempt,
            "policy_id": policy.get("policy_id"),
            "reason": str(reason or "retry requested")[:4000],
            "automatic": automatic,
            "scheduled_at": scheduled_at,
        },
    )
    return request



def clear_final_for_retry(job: ExecutionJob) -> ExecutionJob:
    """Reopen finalization while retaining the previous snapshot for history."""
    job.final_attempt = None
    job.final_status = ""
    job.finalized_at = None
    job.save(update_fields=["final_attempt", "final_status", "finalized_at", "updated_at"])
    return job

def preserve_final_result(
    job: ExecutionJob,
    attempt: ExecutionAttempt,
    status: str,
    result: Mapping[str, Any] | None = None,
    gate_result: Mapping[str, Any] | None = None,
) -> bool:
    """Finalize one attempt monotonically; late attempts cannot overwrite it."""

    with transaction.atomic():
        locked_job = ExecutionJob.objects.select_for_update().get(pk=job.pk)
        if locked_job.final_attempt_id and locked_job.final_attempt_id != attempt.pk:
            return False
        safe_result = redact_result_payload(dict(result or attempt.result or {}))
        ExecutionAttempt.objects.filter(job=locked_job).exclude(pk=attempt.pk).update(is_final=False)
        locked_attempt = ExecutionAttempt.objects.select_for_update().get(pk=attempt.pk)
        locked_attempt.is_final = True
        locked_attempt.save(update_fields=["is_final", "updated_at"])
        locked_job.final_result = safe_result if isinstance(safe_result, dict) else {"value": safe_result}
        locked_job.final_attempt = locked_attempt
        locked_job.final_status = str(status or "")[:32]
        locked_job.finalized_at = timezone.now()
        if gate_result is not None:
            safe_gate = redact_result_payload(dict(gate_result))
            locked_job.quality_gate_result = safe_gate if isinstance(safe_gate, dict) else {"value": safe_gate}
        locked_job.save(update_fields=[
            "final_result", "final_attempt", "final_status", "finalized_at",
            "quality_gate_result", "updated_at",
        ])
        job.final_result = locked_job.final_result
        job.final_attempt_id = locked_job.final_attempt_id
        job.final_status = locked_job.final_status
        job.finalized_at = locked_job.finalized_at
        job.quality_gate_result = locked_job.quality_gate_result
    return True


def _quality_gate(profile: Any, summary: Mapping[str, Any]) -> dict[str, Any]:
    from .ci import quality_gate_result

    return quality_gate_result(profile, summary)


def apply_quality_gate(
    job: ExecutionJob,
    summary: Mapping[str, Any] | None,
    *,
    stage: str = "result",
    evaluated_by: Any = None,
    attempt: ExecutionAttempt | None = None,
) -> dict[str, Any]:
    """Evaluate and audit a configured gate; missing configured data fails closed."""

    profile = getattr(job, "profile", None)
    gate = getattr(profile, "quality_gate", {}) if profile else {}
    if not gate:
        return {"configured": False, "passed": True, "failures": []}
    safe_summary = redact_result_payload(dict(summary or {}))
    result = _quality_gate(profile, summary or {})
    failures = result.get("failures", [])
    safe_result = {
        "configured": True,
        "passed": bool(result.get("passed")),
        "failures": redact_result_payload(failures),
    }
    if isinstance(safe_summary, dict):
        safe_result["summary"] = safe_summary
    evaluation_key = hashlib.sha256(_canonical({
        "job_id": job.pk,
        "attempt_id": getattr(attempt, "pk", None),
        "stage": stage,
        "summary": safe_summary,
        "gate": redact_result_payload(gate),
    }).encode("utf-8")).hexdigest()
    evaluation, _created = ExecutionQualityGateEvaluation.objects.get_or_create(
        evaluation_key=evaluation_key,
        defaults={
            "job": job,
            "attempt": attempt,
            "profile": profile,
            "stage": stage,
            "passed": bool(result.get("passed")),
            "summary": safe_summary if isinstance(safe_summary, dict) else {"value": safe_summary},
            "failures": safe_result["failures"] if isinstance(safe_result["failures"], list) else [],
            "reason": "passed" if result.get("passed") else "quality_gate_failed",
            "evaluated_by": evaluated_by,
        },
    )
    job.quality_gate_result = safe_result
    job.save(update_fields=["quality_gate_result", "updated_at"])
    return safe_result


def active_quarantine_for_job(job: ExecutionJob, case_key: str = "") -> tuple[Any, dict[str, Any]]:
    """Find one effective QI quarantine matching a unified job, if available."""

    try:
        from apps.quality_intelligence.models import QualityQuarantine, QualityTestProfile
        from apps.quality_intelligence.services.flakiness import validate_quarantine_state
    except Exception:
        return None, {"matched": False, "reason": "QI_UNAVAILABLE"}

    source_type = {"performance": "PERF"}.get(str(job.source_type or "").lower(), str(job.source_type or "").upper())
    profile_key = _profile_case_key(job, case_key)
    if not profile_key:
        return None, {"matched": False, "reason": "CASE_KEY_MISSING"}
    profiles = QualityTestProfile.objects.filter(
        source_type=source_type,
        project_id=str(job.project_id),
        test_key=profile_key,
    )
    environment = (job.environment_snapshot or {}).get("environment_fingerprint") if isinstance(job.environment_snapshot, Mapping) else None
    if environment:
        profiles = profiles.filter(environment_fingerprint=environment)
    for quarantine in QualityQuarantine.objects.filter(
        profile__in=profiles,
        status="ACTIVE",
    ).select_related("profile").order_by("-created_at"):
        state = validate_quarantine_state(quarantine)
        if state.get("is_active"):
            return quarantine, {"matched": True, "reason": state.get("reason", "ACTIVE"), "profile_id": quarantine.profile_id}
    return None, {"matched": False, "reason": "NO_ACTIVE_QUARANTINE", "test_key": profile_key}


def check_dispatch_governance(
    job: ExecutionJob,
    *,
    override: bool = False,
    override_reason: str = "",
    actor: Any = None,
) -> dict[str, Any]:
    quarantine, details = active_quarantine_for_job(job)
    if quarantine is None:
        return {"allowed": True, "reason": details.get("reason", "NO_ACTIVE_QUARANTINE"), "quarantine": None}
    if not override:
        return {"allowed": False, "reason": "ACTIVE_QUARANTINE", "quarantine": quarantine, "details": details}
    if not bool(getattr(actor, "is_staff", False) or getattr(actor, "is_superuser", False)):
        return {"allowed": False, "reason": "OVERRIDE_FORBIDDEN", "quarantine": quarantine, "details": details}
    reason = str(override_reason or "").strip()
    if not reason:
        return {"allowed": False, "reason": "OVERRIDE_REASON_REQUIRED", "quarantine": quarantine, "details": details}
    try:
        from apps.quality_intelligence.services.governance import record_quarantine_audit

        record_quarantine_audit(
            quarantine,
            action="OVERRIDE",
            actor=actor,
            execution_job=job,
            reason=reason,
            metadata={"source": "execution_dispatch"},
        )
    except Exception:
        return {"allowed": False, "reason": "OVERRIDE_AUDIT_FAILED", "quarantine": quarantine, "details": details}
    return {"allowed": True, "reason": "QUARANTINE_OVERRIDDEN", "quarantine": quarantine, "details": details}


def sync_quality_result(job: ExecutionJob, attempt: ExecutionAttempt, status: str, result: Mapping[str, Any] | None = None) -> None:
    """Best-effort QI projection; execution lifecycle remains authoritative."""

    try:
        from apps.quality_intelligence.services.unified import sync_unified_attempt

        sync_unified_attempt(job, attempt, status, result)
    except Exception:
        # A derived quality projection must never turn a durable executor
        # result into a transient 500. The source result remains auditable.
        return None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _bound_ai_payload(value: Any, *, max_bytes: int = 65536) -> Any:
    safe = redact_result_payload(value)
    encoded = _canonical(safe).encode("utf-8")
    if len(encoded) <= max_bytes:
        return safe
    digest = hashlib.sha256(encoded).hexdigest()
    preview = encoded[:max_bytes - 180].decode("utf-8", errors="ignore")
    return {"_truncated": True, "sha256": digest, "preview": preview}

def record_ai_suggestion(
    *,
    project: Any,
    kind: str,
    input_payload: Any,
    output_payload: Any,
    job: ExecutionJob | None = None,
    prompt_version: str = "1",
    model_provider: str = "",
    model_name: str = "",
    target_type: str = "",
    target_key: str = "",
    source_fingerprint: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_micros: int = 0,
    requested_by: Any = None,
    force: bool = False,
) -> ExecutionAISuggestion:
    """Store a deterministic redacted suggestion; no model call or auto-apply."""

    if not force and not getattr(settings, "EXECUTION_AI_SUGGESTIONS_ENABLED", False):
        raise AISuggestionDisabled("Audited execution AI suggestions are disabled")
    safe_input = _bound_ai_payload(input_payload)
    safe_output = _bound_ai_payload(output_payload)
    digest_payload = {
        "project_id": getattr(project, "pk", project),
        "job_id": getattr(job, "pk", None),
        "kind": kind,
        "prompt_version": prompt_version,
        "model_provider": model_provider,
        "model_name": model_name,
        "target_type": target_type,
        "target_key": target_key,
        "source_fingerprint": source_fingerprint,
        "input": safe_input,
        "output": safe_output,
    }
    suggestion_hash = hashlib.sha256(_canonical(digest_payload).encode("utf-8")).hexdigest()
    values = {
        "project": project,
        "job": job,
        "kind": kind,
        "target_type": str(target_type or "")[:64],
        "target_key": str(target_key or "")[:255],
        "source_fingerprint": str(source_fingerprint or "")[:128],
        "prompt_version": str(prompt_version)[:64],
        "model_provider": str(model_provider or "")[:64],
        "model_name": str(model_name or "")[:128],
        "input_redacted": safe_input if isinstance(safe_input, dict) else {"value": safe_input},
        "output_redacted": safe_output if isinstance(safe_output, dict) else {"value": safe_output},
        "suggestion_hash": suggestion_hash,
        "input_tokens": max(0, int(input_tokens or 0)),
        "output_tokens": max(0, int(output_tokens or 0)),
        "cost_micros": max(0, int(cost_micros or 0)),
        "requested_by": requested_by,
    }
    suggestion, _created = ExecutionAISuggestion.objects.get_or_create(
        suggestion_hash=suggestion_hash,
        defaults=values,
    )
    return suggestion


def review_ai_suggestion(suggestion: ExecutionAISuggestion, reviewer: Any, status: str, note: str = "") -> ExecutionAISuggestion:
    status = str(status or "").lower()
    if status not in {"approved", "rejected"}:
        raise GovernanceConflict("AI suggestion review status must be approved or rejected")
    with transaction.atomic():
        locked = ExecutionAISuggestion.objects.select_for_update().get(pk=suggestion.pk)
        if locked.status != "proposed":
            raise GovernanceConflict("AI suggestion review is already terminal")
        previous = locked.status
        locked.status = status
        locked.reviewed_by = reviewer
        locked.reviewed_at = timezone.now()
        locked.review_note = str(note or "")[:4000]
        locked.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "updated_at"])
        from .models import ExecutionAISuggestionAudit
        ExecutionAISuggestionAudit.objects.create(
            suggestion=locked,
            action=status,
            actor=reviewer,
            previous_status=previous,
            new_status=status,
            note=locked.review_note,
        )
        return locked


# Backwards-friendly names for callers and integration tests.
resolve_execution_retry_policy = resolve_retry_policy
evaluate_retry = should_retry
create_ai_suggestion = record_ai_suggestion
apply_gate = apply_quality_gate

__all__ = [
    "AISuggestionDisabled",
    "GovernanceConflict",
    "RetryDecision",
    "active_quarantine_for_job",
    "apply_quality_gate",
    "check_dispatch_governance",
    "clear_final_for_retry",
    "create_ai_suggestion",
    "evaluate_retry",
    "mark_retry_requested",
    "preserve_final_result",
    "record_ai_suggestion",
    "resolve_retry_policy",
    "resolve_execution_retry_policy",
    "review_ai_suggestion",
    "should_retry",
    "sync_quality_result",
]
