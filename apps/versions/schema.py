from rest_framework import serializers

from apps.users.schema import UsersSessionEmailField


class VersionsAuthFailureResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class VersionsProjectAccessErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()


class VersionsProjectSummaryResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class VersionsCreatedByResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = UsersSessionEmailField(allow_blank=True)
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    avatar = serializers.CharField(allow_null=True)
    phone = serializers.CharField(allow_null=True)
    department = serializers.CharField(allow_null=True)
    position = serializers.CharField(allow_null=True)
    is_active = serializers.BooleanField()
    date_joined = serializers.DateTimeField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class VersionsProjectVersionResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    description = serializers.CharField()
    is_baseline = serializers.BooleanField()
    projects = VersionsProjectSummaryResponseSerializer(many=True)
    created_by = VersionsCreatedByResponseSerializer()
    created_at = serializers.DateTimeField()
    testcases_count = serializers.IntegerField()
