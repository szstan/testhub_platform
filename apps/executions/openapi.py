from drf_spectacular.types import OpenApiTypes
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.utils import OpenApiParameter, extend_schema_field
from rest_framework import serializers

from apps.executions.serializers import ExecutionArtifactSerializer
from apps.quality_intelligence.serializers import QualityEvidenceSerializer


EXECUTION_METRICS_PARAMETERS = [
    OpenApiParameter(
        "days", {"type": "integer", "minimum": 1, "maximum": 365},
        OpenApiParameter.QUERY, required=False, default=30,
        description="Lookback window in days (1-365).",
    ),
    OpenApiParameter(
        "project", {"type": "integer", "minimum": 1}, OpenApiParameter.QUERY,
        description="Restrict metrics to a project the authenticated user can access.",
    ),
]

EXECUTION_EVIDENCE_PARAMETERS = [
    OpenApiParameter("project", {"type": "integer", "minimum": 1}, OpenApiParameter.QUERY),
    OpenApiParameter("job", {"type": "integer", "minimum": 1}, OpenApiParameter.QUERY),
    OpenApiParameter("attempt", {"type": "integer", "minimum": 1}, OpenApiParameter.QUERY),
    OpenApiParameter("quality_run", {"type": "integer", "minimum": 1}, OpenApiParameter.QUERY),
    OpenApiParameter("case_key", OpenApiTypes.STR, OpenApiParameter.QUERY),
    OpenApiParameter("media_type", OpenApiTypes.STR, OpenApiParameter.QUERY,
                     description="Execution-artifact media type filter."),
    OpenApiParameter("evidence_type", OpenApiTypes.STR, OpenApiParameter.QUERY,
                     description="Quality-evidence type filter."),
    OpenApiParameter("availability", OpenApiTypes.STR, OpenApiParameter.QUERY,
                     description="Quality-evidence availability filter."),
    OpenApiParameter("limit", {"type": "integer", "minimum": 1, "maximum": 200},
                     OpenApiParameter.QUERY, default=100, description="Legacy shared page-size default."),
    OpenApiParameter("page_size", {"type": "integer", "minimum": 1, "maximum": 200},
                     OpenApiParameter.QUERY, default=100, description="Shared page size; defaults to limit."),
    OpenApiParameter("page", {"type": "integer", "minimum": 1, "maximum": 1000000},
                     OpenApiParameter.QUERY, default=1, description="Shared page; each evidence stream can override it."),
    OpenApiParameter("artifact_page", {"type": "integer", "minimum": 1, "maximum": 1000000},
                     OpenApiParameter.QUERY, description="Execution-artifact page; defaults to page."),
    OpenApiParameter("quality_page", {"type": "integer", "minimum": 1, "maximum": 1000000},
                     OpenApiParameter.QUERY, description="Quality-evidence page; defaults to page."),
    OpenApiParameter("artifact_page_size", {"type": "integer", "minimum": 1, "maximum": 200},
                     OpenApiParameter.QUERY, description="Execution-artifact page size; defaults to page_size."),
    OpenApiParameter("quality_page_size", {"type": "integer", "minimum": 1, "maximum": 200},
                     OpenApiParameter.QUERY, description="Quality-evidence page size; defaults to page_size."),
]

CI_WEBHOOK_PARAMETERS = [
    OpenApiParameter("provider", OpenApiTypes.STR, OpenApiParameter.PATH,
                     enum=("github", "gitlab", "jenkins", "generic"),
                     description="Configured CI provider."),
    OpenApiParameter("X-TestHub-Profile", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True,
                     description="Enabled webhook profile identifier."),
    OpenApiParameter("X-TestHub-Event", OpenApiTypes.STR, OpenApiParameter.HEADER,
                     description="Required event ID; use this or one of X-GitHub-Delivery, X-Gitlab-Event-UUID, or X-Request-ID."),
    OpenApiParameter("X-GitHub-Delivery", OpenApiTypes.STR, OpenApiParameter.HEADER),
    OpenApiParameter("X-Gitlab-Event-UUID", OpenApiTypes.STR, OpenApiParameter.HEADER),
    OpenApiParameter("X-Request-ID", OpenApiTypes.STR, OpenApiParameter.HEADER),
    OpenApiParameter("X-Hub-Signature-256", OpenApiTypes.STR, OpenApiParameter.HEADER,
                     description="For GitHub, send this or X-TestHub-Signature."),
    OpenApiParameter("X-TestHub-Signature", OpenApiTypes.STR, OpenApiParameter.HEADER,
                     description="Required for non-GitHub providers; for GitHub it is an alternative signature header. Sign timestamp + '.' + raw body for non-GitHub."),
    OpenApiParameter("X-TestHub-Timestamp", OpenApiTypes.STR, OpenApiParameter.HEADER,
                     description="Required for non-GitHub providers; timestamp must be within five minutes."),
]


class ScopedCITokenAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "apps.users.authentication.CITokenAuthentication"
    name = "ciServiceBearerAuth"

    def get_security_requirement(self, auto_schema):
        if auto_schema.path.rstrip("/") != "/api/executions/import-results":
            return None
        return super().get_security_requirement(auto_schema)

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "TestHub CI service token",
            "description": "Scoped to the execution jobs and result-import endpoints.",
        }


class ErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField(required=False)
    error_code = serializers.CharField(required=False)
    fields = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()), required=False,
    )


class ImportErrorResponseSerializer(ErrorResponseSerializer):
    project_id = serializers.ListField(child=serializers.CharField(), required=False)


class QueryValidationErrorSerializer(ErrorResponseSerializer):
    days = serializers.ListField(child=serializers.CharField(), required=False)
    project = serializers.ListField(child=serializers.CharField(), required=False)
    limit = serializers.ListField(child=serializers.CharField(), required=False)
    page_size = serializers.ListField(child=serializers.CharField(), required=False)
    page = serializers.ListField(child=serializers.CharField(), required=False)
    artifact_page = serializers.ListField(child=serializers.CharField(), required=False)
    quality_page = serializers.ListField(child=serializers.CharField(), required=False)
    artifact_page_size = serializers.ListField(child=serializers.CharField(), required=False)
    quality_page_size = serializers.ListField(child=serializers.CharField(), required=False)
    quality_run = serializers.ListField(child=serializers.CharField(), required=False)
    job = serializers.ListField(child=serializers.CharField(), required=False)
    attempt = serializers.ListField(child=serializers.CharField(), required=False)


class BinaryUploadField(serializers.FileField):
    pass


extend_schema_field(OpenApiTypes.BINARY)(BinaryUploadField)


class ResultImportRequestSerializer(serializers.Serializer):
    project_id = serializers.IntegerField(min_value=1)
    format = serializers.ChoiceField(
        choices=("junit", "allure"),
        help_text="Case-insensitive; supported result formats are junit and allure.",
    )
    external_run_id = serializers.CharField(
        help_text="Required external run identifier; surrounding whitespace is removed and the value is limited to 255 characters.",
    )
    file = BinaryUploadField(help_text="JUnit XML or Allure results archive; maximum size is 20 MiB.")
    build_reference = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional build reference; surrounding whitespace is removed and the value is limited to 255 characters.",
    )


class ResultImportResponseSerializer(serializers.Serializer):
    job_id = serializers.IntegerField()
    created = serializers.BooleanField()
    format = serializers.ChoiceField(choices=("junit", "allure"))
    external_run_id = serializers.CharField()
    total = serializers.IntegerField()
    passed = serializers.IntegerField()
    failed = serializers.IntegerField()
    skipped = serializers.IntegerField()
    matched = serializers.IntegerField()
    unmatched = serializers.IntegerField()
    status = serializers.CharField()


class DynamicObjectField(serializers.JSONField):
    pass


extend_schema_field(OpenApiTypes.OBJECT)(DynamicObjectField)


class ExecutionArtifactResponseSerializer(ExecutionArtifactSerializer):
    metadata = DynamicObjectField()


class QualityEvidenceResponseSerializer(QualityEvidenceSerializer):
    provenance = DynamicObjectField()


class EvidenceCountsSerializer(serializers.Serializer):
    execution_artifacts = serializers.IntegerField()
    quality_evidence = serializers.IntegerField()


class EvidencePageSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()
    has_next = serializers.BooleanField()


class EvidencePaginationSerializer(serializers.Serializer):
    execution_artifacts = EvidencePageSerializer()
    quality_evidence = EvidencePageSerializer()


class ExecutionEvidenceExplorerResponseSerializer(serializers.Serializer):
    execution_artifacts = ExecutionArtifactResponseSerializer(many=True)
    quality_evidence = QualityEvidenceResponseSerializer(many=True)
    counts = EvidenceCountsSerializer()
    pagination = EvidencePaginationSerializer()


class RecentRunSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    project = serializers.IntegerField()
    source_type = serializers.CharField()
    source_id = serializers.CharField()
    build_reference = serializers.CharField()
    status = serializers.CharField()
    quality_gate = DynamicObjectField()
    created_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)
    error_message = serializers.CharField()


class RouteReadinessSerializer(serializers.Serializer):
    api = serializers.BooleanField()
    ui = serializers.BooleanField()
    app = serializers.BooleanField()
    performance = serializers.BooleanField()


class EnabledBindingsSerializer(serializers.Serializer):
    api = serializers.IntegerField()
    ui = serializers.IntegerField()
    app = serializers.IntegerField()
    performance = serializers.IntegerField()


class LastWebhookSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    provider = serializers.CharField()
    status = serializers.CharField()
    received_at = serializers.DateTimeField()
    job_id = serializers.IntegerField(allow_null=True)


class LastCIJobSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    status = serializers.CharField()
    created_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)


class IntegrationHealthSerializer(serializers.Serializer):
    webhook_profiles = serializers.IntegerField()
    last_webhook = LastWebhookSerializer(allow_null=True)
    last_ci_job = LastCIJobSerializer(allow_null=True)


class ScaleThresholdsSerializer(serializers.Serializer):
    execution_jobs = serializers.IntegerField()
    execution_artifacts = serializers.IntegerField()
    execution_artifact_bytes = serializers.IntegerField()
    quality_runs = serializers.IntegerField()
    quality_evidence = serializers.IntegerField()


class ScaleSerializer(serializers.Serializer):
    execution_jobs = serializers.IntegerField()
    execution_artifacts = serializers.IntegerField()
    execution_artifact_bytes = serializers.IntegerField()
    expired_execution_artifacts = serializers.IntegerField()
    execution_oldest_created_at = serializers.DateTimeField(allow_null=True)
    execution_latest_created_at = serializers.DateTimeField(allow_null=True)
    quality_runs = serializers.IntegerField()
    quality_evidence = serializers.IntegerField()
    quality_failures = serializers.IntegerField()
    quality_failure_clusters = serializers.IntegerField()
    unmapped_quality_runs = serializers.IntegerField()
    unmapped_quality_clusters = serializers.IntegerField()
    thresholds = ScaleThresholdsSerializer()
    warnings = serializers.ListField(child=serializers.CharField())


class ExecutionMetricsResponseSerializer(serializers.Serializer):
    window_days = serializers.IntegerField()
    total_jobs = serializers.IntegerField()
    ci_jobs = serializers.IntegerField()
    ci_association_rate = serializers.FloatField()
    unmatched_results = serializers.IntegerField()
    gate_evaluations = serializers.IntegerField()
    gate_pass_rate = serializers.FloatField()
    failure_categories = serializers.DictField(child=serializers.IntegerField())
    flaky_profiles = serializers.IntegerField()
    quality_intelligence_enabled = serializers.BooleanField(read_only=True)
    recent_runs = RecentRunSerializer(many=True)
    route_readiness = RouteReadinessSerializer()
    enabled_bindings = EnabledBindingsSerializer()
    integration_health = IntegrationHealthSerializer()
    scale = ScaleSerializer()


class CIWebhookResponseSerializer(serializers.Serializer):
    event_id = serializers.IntegerField()
    created = serializers.BooleanField()
    job_id = serializers.IntegerField(allow_null=True)
    status = serializers.CharField()
