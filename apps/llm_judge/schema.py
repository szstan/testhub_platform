"""Typed OpenAPI schemas for the LLM judge endpoints."""
from rest_framework import serializers
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field


class JudgeSimpleErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()


class JudgeUploadErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()
    filename = serializers.CharField(required=False)


class JudgeScoringServiceErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()
    detail = serializers.CharField()


class JudgeRequestParseErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class JudgeAuthErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class JudgeSingleValidationErrorResponseSerializer(serializers.Serializer):
    question = serializers.ListField(child=serializers.CharField(), required=False)
    answer = serializers.ListField(child=serializers.CharField(), required=False)
    ground_truth = serializers.ListField(child=serializers.CharField(), required=False)
    auto_gt = serializers.ListField(child=serializers.CharField(), required=False)
    rubric = serializers.ListField(child=serializers.CharField(), required=False)
    context = serializers.ListField(child=serializers.CharField(), required=False)
    non_field_errors = serializers.ListField(child=serializers.CharField(), required=False)


@extend_schema_field(OpenApiTypes.BINARY)
class JudgeBinaryFileField(serializers.FileField):
    pass


class JudgeUploadRequestSerializer(serializers.Serializer):
    file = JudgeBinaryFileField(
        help_text="CSV, XLSX, XLSM, TXT, or MD file; maximum size 10 MB."
    )


class JudgeBatchParsedCaseSerializer(serializers.Serializer):
    question = serializers.CharField()
    answer = serializers.CharField()
    ground_truth = serializers.JSONField(required=False)
    auto_gt = serializers.BooleanField()
    context = serializers.JSONField(required=False)


class JudgeBatchUploadResponseSerializer(serializers.Serializer):
    filename = serializers.CharField()
    total_rows = serializers.IntegerField()
    valid_rows = serializers.IntegerField()
    errors = serializers.ListField(child=serializers.CharField())
    preview = JudgeBatchParsedCaseSerializer(many=True)
    cases = JudgeBatchParsedCaseSerializer(many=True)


class JudgeRuleFindingSerializer(serializers.Serializer):
    rule = serializers.CharField()
    severity = serializers.ChoiceField(choices=("info", "warn", "critical"))
    message = serializers.CharField()
    detail = serializers.JSONField()


class JudgeRuleReportSerializer(serializers.Serializer):
    findings = JudgeRuleFindingSerializer(many=True)
    rule_score = serializers.FloatField()
    vetoed = serializers.BooleanField()
    veto_reasons = serializers.ListField(child=serializers.CharField())


class JudgeDimensionScoreSerializer(serializers.Serializer):
    id = serializers.CharField()
    score = serializers.FloatField(allow_null=True)
    passed = serializers.BooleanField(allow_null=True)
    reasoning = serializers.CharField()


class JudgeVerdictSerializer(serializers.Serializer):
    reasoning = serializers.CharField()
    dimensions = JudgeDimensionScoreSerializer(many=True)
    overall_label = serializers.ChoiceField(choices=(
        "excellent", "acceptable", "needs_improvement", "critical_failure",
    ))
    model = serializers.CharField()


class JudgeScoreMetaSerializer(serializers.Serializer):
    rule_weight = serializers.FloatField()
    llm_weight = serializers.FloatField()
    veto_reasons = serializers.ListField(child=serializers.CharField())
    latency_ms = serializers.IntegerField()
    cache_hit = serializers.BooleanField()


class JudgeSingleResponseSerializer(serializers.Serializer):
    request_id = serializers.CharField()
    rule_report = JudgeRuleReportSerializer()
    verdict = JudgeVerdictSerializer()
    dimension_scores = serializers.DictField(child=serializers.FloatField())
    llm_score = serializers.FloatField()
    final_score = serializers.FloatField()
    overall_label = serializers.ChoiceField(choices=(
        "excellent", "acceptable", "needs_improvement", "critical_failure",
    ))
    gate_zone = serializers.ChoiceField(choices=("green", "yellow", "red"))
    meta = JudgeScoreMetaSerializer()
    rule_score = serializers.FloatField()
    vetoed = serializers.BooleanField()
    veto_reasons = serializers.ListField(child=serializers.CharField())
    rule_findings = JudgeRuleFindingSerializer(many=True)
    judge_model = serializers.CharField()
    latency_ms = serializers.IntegerField()
    cache_hit = serializers.BooleanField()
    record_id = serializers.IntegerField()


class JudgeServiceConfigResponseSerializer(serializers.Serializer):
    judge_model = serializers.CharField()
    judge_mock = serializers.BooleanField()
    n_runs = serializers.IntegerField()
    rule_llm_fallback = serializers.BooleanField()
    cache_timeout = serializers.IntegerField()
    has_api_key = serializers.BooleanField(
        help_text="True only when a server API key is configured; the key itself is never returned."
    )


class JudgeServiceConnectionTestResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    message = serializers.CharField()


class JudgeDashboardZoneDistributionSerializer(serializers.Serializer):
    green = serializers.IntegerField()
    yellow = serializers.IntegerField()
    red = serializers.IntegerField()


class JudgeDashboardVetoRuleSerializer(serializers.Serializer):
    rule = serializers.CharField()
    count = serializers.IntegerField()


class JudgeDashboardDailyTrendSerializer(serializers.Serializer):
    day = serializers.DateField()
    mean_score = serializers.FloatField()
    cnt = serializers.IntegerField()


class JudgeDashboardLabelDistributionSerializer(serializers.Serializer):
    excellent = serializers.IntegerField(required=False)
    acceptable = serializers.IntegerField(required=False)
    needs_improvement = serializers.IntegerField(required=False)
    critical_failure = serializers.IntegerField(required=False)


class JudgeDashboardStatsResponseSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    vetoed_count = serializers.IntegerField()
    veto_rate = serializers.FloatField()
    pass_rate = serializers.FloatField()
    zone_distribution = JudgeDashboardZoneDistributionSerializer()
    label_distribution = JudgeDashboardLabelDistributionSerializer()
    veto_top5 = JudgeDashboardVetoRuleSerializer(many=True)
    daily_trend = JudgeDashboardDailyTrendSerializer(many=True)
    cache_hit_rate = serializers.FloatField()
    avg_latency_ms = serializers.FloatField()
    mean_score = serializers.FloatField()
    days = serializers.IntegerField()


__all__ = [
    "JudgeAuthErrorResponseSerializer",
    "JudgeBatchParsedCaseSerializer",
    "JudgeBatchUploadResponseSerializer",
    "JudgeDashboardStatsResponseSerializer",
    "JudgeRequestParseErrorResponseSerializer",
    "JudgeScoringServiceErrorResponseSerializer",
    "JudgeSimpleErrorResponseSerializer",
    "JudgeUploadErrorResponseSerializer",
    "JudgeServiceConfigResponseSerializer",
    "JudgeServiceConnectionTestResponseSerializer",
    "JudgeSingleResponseSerializer",
    "JudgeSingleValidationErrorResponseSerializer",
    "JudgeUploadRequestSerializer",
]
