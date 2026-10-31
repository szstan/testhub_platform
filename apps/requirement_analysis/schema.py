from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


class RequirementBinaryUploadField(serializers.FileField):
    pass


extend_schema_field(OpenApiTypes.BINARY)(RequirementBinaryUploadField)


class RequirementUploadRequestSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    file = RequirementBinaryUploadField()
    project = serializers.IntegerField(required=False, allow_null=True)


class RequirementAnalyzeTextRequestSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    description = serializers.CharField()
    project = serializers.IntegerField(required=False, allow_null=True)


class RequirementQueuedAnalysisResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    document_id = serializers.IntegerField()
    task_id = serializers.CharField()


class RequirementAnalyzeTextErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()


class RequirementAuthFailureResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class RequirementUploadFieldErrorsSerializer(serializers.Serializer):
    title = serializers.ListField(child=serializers.CharField(), required=False)
    file = serializers.ListField(child=serializers.CharField(), required=False)
    project = serializers.ListField(child=serializers.CharField(), required=False)


class RequirementUploadValidationErrorResponseSerializer(serializers.Serializer):
    error = RequirementUploadFieldErrorsSerializer()


class RequirementWriterModelStatusSerializer(serializers.Serializer):
    configured = serializers.BooleanField()
    enabled = serializers.BooleanField()
    name = serializers.CharField(allow_null=True)
    provider = serializers.CharField(allow_null=True)
    id = serializers.IntegerField(allow_null=True)
    required = serializers.BooleanField()


class RequirementReviewerModelStatusSerializer(serializers.Serializer):
    configured = serializers.BooleanField()
    enabled = serializers.BooleanField()
    name = serializers.CharField(allow_null=True)
    id = serializers.IntegerField(allow_null=True)
    required = serializers.BooleanField()


class RequirementConfigPromptStatusSerializer(serializers.Serializer):
    configured = serializers.BooleanField()
    enabled = serializers.BooleanField()
    name = serializers.CharField(allow_null=True)
    id = serializers.IntegerField(allow_null=True)
    required = serializers.BooleanField()


class RequirementConfigGenerationStatusSerializer(serializers.Serializer):
    configured = serializers.BooleanField()
    enabled = serializers.BooleanField()
    name = serializers.CharField(allow_null=True)
    id = serializers.IntegerField(allow_null=True)
    required = serializers.BooleanField()
    default_output_mode = serializers.ChoiceField(
        choices=('stream', 'complete'), allow_null=True
    )
    enable_auto_review = serializers.BooleanField(allow_null=True)


class RequirementConfigStatusResponseSerializer(serializers.Serializer):
    overall_status = serializers.ChoiceField(
        choices=('enabled', 'disabled', 'not_configured')
    )
    message = serializers.CharField()
    writer_model = RequirementWriterModelStatusSerializer()
    writer_prompt = RequirementConfigPromptStatusSerializer()
    reviewer_model = RequirementReviewerModelStatusSerializer()
    reviewer_prompt = RequirementConfigPromptStatusSerializer()
    generation_config = RequirementConfigGenerationStatusSerializer()


class RequirementConfigStatusErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()
