from rest_framework import serializers

from .serializers import AppTestConfigSerializer, AppTestExecutionSerializer


class AppAutomationAuthErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class AppAutomationFailureResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    message = serializers.CharField()


class AppConfigValidationFailureResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    message = serializers.CharField()
    errors = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField())
    )


class AppConfigCurrentSuccessResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    data = AppTestConfigSerializer()


class AppConfigSaveSuccessResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    message = serializers.CharField()
    data = AppTestConfigSerializer()


class AppDashboardDeviceCountsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    online = serializers.IntegerField()
    locked = serializers.IntegerField()
    available = serializers.IntegerField()


class AppDashboardTestCaseCountsSerializer(serializers.Serializer):
    total = serializers.IntegerField()


class AppDashboardExecutionCountsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    success = serializers.IntegerField()
    failed = serializers.IntegerField()
    pass_rate = serializers.FloatField()


class AppDashboardDataSerializer(serializers.Serializer):
    devices = AppDashboardDeviceCountsSerializer()
    test_cases = AppDashboardTestCaseCountsSerializer()
    executions = AppDashboardExecutionCountsSerializer()
    recent_executions = AppTestExecutionSerializer(many=True)


class AppDashboardStatisticsResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    data = AppDashboardDataSerializer()
