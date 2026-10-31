from rest_framework import serializers


class ApiDashboardStatsResponseSerializer(serializers.Serializer):
    project_count = serializers.IntegerField()
    interface_count = serializers.IntegerField()
    suite_count = serializers.IntegerField()
    history_count = serializers.IntegerField()


class ApiDashboardAuthErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
