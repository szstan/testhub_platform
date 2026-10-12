from rest_framework import serializers

from apps.projects.models import Project
from apps.users.serializers import UserSimpleSerializer
from apps.testcases.models import TestCase

from .models import TestSuite, TestSuiteCase


class ProjectSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ('id', 'name')


class TestCaseSummarySerializer(serializers.ModelSerializer):
    """The small, stable representation used when displaying suite members."""

    class Meta:
        model = TestCase
        fields = (
            'id', 'title', 'description', 'project', 'priority', 'status',
            'test_type', 'created_at', 'updated_at',
        )
        read_only_fields = fields


class TestSuiteCaseSerializer(serializers.ModelSerializer):
    """Serialize a suite membership together with its execution order."""

    testcase = TestCaseSummarySerializer(read_only=True)
    # ``test_case_id`` is accepted by the endpoint for compatibility with the
    # UI automation APIs. The view normalizes that alias before creating the
    # through model; ``testcase_id`` is the canonical serializer input.
    testcase_id = serializers.IntegerField(write_only=True, required=False, min_value=1)
    order = serializers.IntegerField(min_value=0)

    class Meta:
        model = TestSuiteCase
        fields = ('id', 'testcase', 'testcase_id', 'order')
        read_only_fields = ('id', 'testcase')

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Keep IDs available without requiring clients to unpack ``testcase``.
        data['testcase_id'] = instance.testcase_id
        data['test_case_id'] = instance.testcase_id
        return data


class TestSuiteSerializer(serializers.ModelSerializer):
    project = ProjectSummarySerializer(read_only=True)
    author = UserSimpleSerializer(read_only=True)
    suite_cases = TestSuiteCaseSerializer(
        source='testsuitecase_set', many=True, read_only=True
    )
    testcases = TestCaseSummarySerializer(many=True, read_only=True)
    # ``test_cases`` is an output alias used by a few existing frontend
    # views; the through rows remain available as ``suite_cases`` so order is
    # not lost.
    test_cases = TestCaseSummarySerializer(
        source='testcases', many=True, read_only=True
    )
    test_case_count = serializers.SerializerMethodField()
    project_id = serializers.IntegerField(write_only=True, required=False, min_value=1)
    test_case_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        write_only=True,
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = TestSuite
        fields = (
            'id', 'project', 'project_id', 'name', 'description', 'author',
            'created_at', 'updated_at', 'test_case_count', 'testcases',
            'test_cases', 'suite_cases', 'test_case_ids',
        )
        read_only_fields = (
            'id', 'project', 'author', 'created_at', 'updated_at',
            'test_case_count', 'testcases', 'test_cases', 'suite_cases',
        )

    def get_test_case_count(self, obj):
        return obj.testcases.count()

    def validate_test_case_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError('测试用例不能重复')
        return value

    def create(self, validated_data):
        # Project and membership rows are resolved by the view, where the
        # authenticated user's project scope is available.
        validated_data.pop('project_id', None)
        validated_data.pop('test_case_ids', None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop('project_id', None)
        validated_data.pop('test_case_ids', None)
        return super().update(instance, validated_data)


class TestSuiteCreateSerializer(TestSuiteSerializer):
    class Meta(TestSuiteSerializer.Meta):
        pass


class TestSuiteUpdateSerializer(TestSuiteSerializer):
    class Meta(TestSuiteSerializer.Meta):
        pass
