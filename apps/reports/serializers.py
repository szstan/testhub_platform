from rest_framework import serializers

from .models import TestReport


class TestReportSerializer(serializers.ModelSerializer):
    generated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = TestReport
        fields = '__all__'
        read_only_fields = ('created_at', 'generated_by')
