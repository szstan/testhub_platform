from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CIWebhookView, ResultImportView, ExecutionMetricsView
from .views import (
    ExecutionArtifactViewSet,
    ExecutionAttemptViewSet,
    ExecutionBuildViewSet,
    ExecutionDataMatrixViewSet,
    ExecutionDatasetViewSet,
    ExecutionDefectLinkViewSet,
    ExecutionEventViewSet,
    ExecutionFixtureViewSet,
    ExecutionJobViewSet,
    ExecutionJobVariantViewSet,
    ExecutionPluginManifestViewSet,
    ExecutionRequirementLinkViewSet,
    ExecutionProfileViewSet,
    ExecutionRetryPolicyViewSet,
    ExecutionQualityGateEvaluationViewSet,
    ExecutionAISuggestionViewSet,
    ExecutionAISuggestionAuditViewSet,
    ExecutionFailureReviewViewSet,
    ExecutionStepResultViewSet,
    WebhookEventViewSet,
    RequirementTestCaseLinkViewSet,
    TestCaseDefectLinkViewSet,
    TestCaseRevisionViewSet,
    TestPlanViewSet,
    TestRunViewSet,
    TestRunCaseViewSet,
    TestRunCaseHistoryViewSet,
)

router = DefaultRouter()
router.register(r'plans', TestPlanViewSet)
router.register(r'runs', TestRunViewSet)
router.register(r'run_cases', TestRunCaseViewSet)
router.register(r'history', TestRunCaseHistoryViewSet)
router.register(r'jobs', ExecutionJobViewSet, basename='execution-job')
router.register(r'job-variants', ExecutionJobVariantViewSet, basename='execution-job-variant')
router.register(r'attempts', ExecutionAttemptViewSet, basename='execution-attempt')
router.register(r'step-results', ExecutionStepResultViewSet, basename='execution-step-result')
router.register(r'artifacts', ExecutionArtifactViewSet, basename='execution-artifact')
router.register(r'builds', ExecutionBuildViewSet, basename='execution-build')
router.register(r'requirement-testcase-links', RequirementTestCaseLinkViewSet, basename='requirement-testcase-link')
router.register(r'testcase-defect-links', TestCaseDefectLinkViewSet, basename='testcase-defect-link')
router.register(r'execution-requirement-links', ExecutionRequirementLinkViewSet, basename='execution-requirement-link')
router.register(r'execution-defect-links', ExecutionDefectLinkViewSet, basename='execution-defect-link')
router.register(r'events', ExecutionEventViewSet, basename='execution-event')
router.register(r'testcase-revisions', TestCaseRevisionViewSet, basename='testcase-revision')
router.register(r'profiles', ExecutionProfileViewSet, basename='execution-profile')
router.register(r'retry-policies', ExecutionRetryPolicyViewSet, basename='execution-retry-policy')
router.register(r'quality-gate-evaluations', ExecutionQualityGateEvaluationViewSet, basename='execution-quality-gate-evaluation')
router.register(r'ai-suggestions', ExecutionAISuggestionViewSet, basename='execution-ai-suggestion')
router.register(r'ai-suggestion-audits', ExecutionAISuggestionAuditViewSet, basename='execution-ai-suggestion-audit')
router.register(r'failure-reviews', ExecutionFailureReviewViewSet, basename='execution-failure-review')
router.register(r'datasets', ExecutionDatasetViewSet, basename='execution-dataset')
router.register(r'data-matrices', ExecutionDataMatrixViewSet, basename='execution-data-matrix')
router.register(r'fixtures', ExecutionFixtureViewSet, basename='execution-fixture')
router.register(r'plugins', ExecutionPluginManifestViewSet, basename='execution-plugin')
router.register(r'webhook-events', WebhookEventViewSet, basename='webhook-event')

urlpatterns = [
    path('webhooks/<str:provider>/', CIWebhookView.as_view(), name='ci-webhook'),
    path('import-results/', ResultImportView.as_view(), name='result-import'),
    path('metrics/', ExecutionMetricsView.as_view(), name='execution-metrics'),
    path('', include(router.urls)),
]
