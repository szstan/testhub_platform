"""Quarantine governance and immutable audit helpers."""

from __future__ import annotations

from typing import Any, Mapping

from django.db import transaction
from django.utils import timezone

from apps.quality_intelligence.models import (
    QualityQuarantine,
    QualityQuarantineApprovalStatus,
    QualityQuarantineAudit,
    QualityQuarantineStatus,
)
from apps.quality_intelligence.services.redaction import bound_text, redact_payload, redact_text
from apps.quality_intelligence.permissions import is_privileged


class QuarantineGovernanceError(ValueError):
    """Raised when a quarantine transition is invalid."""


def record_quarantine_audit(
    quarantine: QualityQuarantine,
    *,
    action: str,
    actor: Any = None,
    execution_job: Any = None,
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
    previous_status: str | None = None,
    new_status: str | None = None,
) -> QualityQuarantineAudit:
    """Append a bounded, redacted governance event."""

    action = str(action or "").upper()
    allowed = {value for value, _label in QualityQuarantineAudit.ACTION_CHOICES}
    if action not in allowed:
        raise QuarantineGovernanceError("unknown quarantine audit action")
    return QualityQuarantineAudit.objects.create(
        quarantine=quarantine,
        profile=quarantine.profile,
        execution_job=execution_job,
        action=action,
        actor=actor,
        reason=bound_text(redact_text(reason, max_length=4096), 4096),
        previous_status=str(previous_status if previous_status is not None else quarantine.status or "")[:16],
        new_status=str(new_status if new_status is not None else quarantine.status or "")[:16],
        metadata=redact_payload(dict(metadata or {})),
    )


def approve_quarantine(quarantine: QualityQuarantine, actor: Any, note: str = "") -> QualityQuarantine:
    with transaction.atomic():
        locked = QualityQuarantine.objects.select_for_update().get(pk=quarantine.pk)
        if not is_privileged(actor) or locked.owner_id == getattr(actor, "pk", None):
            raise QuarantineGovernanceError("quarantine approval requires an independent privileged reviewer")
        previous = locked.approval_status
        if locked.approval_status == QualityQuarantineApprovalStatus.APPROVED:
            return locked
        if locked.approval_status == QualityQuarantineApprovalStatus.REJECTED:
            raise QuarantineGovernanceError("rejected quarantine requires a new record")
        locked.approval_status = QualityQuarantineApprovalStatus.APPROVED
        locked.approved_by = actor
        locked.approved_at = timezone.now()
        locked.approval_note = bound_text(redact_text(note, max_length=4096), 4096)
        locked.status = QualityQuarantineStatus.ACTIVE
        locked.save(update_fields=["approval_status", "approved_by", "approved_at", "approval_note", "status", "updated_at"])
        record_quarantine_audit(
            locked,
            action="APPROVE",
            actor=actor,
            reason=note,
            previous_status=previous,
            new_status=locked.approval_status,
        )
        return locked


def reject_quarantine(quarantine: QualityQuarantine, actor: Any, note: str = "") -> QualityQuarantine:
    with transaction.atomic():
        locked = QualityQuarantine.objects.select_for_update().get(pk=quarantine.pk)
        if not is_privileged(actor) or locked.owner_id == getattr(actor, "pk", None):
            raise QuarantineGovernanceError("quarantine rejection requires an independent privileged reviewer")
        previous = locked.approval_status
        if locked.approval_status == QualityQuarantineApprovalStatus.APPROVED:
            raise QuarantineGovernanceError("approved quarantine cannot be rejected")
        locked.approval_status = QualityQuarantineApprovalStatus.REJECTED
        locked.approved_by = actor
        locked.approved_at = timezone.now()
        locked.approval_note = bound_text(redact_text(note, max_length=4096), 4096)
        locked.save(update_fields=["approval_status", "approved_by", "approved_at", "approval_note", "updated_at"])
        record_quarantine_audit(
            locked,
            action="REJECT",
            actor=actor,
            reason=note,
            previous_status=previous,
            new_status=locked.approval_status,
        )
        return locked


def release_quarantine(quarantine: QualityQuarantine, actor: Any, reason: str = "") -> QualityQuarantine:
    with transaction.atomic():
        locked = QualityQuarantine.objects.select_for_update().get(pk=quarantine.pk)
        if locked.owner_id != getattr(actor, "pk", None) and not is_privileged(actor):
            raise QuarantineGovernanceError("quarantine release is not authorized")
        if locked.status != QualityQuarantineStatus.ACTIVE:
            raise QuarantineGovernanceError("quarantine is already released or expired")
        previous = locked.status
        locked.status = QualityQuarantineStatus.RELEASED
        locked.released_by = actor
        locked.released_at = timezone.now()
        locked.save(update_fields=["status", "released_by", "released_at", "updated_at"])
        audit = record_quarantine_audit(locked, action="RELEASE", actor=actor, reason=reason)
        audit.previous_status = previous
        audit.new_status = locked.status
        audit.save(update_fields=["previous_status", "new_status"])
        return locked


def expire_quarantine(quarantine: QualityQuarantine, actor: Any = None, reason: str = "") -> QualityQuarantine:
    with transaction.atomic():
        locked = QualityQuarantine.objects.select_for_update().get(pk=quarantine.pk)
        if locked.status != QualityQuarantineStatus.ACTIVE:
            return locked
        previous = locked.status
        locked.status = QualityQuarantineStatus.EXPIRED
        locked.save(update_fields=["status", "updated_at"])
        audit = record_quarantine_audit(locked, action="EXPIRE", actor=actor, reason=reason)
        audit.previous_status = previous
        audit.new_status = locked.status
        audit.save(update_fields=["previous_status", "new_status"])
        return locked


__all__ = [
    "QuarantineGovernanceError",
    "approve_quarantine",
    "expire_quarantine",
    "record_quarantine_audit",
    "reject_quarantine",
    "release_quarantine",
]
