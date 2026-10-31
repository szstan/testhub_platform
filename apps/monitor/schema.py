from rest_framework import serializers
from drf_spectacular.utils import extend_schema_serializer

from .models import TargetType


class MonitorDashboardAuthErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class MonitorSchedulerStatusResponseSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=('online', 'offline', 'unknown'))


class MonitorDashboardSummarySerializer(serializers.Serializer):
    total = serializers.IntegerField()
    enabled = serializers.IntegerField()
    disabled = serializers.IntegerField()
    up = serializers.IntegerField()
    down = serializers.IntegerField()
    unknown = serializers.IntegerField()
    availability = serializers.FloatField(allow_null=True)
    active_alerts = serializers.IntegerField()


class MonitorDashboardTypeCountsSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=TargetType.CHOICES)
    label = serializers.CharField()
    total = serializers.IntegerField()
    UP = serializers.IntegerField()
    DOWN = serializers.IntegerField()
    UNKNOWN = serializers.IntegerField()


class MonitorDashboardTrendPointSerializer(serializers.Serializer):
    ts = serializers.DateTimeField()
    up = serializers.IntegerField()
    down = serializers.IntegerField()
    availability = serializers.FloatField(allow_null=True)


class MonitorDashboardFailureSerializer(serializers.Serializer):
    target_id = serializers.IntegerField()
    name = serializers.CharField()
    type = serializers.ChoiceField(choices=TargetType.CHOICES)
    checked_at = serializers.DateTimeField(allow_null=True)
    latency_ms = serializers.IntegerField(allow_null=True)
    message = serializers.CharField()


@extend_schema_serializer(many=False)
class MonitorDashboardResponseSerializer(serializers.Serializer):
    summary = MonitorDashboardSummarySerializer()
    by_type = MonitorDashboardTypeCountsSerializer(many=True)
    trend = MonitorDashboardTrendPointSerializer(many=True)
    recent_failures = MonitorDashboardFailureSerializer(many=True)
