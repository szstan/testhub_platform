from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    BaselineViewSet,
    ComparisonViewSet,
    EvidenceViewSet,
    FailureViewSet,
    FailureClusterViewSet,
    FailureClusterAuditViewSet,
    FailureReviewAuditViewSet,
    IngestView,
    ItemViewSet,
    MetricViewSet,
    ProfileViewSet,
    QuarantineViewSet,
    QuarantineAuditViewSet,
    RunViewSet,
    SummaryView,
    TrendView,
)

app_name = "quality_intelligence"

router = DefaultRouter()
router.register(r"runs", RunViewSet, basename="quality-run")
router.register(r"items", ItemViewSet, basename="quality-item")
router.register(r"evidence", EvidenceViewSet, basename="quality-evidence")
router.register(r"failures", FailureViewSet, basename="quality-failure")
router.register(r"failure-clusters", FailureClusterViewSet, basename="quality-failure-cluster")
router.register(r"failure-cluster-audits", FailureClusterAuditViewSet, basename="quality-failure-cluster-audit")
router.register(r"failure-review-audits", FailureReviewAuditViewSet, basename="quality-failure-review-audit")
router.register(r"profiles", ProfileViewSet, basename="quality-profile")
router.register(r"quarantines", QuarantineViewSet, basename="quality-quarantine")
router.register(r"quarantine-audits", QuarantineAuditViewSet, basename="quality-quarantine-audit")
router.register(r"metrics", MetricViewSet, basename="quality-metric")
router.register(r"baselines", BaselineViewSet, basename="quality-baseline")
router.register(r"comparisons", ComparisonViewSet, basename="quality-comparison")

urlpatterns = [
    path("ingest/", IngestView.as_view(), name="ingest"),
    path("summary/", SummaryView.as_view(), name="summary"),
    path("trend/", TrendView.as_view(), name="trend"),
    path("", include(router.urls)),
]
