from rest_framework import serializers


class TestCaseSyncProfileSyncResponseSerializer(serializers.Serializer):
    profile_id = serializers.IntegerField()
    provider = serializers.ChoiceField(choices=('github', 'gitlab'))
    commit = serializers.CharField()
    files = serializers.IntegerField()
    created = serializers.IntegerField()
    updated = serializers.IntegerField()
    skipped = serializers.IntegerField()
    conflicts = serializers.IntegerField()
    warnings = serializers.ListField(child=serializers.CharField())


class TestCaseSyncProfileSyncErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()


class TestCaseDownloadNotFoundResponseSerializer(serializers.Serializer):
    error = serializers.CharField()


class TestCaseAuthFailureResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
