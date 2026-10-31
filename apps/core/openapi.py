from rest_framework import serializers


class DocsListItemSerializer(serializers.Serializer):
    name = serializers.CharField()
    title = serializers.CharField()
    size = serializers.IntegerField()
    updated_at = serializers.IntegerField(help_text="Unix timestamp in seconds.")


class DocsListResponseSerializer(serializers.Serializer):
    items = DocsListItemSerializer(many=True)
    total = serializers.IntegerField()


class DocsContentResponseSerializer(serializers.Serializer):
    name = serializers.CharField()
    title = serializers.CharField()
    content = serializers.CharField()


class DocsErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()
