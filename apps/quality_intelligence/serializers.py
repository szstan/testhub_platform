"""Safe, read-oriented serializers for quality intelligence APIs."""

from __future__ import annotations

import re
from typing import Any

from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field

from apps.quality_intelligence.models import (
    QualityBaseline,
    QualityBaselineType,
    QualityComparison,
    QualityEvidence,
    QualityEvidenceType,
    QualityFailure,
    QualityFailureCluster,
    QualityFailureClusterAudit,
    QualityFailureReviewAudit,
    QualityFailureCategory,
    QualityMetricObservation,
    QualityQuarantine,
    QualityQuarantineApprovalStatus,
    QualityQuarantineAudit,
    QualityRun,
    QualityRunItem,
    QualitySourceType,
    QualityTestProfile,
)
from apps.quality_intelligence.services.redaction import (
    bound_text,
    redact_payload,
    redact_text,
)
from apps.quality_intelligence.adapters.base import safe_reference


_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
_PATH_KEY = re.compile(r"(?:^|_)(?:path|dir|directory|file|filename)$", re.IGNORECASE)


def _public_payload(value: Any, key: str = "") -> Any:
    value = redact_payload(value, max_text_length=4096, max_items=1000, max_depth=12)
    if isinstance(value, dict):
        return {str(child_key): _public_payload(child, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_public_payload(child, key) for child in value[:1000]]
    if isinstance(value, str) and _PATH_KEY.search(key) and _ABSOLUTE_PATH.match(value):
        return "[REDACTED]"
    return value


def _safe_text(value: Any, max_length: int = 4096) -> str:
    return bound_text(redact_text(value, max_length=max_length), max_length)


class PublicModelSerializer(serializers.ModelSerializer):
    classification_mode = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField())
    def get_classification_mode(self, obj) -> str:
        return "deterministic"

    def to_representation(self, instance):
        result = super().to_representation(instance)
        for key in ("summary", "provenance", "warnings", "details", "dimensions", "snapshot"):
            if key in result:
                result[key] = _public_payload(result[key])
        return result


class QualityRunSerializer(PublicModelSerializer):
    class Meta:
        model = QualityRun
        fields = (
            "id",
            "source_type",
            "source_scope",
            "source_execution_id",
            "project_type",
            "project_id",
            "source_asset_id",
            "source_asset_name",
            "source_status",
            "quality_status",
            "started_at",
            "finished_at",
            "duration_ms",
            "environment_fingerprint",
            "source_fingerprint",
            "adapter_version",
            "redaction_version",
            "schema_version",
            "summary",
            "provenance",
            "warnings",
            "classification_mode",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class QualityRunItemSerializer(PublicModelSerializer):
    class Meta:
        model = QualityRunItem
        fields = (
            "id",
            "run",
            "source_item_type",
            "source_item_id",
            "test_key",
            "name",
            "source_status",
            "outcome",
            "attempt",
            "duration_ms",
            "environment_fingerprint",
            "details",
            "created_at",
            "updated_at",
            "classification_mode",
        )
        read_only_fields = fields


class QualityEvidenceSerializer(PublicModelSerializer):
    class Meta:
        model = QualityEvidence
        fields = (
            "id",
            "run",
            "item",
            "evidence_type",
            "snippet",
            "source_uri",
            "source_field",
            "sha256",
            "mime_type",
            "size_bytes",
            "availability",
            "provenance",
            "redaction_version",
            "captured_at",
            "created_at",
            "classification_mode",
        )
        read_only_fields = fields

    def to_representation(self, instance):
        result = super().to_representation(instance)
        result["snippet"] = _safe_text(result.get("snippet", ""))
        result["source_uri"] = safe_reference(result.get("source_uri", ""))
        result["provenance"] = _public_payload(result.get("provenance", {}))
        return result


class QualityFailureClusterSerializer(PublicModelSerializer):
    class Meta:
        model = QualityFailureCluster
        fields = (
            'id', 'cluster_key', 'mapped_project', 'source_type', 'project_type', 'project_id',
            'test_key', 'environment_fingerprint', 'signature', 'taxonomy_code',
            'subtype', 'owner', 'defect', 'external_provider', 'external_key',
            'external_url', 'first_seen_at', 'last_seen_at', 'occurrence_count',
            'last_failure_run', 'last_fixed_run', 'resolved_at', 'metadata',
            'created_at', 'updated_at', 'classification_mode',
        )
        read_only_fields = fields

    def to_representation(self, instance):
        result = super().to_representation(instance)
        result['metadata'] = _public_payload(result.get('metadata', {}))
        result['external_url'] = safe_reference(result.get('external_url', ''))
        return result


class QualityFailureClusterAuditSerializer(PublicModelSerializer):
    class Meta:
        model = QualityFailureClusterAudit
        fields = (
            'id', 'cluster', 'action', 'actor', 'previous_value', 'new_value',
            'note', 'created_at', 'classification_mode',
        )
        read_only_fields = fields

    def to_representation(self, instance):
        result = super().to_representation(instance)
        result['previous_value'] = _public_payload(result.get('previous_value', {}))
        result['new_value'] = _public_payload(result.get('new_value', {}))
        result['note'] = _safe_text(result.get('note', ''))
        return result


class QualityFailureSerializer(PublicModelSerializer):
    class Meta:
        model = QualityFailure
        fields = (
            "id",
            "run",
            "item",
            "taxonomy_code",
            "subtype",
            "phase",
            "signature",
            "source_error_code",
            "message",
            "stack_excerpt",
            "retryable",
            "details",
            "initial_taxonomy_code",
            "classifier",
            "confidence",
            "rule_id",
            "reviewed_at",
            "review_note",
            "created_at",
            "updated_at",
            "classification_mode",
        )
        read_only_fields = fields

    def to_representation(self, instance):
        result = super().to_representation(instance)
        result["message"] = _safe_text(result.get("message", ""))
        result["stack_excerpt"] = _safe_text(result.get("stack_excerpt", ""))
        result["review_note"] = _safe_text(result.get("review_note", ""))
        result["details"] = _public_payload(result.get("details", {}))
        return result


class FailureReviewSerializer(serializers.Serializer):
    taxonomy_code = serializers.ChoiceField(choices=QualityFailureCategory.choices, required=True)
    subtype = serializers.CharField(max_length=64, required=False, allow_blank=True)
    review_note = serializers.CharField(max_length=4096, required=False, allow_blank=True)


class QualityFailureReviewAuditSerializer(PublicModelSerializer):
    class Meta:
        model = QualityFailureReviewAudit
        fields = (
            'id', 'failure', 'source_failure_key', 'actor', 'previous_taxonomy',
            'new_taxonomy', 'note', 'metadata', 'created_at', 'classification_mode',
        )
        read_only_fields = fields

    def to_representation(self, instance):
        result = super().to_representation(instance)
        result['note'] = _safe_text(result.get('note', ''))
        result['metadata'] = _public_payload(result.get('metadata', {}))
        return result


class QualityMetricObservationSerializer(PublicModelSerializer):
    class Meta:
        model = QualityMetricObservation
        fields = (
            "id",
            "run",
            "item",
            "profile",
            "metric_key",
            "sample_key",
            "value",
            "unit",
            "aggregation",
            "dimensions",
            "observed_at",
            "created_at",
            "classification_mode",
        )
        read_only_fields = fields


class QualityTestProfileSerializer(PublicModelSerializer):
    class Meta:
        model = QualityTestProfile
        fields = (
            "id",
            "source_type",
            "project_type",
            "project_id",
            "test_key",
            "name",
            "environment_fingerprint",
            "window_size",
            "min_attempts",
            "eligible_attempts",
            "pass_count",
            "fail_count",
            "transition_count",
            "flaky_score",
            "reliability_status",
            "first_seen_at",
            "last_seen_at",
            "metadata",
            "created_at",
            "updated_at",
            "classification_mode",
        )
        read_only_fields = fields


class QualityQuarantineSerializer(PublicModelSerializer):
    class Meta:
        model = QualityQuarantine
        fields = (
            "id",
            "profile",
            "status",
            "reason",
            "owner",
            "approval_status",
            "approved_by",
            "approved_at",
            "approval_note",
            "starts_at",
            "expires_at",
            "released_at",
            "note",
            "created_at",
            "updated_at",
            "classification_mode",
        )
        read_only_fields = fields

    def to_representation(self, instance):
        result = super().to_representation(instance)
        result["reason"] = _safe_text(result.get("reason", ""))
        result["note"] = _safe_text(result.get("note", ""))
        result["approval_note"] = _safe_text(result.get("approval_note", ""))
        return result


class QuarantineCreateSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=4096)
    starts_at = serializers.DateTimeField(required=False)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    note = serializers.CharField(max_length=4096, required=False, allow_blank=True)
    requires_approval = serializers.BooleanField(required=False, default=False)


class QualityQuarantineAuditSerializer(PublicModelSerializer):
    class Meta:
        model = QualityQuarantineAudit
        fields = (
            "id", "quarantine", "profile", "execution_job", "action", "actor",
            "reason", "previous_status", "new_status", "metadata", "created_at",
            "classification_mode",
        )
        read_only_fields = fields

    def to_representation(self, instance):
        result = super().to_representation(instance)
        result["reason"] = _safe_text(result.get("reason", ""))
        result["metadata"] = _public_payload(result.get("metadata", {}))
        return result


class QualityBaselineSerializer(PublicModelSerializer):
    profile_id = serializers.IntegerField(required=False, allow_null=True, source="profile.id", read_only=True)

    class Meta:
        model = QualityBaseline
        fields = (
            "id",
            "source_type",
            "project_type",
            "project_id",
            "profile_id",
            "metric_key",
            "environment_fingerprint",
            "baseline_type",
            "source_baseline_key",
            "source_run",
            "value",
            "unit",
            "tolerance",
            "direction",
            "snapshot",
            "active",
            "created_at",
            "updated_at",
            "classification_mode",
        )
        read_only_fields = (
            "id",
            "profile_id",
            "source_run",
            "created_at",
            "updated_at",
            "classification_mode",
        )

    def to_representation(self, instance):
        result = super().to_representation(instance)
        result["snapshot"] = _public_payload(result.get("snapshot", {}))
        return result


class QualityBaselineWriteSerializer(serializers.Serializer):
    source_type = serializers.ChoiceField(choices=QualitySourceType.choices)
    project_type = serializers.CharField(max_length=64)
    project_id = serializers.CharField(max_length=128)
    profile_id = serializers.IntegerField(required=False, allow_null=True)
    metric_key = serializers.CharField(max_length=128)
    environment_fingerprint = serializers.CharField(max_length=128, default="unknown")
    baseline_type = serializers.ChoiceField(choices=QualityBaselineType.choices, default=QualityBaselineType.MANUAL)
    source_baseline_key = serializers.CharField(max_length=255, required=False, allow_blank=True)
    value = serializers.FloatField()
    unit = serializers.CharField(max_length=32, required=False, allow_blank=True)
    tolerance = serializers.FloatField(required=False, default=0.0, min_value=0)
    direction = serializers.ChoiceField(choices=("LOWER_IS_BETTER", "HIGHER_IS_BETTER", "EQUAL"), default="LOWER_IS_BETTER")
    snapshot = serializers.JSONField(required=False, default=dict)
    active = serializers.BooleanField(required=False, default=True)


class QualityComparisonSerializer(PublicModelSerializer):
    class Meta:
        model = QualityComparison
        fields = (
            "id",
            "run",
            "baseline",
            "profile",
            "metric_key",
            "baseline_value",
            "actual_value",
            "delta",
            "tolerance",
            "unit",
            "verdict",
            "details",
            "created_at",
            "classification_mode",
        )
        read_only_fields = fields


class IngestSerializer(serializers.Serializer):
    source_type = serializers.ChoiceField(choices=QualitySourceType.choices)
    source_execution_id = serializers.CharField(max_length=128)

    def validate_source_execution_id(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("source_execution_id is required")
        return value


__all__ = [
    "FailureReviewSerializer",
    "IngestSerializer",
    "QualityBaselineSerializer",
    "QualityBaselineWriteSerializer",
    "QualityComparisonSerializer",
    "QualityEvidenceSerializer",
    "QualityFailureSerializer",
    "QualityFailureReviewAuditSerializer",
    "QualityMetricObservationSerializer",
    "QualityQuarantineSerializer",
    "QualityQuarantineAuditSerializer",
    "QualityRunItemSerializer",
    "QualityRunSerializer",
    "QualityTestProfileSerializer",
    "QuarantineCreateSerializer",
]
