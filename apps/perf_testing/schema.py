from rest_framework import serializers
from drf_spectacular.utils import extend_schema_serializer

from .models import PerfExecution, PerfScenario
from .serializers import PerfMetricSampleSerializer


class PerfEngineAuthErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class PerfEngineStatusItemSerializer(serializers.Serializer):
    name = serializers.ChoiceField(choices=PerfScenario.ENGINE_CHOICES)
    label = serializers.CharField()
    available = serializers.BooleanField()
    version = serializers.CharField(allow_null=True)
    description = serializers.CharField()


class PerfEngineStatusLimitsSerializer(serializers.Serializer):
    max_concurrency = serializers.IntegerField()
    max_target_rps = serializers.IntegerField()
    max_duration = serializers.IntegerField()
    max_concurrent_executions = serializers.IntegerField()


class PerfEngineStatusResponseSerializer(serializers.Serializer):
    engines = PerfEngineStatusItemSerializer(many=True)
    websocket = serializers.BooleanField()
    limits = PerfEngineStatusLimitsSerializer()


class PerfComparisonAuthErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class PerfComparisonErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()


class PerfComparisonLoadStageSerializer(serializers.Serializer):
    duration = serializers.FloatField(allow_null=True, required=False)
    target = serializers.IntegerField(allow_null=True, required=False)


class PerfComparisonLoadSnapshotSerializer(serializers.Serializer):
    model = serializers.ChoiceField(choices=('CONCURRENCY', 'RAMPING', 'RPS', 'SPIKE'))
    duration = serializers.FloatField(allow_null=True, required=False)
    max_requests = serializers.IntegerField(allow_null=True, required=False)
    concurrency = serializers.IntegerField(allow_null=True, required=False)
    ramp_up = serializers.FloatField(allow_null=True, required=False)
    stages = PerfComparisonLoadStageSerializer(many=True, allow_null=True, required=False)
    target_rps = serializers.IntegerField(allow_null=True, required=False)
    max_concurrency = serializers.IntegerField(allow_null=True, required=False)
    baseline_concurrency = serializers.IntegerField(allow_null=True, required=False)
    spike_concurrency = serializers.IntegerField(allow_null=True, required=False)
    spike_duration = serializers.FloatField(allow_null=True, required=False)
    spike_times = serializers.IntegerField(allow_null=True, required=False)
    _planned_duration = serializers.FloatField()


class PerfComparisonMetricValuesSerializer(serializers.Serializer):
    total_requests = serializers.IntegerField(allow_null=True)
    tps = serializers.FloatField(allow_null=True)
    peak_tps = serializers.FloatField(allow_null=True)
    avg_rt = serializers.FloatField(allow_null=True)
    p90_rt = serializers.FloatField(allow_null=True)
    p95_rt = serializers.FloatField(allow_null=True)
    p99_rt = serializers.FloatField(allow_null=True)
    max_rt = serializers.FloatField(allow_null=True)
    error_rate = serializers.FloatField(allow_null=True)


class PerfComparisonMetricDeltasSerializer(serializers.Serializer):
    total_requests = serializers.FloatField(allow_null=True)
    tps = serializers.FloatField(allow_null=True)
    peak_tps = serializers.FloatField(allow_null=True)
    avg_rt = serializers.FloatField(allow_null=True)
    p90_rt = serializers.FloatField(allow_null=True)
    p95_rt = serializers.FloatField(allow_null=True)
    p99_rt = serializers.FloatField(allow_null=True)
    max_rt = serializers.FloatField(allow_null=True)
    error_rate = serializers.FloatField(allow_null=True)


class PerfComparisonExecutionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    execution_no = serializers.CharField()
    scenario_name = serializers.CharField()
    status = serializers.ChoiceField(choices=PerfExecution.STATUS_CHOICES)
    sla_result = serializers.ChoiceField(choices=PerfExecution.SLA_RESULT_CHOICES)
    created_at = serializers.DateTimeField(allow_null=True)
    duration = serializers.FloatField(allow_null=True)
    load_snapshot = PerfComparisonLoadSnapshotSerializer()
    is_reference = serializers.BooleanField()
    summary = PerfComparisonMetricValuesSerializer()
    delta_pct = PerfComparisonMetricDeltasSerializer()
    samples = PerfMetricSampleSerializer(many=True)


class PerfComparisonStepValueSerializer(serializers.Serializer):
    execution_no = serializers.CharField()
    tps = serializers.FloatField(allow_null=True, required=False)
    avg_rt = serializers.FloatField(allow_null=True, required=False)
    p95_rt = serializers.FloatField(allow_null=True, required=False)
    error_rate = serializers.FloatField(allow_null=True, required=False)


class PerfComparisonStepSerializer(serializers.Serializer):
    step_name = serializers.CharField()
    values = PerfComparisonStepValueSerializer(many=True)


class PerfComparisonSnapshotSerializer(serializers.Serializer):
    baseline_execution_no = serializers.CharField()
    reference_execution_id = serializers.IntegerField()
    metric_keys = serializers.ListField(
        child=serializers.ChoiceField(choices=(
            'total_requests', 'tps', 'peak_tps', 'avg_rt', 'p90_rt',
            'p95_rt', 'p99_rt', 'max_rt', 'error_rate',
        ))
    )
    executions = PerfComparisonExecutionSerializer(many=True)
    step_comparison = PerfComparisonStepSerializer(many=True)


class PerfComparisonReportBriefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField()
    project_id = serializers.IntegerField()
    execution_ids = serializers.ListField(child=serializers.IntegerField())
    reference_execution_id = serializers.IntegerField(allow_null=True)
    has_ai_analysis = serializers.BooleanField()
    created_by = serializers.CharField()
    created_at = serializers.DateTimeField()


class PerfComparisonReportDetailSerializer(PerfComparisonReportBriefSerializer):
    snapshot = PerfComparisonSnapshotSerializer()
    ai_analysis = serializers.CharField(allow_null=True)


@extend_schema_serializer(many=False)
class PerfComparisonReportListResponseSerializer(serializers.Serializer):
    items = PerfComparisonReportBriefSerializer(many=True)
    total = serializers.IntegerField()


class PerfComparisonReportCreateRequestSerializer(serializers.Serializer):
    execution_ids = serializers.ListField(
        child=serializers.IntegerField(), min_length=2, max_length=5
    )
    project_id = serializers.IntegerField(required=False, allow_null=True)
    reference_execution_id = serializers.IntegerField(required=False, allow_null=True)
    title = serializers.CharField(required=False, allow_blank=True)
    ai_analyze = serializers.BooleanField(required=False)
