from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.quality_intelligence.models import QualitySourceType
from apps.quality_intelligence.serializers import QualityRunSerializer


class DynamicObjectField(serializers.JSONField):
    pass


extend_schema_field(OpenApiTypes.OBJECT)(DynamicObjectField)


class QualityRunResponseSerializer(QualityRunSerializer):
    summary = DynamicObjectField()
    provenance = DynamicObjectField()
    warnings = serializers.ListField(child=serializers.CharField())


class IngestRequestSerializer(serializers.Serializer):
    source_type = serializers.ChoiceField(choices=QualitySourceType.choices)
    source_execution_id = serializers.CharField(max_length=128, trim_whitespace=True)


class IngestionResultSerializer(serializers.Serializer):
    source_type = serializers.ChoiceField(choices=QualitySourceType.choices)
    source_execution_id = serializers.CharField()
    quality_run_id = serializers.IntegerField()
    quality_status = serializers.CharField()
    created = serializers.BooleanField()
    updated = serializers.BooleanField()
    reused = serializers.BooleanField()
    source_missing = serializers.BooleanField()
    dry_run = serializers.BooleanField()
    warnings = serializers.ListField(child=serializers.CharField())


class IngestResponseSerializer(serializers.Serializer):
    ingestion = IngestionResultSerializer()
    run = QualityRunResponseSerializer()


class IngestErrorResponseSerializer(serializers.Serializer):
    error_code = serializers.CharField(required=False)
    detail = serializers.CharField(required=False)
    fields = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()), required=False,
    )
