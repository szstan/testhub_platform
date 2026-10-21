"""Authenticated DRF API for derived quality-intelligence data."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema

from apps.quality_intelligence.adapters.base import (
    AdapterError,
    SourceNotFoundError,
    SourceNotTerminalError,
)
from apps.quality_intelligence.filters import apply_quality_filters
from apps.quality_intelligence.models import (
    QualityBaseline,
    QualityComparison,
    QualityEvidence,
    QualityFailure,
    QualityFailureReviewAudit,
    QualityMetricObservation,
    QualityQuarantine,
    QualityQuarantineApprovalStatus,
    QualityQuarantineAudit,
    QualityRun,
    QualityRunItem,
    QualityTestProfile,
    QualityQuarantineStatus,
)
from apps.quality_intelligence.permissions import (
    QualityIntelligenceEnabledPermission,
    accessible_project_ids,
    can_access_source_execution,
    is_privileged,
    quality_project_filter,
    source_execution_queryset,
)
from apps.quality_intelligence.serializers import (
    FailureReviewSerializer,
    IngestSerializer,
    QualityBaselineSerializer,
    QualityBaselineWriteSerializer,
    QualityComparisonSerializer,
    QualityEvidenceSerializer,
    QualityFailureSerializer,
    QualityFailureReviewAuditSerializer,
    QualityMetricObservationSerializer,
    QualityQuarantineSerializer,
    QualityQuarantineAuditSerializer,
    QualityRunItemSerializer,
    QualityRunSerializer,
    QualityTestProfileSerializer,
    QuarantineCreateSerializer,
)
from apps.quality_intelligence.services.baseline import compare_baseline
from apps.quality_intelligence.services.ingestion import ingest_source
from apps.quality_intelligence.services.governance import (
    QuarantineGovernanceError,
    approve_quarantine,
    record_quarantine_audit,
    reject_quarantine,
    release_quarantine,
)
from apps.quality_intelligence.services.redaction import bound_text, redact_payload, redact_text

logger = logging.getLogger(__name__)


class QualityPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


def _error(code: str, message: str, http_status: int, **extra: Any) -> Response:
    payload = {"error_code": code, "detail": message}
    payload.update(extra)
    return Response(payload, status=http_status)


def _safe_error_message(exc: Exception) -> str:
    return bound_text(redact_text(str(exc), max_length=512), 512)


def _run_queryset(user):
    return quality_project_filter(
        QualityRun.objects.all().select_related(), user
    )


def _item_queryset(user):
    return QualityRunItem.objects.filter(run__in=_run_queryset(user)).select_related("run")


def _evidence_queryset(user):
    return QualityEvidence.objects.filter(run__in=_run_queryset(user)).select_related("run", "item")


def _failure_queryset(user):
    return QualityFailure.objects.filter(run__in=_run_queryset(user)).select_related("run", "item")


def _metric_queryset(user):
    return QualityMetricObservation.objects.filter(run__in=_run_queryset(user)).select_related("run", "item", "profile")


def _profile_queryset(user):
    return quality_project_filter(QualityTestProfile.objects.all(), user)


def _baseline_queryset(user):
    return quality_project_filter(QualityBaseline.objects.all(), user)


def _comparison_queryset(user):
    return QualityComparison.objects.filter(run__in=_run_queryset(user)).select_related("run", "baseline", "profile")


def _paginate(view, queryset, serializer_class):
    page = view.paginate_queryset(queryset)
    if page is not None:
        return view.get_paginated_response(serializer_class(page, many=True).data)
    return Response(serializer_class(queryset, many=True).data)


def _parse_project_access(user, source_type: str, project_id: Any) -> bool:
    if is_privileged(user):
        return True
    visible = accessible_project_ids(user, source_type)
    return visible is not None and str(project_id) in visible


def _choose_baseline(run: QualityRun, observation: QualityMetricObservation):
    queryset = QualityBaseline.objects.filter(
        source_type=run.source_type,
        project_type=run.project_type,
        project_id=run.project_id,
        metric_key=observation.metric_key,
        environment_fingerprint=run.environment_fingerprint,
        active=True,
    )
    if observation.profile_id:
        profile_match = queryset.filter(profile_id=observation.profile_id).first()
        if profile_match is not None:
            return profile_match
    return queryset.filter(profile__isnull=True).first() or queryset.first()


def recompute_run_comparisons(run: QualityRun) -> list[QualityComparison]:
    """Rebuild comparisons from quality metrics only; never reads source apps."""

    with transaction.atomic():
        QualityComparison.objects.filter(run=run).delete()
        observations = list(
            run.metrics.select_related("item", "profile").order_by("metric_key", "sample_key", "id")
        )
        selected: dict[str, QualityMetricObservation] = {}
        for observation in observations:
            previous = selected.get(observation.metric_key)
            if previous is None or (
                observation.sample_key == "summary" and previous.sample_key != "summary"
            ):
                selected[observation.metric_key] = observation
        comparisons: list[QualityComparison] = []
        for metric_key, observation in selected.items():
            baseline = _choose_baseline(run, observation)
            actual = {
                "value": observation.value,
                "source_type": run.source_type,
                "project_type": run.project_type,
                "project_id": run.project_id,
                "profile_id": observation.profile_id,
                "environment_fingerprint": run.environment_fingerprint,
                "unit": observation.unit,
            }
            baseline_data = None
            if baseline is not None:
                baseline_data = {
                    "value": baseline.value,
                    "source_type": baseline.source_type,
                    "project_type": baseline.project_type,
                    "project_id": baseline.project_id,
                    "profile_id": baseline.profile_id,
                    "environment_fingerprint": baseline.environment_fingerprint,
                    "tolerance": baseline.tolerance,
                    "direction": baseline.direction,
                    "unit": baseline.unit,
                    "metric_key": baseline.metric_key,
                }
            result = compare_baseline(actual, baseline_data, metric_key=metric_key)
            comparisons.append(
                QualityComparison.objects.create(
                    run=run,
                    baseline=baseline,
                    profile=observation.profile,
                    metric_key=metric_key,
                    baseline_value=result.get("baseline_value"),
                    actual_value=result.get("actual_value"),
                    delta=result.get("delta"),
                    tolerance=result.get("tolerance"),
                    unit=observation.unit,
                    verdict=result.get("verdict", "ERROR"),
                    details=redact_payload(
                        {
                            "delta_pct": result.get("delta_pct"),
                            "allowed_delta": result.get("allowed_delta"),
                            "direction": result.get("direction"),
                            "reason": result.get("reason"),
                            "mismatch_fields": result.get("mismatch_fields", []),
                            "sample_key": observation.sample_key,
                        }
                    ),
                )
            )
        return comparisons


class IngestView(APIView):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]

    def post(self, request):
        serializer = IngestSerializer(data=request.data)
        if not serializer.is_valid():
            return _error("INVALID_INGEST_REQUEST", "invalid source ingestion request", status.HTTP_400_BAD_REQUEST, fields=serializer.errors)
        source_type = serializer.validated_data["source_type"]
        execution_id = serializer.validated_data["source_execution_id"]
        try:
            execution = source_execution_queryset(source_type, execution_id)
        except (TypeError, ValueError):
            return _error("INVALID_SOURCE_EXECUTION", "source_execution_id is invalid", status.HTTP_400_BAD_REQUEST)
        if execution is None or not can_access_source_execution(request.user, source_type, execution):
            return _error("SOURCE_NOT_FOUND", "source execution not found", status.HTTP_404_NOT_FOUND)
        try:
            result = ingest_source(source_type, execution_id, allow_missing=False)
        except SourceNotTerminalError:
            return _error("SOURCE_RUNNING", "source execution is not terminal", status.HTTP_409_CONFLICT)
        except SourceNotFoundError:
            return _error("SOURCE_NOT_FOUND", "source execution not found", status.HTTP_404_NOT_FOUND)
        except AdapterError as exc:
            return _error("INGESTION_INVALID", _safe_error_message(exc), status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("quality source ingestion failed", extra={"source_type": source_type})
            return _error("INGESTION_FAILED", "quality ingestion failed", status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(
            {
                "ingestion": result.as_dict(),
                "run": QualityRunSerializer(result.quality_run).data,
            },
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )


class RunViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]
    serializer_class = QualityRunSerializer
    pagination_class = QualityPagination
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        queryset = _run_queryset(self.request.user).order_by("-created_at")
        return apply_quality_filters(queryset, self.request.query_params)

    @action(detail=True, methods=["get"], url_path="items")
    def items(self, request, pk=None):
        run = self.get_object()
        queryset = QualityRunItem.objects.filter(run=run).order_by("id")
        queryset = apply_quality_filters(queryset, request.query_params)
        return _paginate(self, queryset, QualityRunItemSerializer)

    @action(detail=True, methods=["get"], url_path="evidence")
    def evidence(self, request, pk=None):
        run = self.get_object()
        queryset = QualityEvidence.objects.filter(run=run).order_by("-created_at")
        return _paginate(self, queryset, QualityEvidenceSerializer)

    @action(detail=True, methods=["post"], url_path="recompute")
    def recompute(self, request, pk=None):
        run = self.get_object()
        comparisons = recompute_run_comparisons(run)
        return Response(
            {
                "run": QualityRunSerializer(run).data,
                "comparisons": QualityComparisonSerializer(comparisons, many=True).data,
                "classification_mode": "deterministic",
            }
        )


class ItemViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]
    serializer_class = QualityRunItemSerializer
    pagination_class = QualityPagination
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        queryset = _item_queryset(self.request.user).order_by("-created_at", "id")
        return apply_quality_filters(queryset, self.request.query_params)

    @action(detail=True, methods=["get"], url_path="evidence")
    def evidence(self, request, pk=None):
        item = self.get_object()
        queryset = QualityEvidence.objects.filter(item=item).order_by("-created_at")
        return _paginate(self, queryset, QualityEvidenceSerializer)


class EvidenceViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]
    serializer_class = QualityEvidenceSerializer
    pagination_class = QualityPagination
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        queryset = _evidence_queryset(self.request.user).order_by("-created_at")
        return apply_quality_filters(queryset, self.request.query_params)


class FailureViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]
    serializer_class = QualityFailureSerializer
    pagination_class = QualityPagination
    http_method_names = ["get", "head", "options", "patch", "post"]

    def get_queryset(self):
        queryset = _failure_queryset(self.request.user).order_by("-created_at")
        return apply_quality_filters(queryset, self.request.query_params)

    @action(detail=True, methods=["patch", "post"], url_path="review")
    def review(self, request, pk=None):
        failure = self.get_object()
        serializer = FailureReviewSerializer(data=request.data)
        if not serializer.is_valid():
            return _error("INVALID_FAILURE_REVIEW", "invalid failure review", status.HTTP_400_BAD_REQUEST, fields=serializer.errors)
        values = serializer.validated_data
        previous_taxonomy = failure.taxonomy_code
        failure.taxonomy_code = values["taxonomy_code"]
        if "subtype" in values:
            failure.subtype = values["subtype"]
        failure.classifier = "HUMAN"
        failure.reviewer = request.user
        failure.reviewed_at = timezone.now()
        failure.review_note = bound_text(redact_text(values.get("review_note", ""), max_length=4096), 4096)
        failure.save(update_fields=["taxonomy_code", "subtype", "classifier", "reviewer", "reviewed_at", "review_note", "updated_at"])
        QualityFailureReviewAudit.objects.create(
            failure=failure,
            source_failure_key=failure.dedupe_key,
            actor=request.user,
            previous_taxonomy=previous_taxonomy,
            new_taxonomy=failure.taxonomy_code,
            note=failure.review_note,
        )
        return Response(QualityFailureSerializer(failure).data)


class FailureReviewAuditViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]
    serializer_class = QualityFailureReviewAuditSerializer
    pagination_class = QualityPagination
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        return QualityFailureReviewAudit.objects.filter(
            failure__run__in=_run_queryset(self.request.user)
        ).select_related("failure", "actor").order_by("-created_at")


class ProfileViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]
    serializer_class = QualityTestProfileSerializer
    pagination_class = QualityPagination
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        queryset = _profile_queryset(self.request.user).order_by("-last_seen_at", "-created_at")
        return apply_quality_filters(queryset, self.request.query_params, date_field="last_seen_at")

    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request, pk=None):
        profile = self.get_object()
        queryset = QualityRunItem.objects.filter(
            run__source_type=profile.source_type,
            run__project_type=profile.project_type,
            run__project_id=profile.project_id,
            test_key=profile.test_key,
            environment_fingerprint=profile.environment_fingerprint,
            run__in=_run_queryset(request.user),
        ).order_by("-created_at", "-id")
        return _paginate(self, queryset, QualityRunItemSerializer)

    @action(detail=True, methods=["post"], url_path="quarantine")
    def quarantine(self, request, pk=None):
        profile = self.get_object()
        serializer = QuarantineCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _error("INVALID_QUARANTINE", "invalid quarantine request", status.HTTP_400_BAD_REQUEST, fields=serializer.errors)
        values = serializer.validated_data
        starts_at = values.get("starts_at") or timezone.now()
        expires_at = values.get("expires_at")
        if expires_at is not None and expires_at <= starts_at:
            return _error("INVALID_QUARANTINE", "expires_at must be after starts_at", status.HTTP_400_BAD_REQUEST)
        requires_approval = bool(values.get("requires_approval"))
        approval_status = (
            QualityQuarantineApprovalStatus.PENDING
            if requires_approval else QualityQuarantineApprovalStatus.APPROVED
        )
        now = timezone.now()
        quarantine = QualityQuarantine.objects.create(
            profile=profile,
            status=QualityQuarantineStatus.ACTIVE,
            reason=bound_text(redact_text(values["reason"], max_length=4096), 4096),
            owner=request.user,
            approval_status=approval_status,
            approved_by=None if requires_approval else request.user,
            approved_at=None if requires_approval else now,
            approval_note="" if requires_approval else "created by authenticated operator",
            note=bound_text(redact_text(values.get("note", ""), max_length=4096), 4096),
            created_by=request.user,
            starts_at=starts_at,
            expires_at=expires_at,
        )
        record_quarantine_audit(
            quarantine,
            action="CREATE",
            actor=request.user,
            reason=values["reason"],
            previous_status="",
            new_status=approval_status,
            metadata={"requires_approval": requires_approval},
        )
        if not requires_approval:
            record_quarantine_audit(
                quarantine,
                action="APPROVE",
                actor=request.user,
                reason="created by authenticated operator",
                previous_status="PENDING",
                new_status="APPROVED",
            )
        return Response(QualityQuarantineSerializer(quarantine).data, status=status.HTTP_201_CREATED)


class QuarantineViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]
    serializer_class = QualityQuarantineSerializer
    pagination_class = QualityPagination
    http_method_names = ["get", "head", "options", "post"]

    def get_queryset(self):
        queryset = QualityQuarantine.objects.filter(profile__in=_profile_queryset(self.request.user)).select_related("profile").order_by("-created_at")
        return apply_quality_filters(queryset, self.request.query_params)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        quarantine = self.get_object()
        try:
            quarantine = approve_quarantine(quarantine, request.user, request.data.get("note", ""))
        except QuarantineGovernanceError as exc:
            return _error("QUARANTINE_APPROVAL_INVALID", str(exc), status.HTTP_409_CONFLICT)
        return Response(QualityQuarantineSerializer(quarantine).data)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        quarantine = self.get_object()
        try:
            quarantine = reject_quarantine(quarantine, request.user, request.data.get("note", ""))
        except QuarantineGovernanceError as exc:
            return _error("QUARANTINE_APPROVAL_INVALID", str(exc), status.HTTP_409_CONFLICT)
        return Response(QualityQuarantineSerializer(quarantine).data)

    @action(detail=True, methods=["post"], url_path="release")
    def release(self, request, pk=None):
        quarantine = self.get_object()
        try:
            quarantine = release_quarantine(quarantine, request.user, request.data.get("reason", ""))
        except QuarantineGovernanceError as exc:
            return _error("QUARANTINE_TERMINAL", str(exc), status.HTTP_400_BAD_REQUEST)
        return Response(QualityQuarantineSerializer(quarantine).data)


class QuarantineAuditViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]
    serializer_class = QualityQuarantineAuditSerializer
    pagination_class = QualityPagination
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        queryset = QualityQuarantineAudit.objects.filter(
            profile__in=_profile_queryset(self.request.user)
        ).select_related("quarantine", "profile", "execution_job", "actor").order_by("-created_at")
        return apply_quality_filters(queryset, self.request.query_params, date_field="created_at")


class MetricViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]
    serializer_class = QualityMetricObservationSerializer
    pagination_class = QualityPagination
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        queryset = _metric_queryset(self.request.user).order_by("-created_at")
        return apply_quality_filters(queryset, self.request.query_params)


class BaselineViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]
    pagination_class = QualityPagination
    http_method_names = ["get", "post", "put", "patch", "head", "options"]

    def get_queryset(self):
        queryset = _baseline_queryset(self.request.user).select_related("profile", "source_run").order_by("-updated_at")
        return apply_quality_filters(queryset, self.request.query_params, date_field="updated_at")

    def get_serializer_class(self):
        if self.request.method in {"POST", "PUT", "PATCH"}:
            return QualityBaselineWriteSerializer
        return QualityBaselineSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return _error("INVALID_BASELINE", "invalid baseline", status.HTTP_400_BAD_REQUEST, fields=serializer.errors)
        values = dict(serializer.validated_data)
        profile_id = values.pop("profile_id", None)
        if not _parse_project_access(request.user, values["source_type"], values["project_id"]):
            return _error("PROJECT_FORBIDDEN", "project is not accessible", status.HTTP_404_NOT_FOUND)
        if profile_id is not None:
            profile = _profile_queryset(request.user).filter(
                pk=profile_id,
                source_type=values["source_type"],
                project_type=values["project_type"],
                project_id=values["project_id"],
            ).first()
            if profile is None:
                return _error("PROFILE_NOT_FOUND", "quality profile not found", status.HTTP_404_NOT_FOUND)
            values["profile"] = profile
        values["snapshot"] = redact_payload(values.get("snapshot", {}) or {})
        baseline = QualityBaseline.objects.create(
            **values,
            created_by=request.user,
            updated_by=request.user,
        )
        return Response(QualityBaselineSerializer(baseline).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        baseline = self.get_object()
        serializer = self.get_serializer(baseline, data=request.data, partial=kwargs.pop("partial", False))
        if not serializer.is_valid():
            return _error("INVALID_BASELINE", "invalid baseline", status.HTTP_400_BAD_REQUEST, fields=serializer.errors)
        values = dict(serializer.validated_data)
        has_profile = "profile_id" in values
        profile_id = values.pop("profile_id", None)
        source_type = values.get("source_type", baseline.source_type)
        project_type = values.get("project_type", baseline.project_type)
        project_id = values.get("project_id", baseline.project_id)
        if any(key in values for key in ("project_id", "source_type", "project_type")):
            if not _parse_project_access(request.user, source_type, project_id):
                return _error("PROJECT_FORBIDDEN", "project is not accessible", status.HTTP_404_NOT_FOUND)
        if has_profile:
            if profile_id is None:
                values["profile"] = None
            else:
                profile = _profile_queryset(request.user).filter(
                    pk=profile_id,
                    source_type=source_type,
                    project_type=project_type,
                    project_id=project_id,
                ).first()
                if profile is None:
                    return _error("PROFILE_NOT_FOUND", "quality profile not found", status.HTTP_404_NOT_FOUND)
                values["profile"] = profile
        for key, value in values.items():
            if key == "snapshot":
                value = redact_payload(value or {})
            setattr(baseline, key, value)
        baseline.updated_by = request.user
        baseline.save()
        return Response(QualityBaselineSerializer(baseline).data)


class ComparisonViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]
    serializer_class = QualityComparisonSerializer
    pagination_class = QualityPagination
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        queryset = _comparison_queryset(self.request.user).order_by("-created_at")
        return apply_quality_filters(queryset, self.request.query_params)


@extend_schema(responses=OpenApiTypes.OBJECT)
class SummaryView(APIView):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]

    def get(self, request):
        runs = apply_quality_filters(_run_queryset(request.user), request.query_params)
        run_list = list(runs.only("id", "source_type", "quality_status", "project_id"))
        run_ids = [run.id for run in run_list]
        items = QualityRunItem.objects.filter(run_id__in=run_ids)
        failures = QualityFailure.objects.filter(run_id__in=run_ids)
        by_source: dict[str, int] = defaultdict(int)
        by_quality: dict[str, int] = defaultdict(int)
        by_outcome: dict[str, int] = defaultdict(int)
        by_taxonomy: dict[str, int] = defaultdict(int)
        for run in run_list:
            by_source[run.source_type] += 1
            by_quality[run.quality_status] += 1
        for item in items:
            by_outcome[item.outcome] += 1
        for failure in failures:
            by_taxonomy[failure.taxonomy_code] += 1
        return Response(
            {
                "run_count": len(run_list),
                "item_count": items.count(),
                "failure_count": failures.count(),
                "runs_by_source": dict(by_source),
                "runs_by_quality_status": dict(by_quality),
                "items_by_outcome": dict(by_outcome),
                "failures_by_taxonomy": dict(by_taxonomy),
                "classification_mode": "deterministic",
            }
        )


@extend_schema(responses=OpenApiTypes.OBJECT)
class TrendView(APIView):
    permission_classes = [QualityIntelligenceEnabledPermission, IsAuthenticated]

    def get(self, request):
        runs = apply_quality_filters(_run_queryset(request.user), request.query_params)
        buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"runs": 0, "partial": 0, "failed_items": 0, "passed_items": 0})
        for run in runs.only("id", "created_at", "quality_status"):
            key = run.created_at.date().isoformat() if run.created_at else "unknown"
            buckets[key]["runs"] += 1
            if run.quality_status == "PARTIAL":
                buckets[key]["partial"] += 1
            item_counts = run.items.values("outcome").annotate(count=Count("id"))
            for row in item_counts:
                if row["outcome"] in {"FAIL", "ERROR"}:
                    buckets[key]["failed_items"] += row["count"]
                elif row["outcome"] == "PASS":
                    buckets[key]["passed_items"] += row["count"]
        points = [{"date": key, **buckets[key]} for key in sorted(buckets)]
        return Response({"points": points, "classification_mode": "deterministic"})


__all__ = [
    "BaselineViewSet",
    "ComparisonViewSet",
    "EvidenceViewSet",
    "FailureViewSet",
    "FailureReviewAuditViewSet",
    "IngestView",
    "ItemViewSet",
    "MetricViewSet",
    "ProfileViewSet",
    "QuarantineViewSet",
    "QuarantineAuditViewSet",
    "RunViewSet",
    "SummaryView",
    "TrendView",
    "recompute_run_comparisons",
]
