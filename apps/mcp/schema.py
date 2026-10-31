"""Typed OpenAPI schemas for MCP management endpoints."""
from rest_framework import serializers
from drf_spectacular.utils import (
    OpenApiResponse, PolymorphicProxySerializer, extend_schema_field,
)


class McpAuthErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class McpSchemaErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()


class McpToolAnnotationsSerializer(serializers.Serializer):
    title = serializers.CharField()
    readOnlyHint = serializers.BooleanField()
    destructiveHint = serializers.BooleanField()
    idempotentHint = serializers.BooleanField()
    openWorldHint = serializers.BooleanField()


class McpToolStatsSerializer(serializers.Serializer):
    calls = serializers.IntegerField()
    success = serializers.IntegerField()
    success_rate = serializers.FloatField(allow_null=True)
    avg_duration_ms = serializers.IntegerField(allow_null=True)


class McpToolCatalogEntrySerializer(serializers.Serializer):
    name = serializers.CharField()
    title = serializers.CharField()
    category = serializers.ChoiceField(choices=("read", "preview", "confirm", "approval"))
    domain = serializers.CharField()
    summary = serializers.CharField()
    annotations = McpToolAnnotationsSerializer()
    input_schema = serializers.JSONField(
        help_text="Tool-specific JSON Schema generated from the registered MCP tool."
    )
    paired_with = serializers.CharField()
    examples = serializers.ListField(child=serializers.JSONField())
    stats = McpToolStatsSerializer()


class McpToolDetailResponseSerializer(McpToolCatalogEntrySerializer):
    description = serializers.CharField()


class McpToolCategoryCountsSerializer(serializers.Serializer):
    read = serializers.IntegerField()
    preview = serializers.IntegerField()
    confirm = serializers.IntegerField()
    approval = serializers.IntegerField()


class McpCatalogSummarySerializer(serializers.Serializer):
    total = serializers.IntegerField()
    by_category = McpToolCategoryCountsSerializer()
    calls_7d = serializers.IntegerField()


class McpToolCatalogResponseSerializer(serializers.Serializer):
    summary = McpCatalogSummarySerializer()
    tools = McpToolCatalogEntrySerializer(many=True)


@extend_schema_field({
    "type": "object",
    "description": "The authenticated user's own MCP API-key header.",
    "properties": {
        "x-mcp-api-key": {"type": "string"},
    },
    "required": ["x-mcp-api-key"],
    "additionalProperties": False,
})
class McpApiKeyHeaderField(serializers.DictField):
    pass


@extend_schema_field({
    "type": "object",
    "description": "An Authorization header containing a JWT placeholder.",
    "properties": {
        "Authorization": {"type": "string"},
    },
    "required": ["Authorization"],
    "additionalProperties": False,
})
class McpJwtHeaderField(serializers.DictField):
    pass


class McpAuthHeaderMapSerializer(serializers.Serializer):
    api_key = McpApiKeyHeaderField(child=serializers.CharField())
    jwt = McpJwtHeaderField(child=serializers.CharField())


class McpConnectionConfigResponseSerializer(serializers.Serializer):
    server_name = serializers.ChoiceField(choices=("TestHub",))
    endpoint = serializers.URLField()
    tool_count = serializers.IntegerField()
    api_key = serializers.CharField(
        help_text=(
            "The authenticated user's own long-lived DRF token. This endpoint does not return "
            "server or provider credentials."
        )
    )
    auth_headers = McpAuthHeaderMapSerializer()


class McpPendingApproveResponseSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=("approved",))
    result = serializers.JSONField(
        help_text="Result returned by the approved tool; its structure depends on that tool."
    )


class McpPendingRejectResponseSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=("rejected",))


def mcp_forbidden_response():
    return OpenApiResponse(response=PolymorphicProxySerializer(
        component_name="McpForbiddenResponse",
        serializers=[McpAuthErrorResponseSerializer, McpSchemaErrorResponseSerializer],
        resource_type_field_name=None,
    ))


__all__ = [
    "McpAuthErrorResponseSerializer",
    "McpConnectionConfigResponseSerializer",
    "McpPendingApproveResponseSerializer",
    "McpPendingRejectResponseSerializer",
        "McpSchemaErrorResponseSerializer",
    "McpToolCatalogResponseSerializer",
    "McpToolDetailResponseSerializer",
    "mcp_forbidden_response",
]
