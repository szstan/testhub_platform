from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from django.db.models import Q
from .models import (
    Artifact,
    Attempt,
    ExecutionArtifact,
    ExecutionAttempt,
    ExecutionBuild,
    ExecutionDataMatrix,
    ExecutionDataset,
    ExecutionDefectLink,
    ExecutionEvent,
    ExecutionFixture,
    ExecutionJob,
    ExecutionJobVariant,
    ExecutionPluginManifest,
    ExecutionProfile,
    ExecutionQualityGateEvaluation,
    ExecutionRetryPolicy,
    ExecutionFailureReview,
    ExecutionAISuggestion,
    ExecutionAISuggestionAudit,
    WebhookEvent,
    ExecutionRequirementLink,
    ExecutionStepResult,
    IdempotencyConflict,
    RequirementTestCaseLink,
    StepResult,
    TestCaseDefectLink,
    TestCaseRevision,
    TestPlan,
    TestRun,
    TestRunCase,
    TestRunCaseHistory,
)
from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.users.serializers import UserSimpleSerializer
from apps.versions.models import Version


def accessible_execution_projects(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return Project.objects.none()
    if user.is_staff or user.is_superuser:
        return Project.objects.all()
    return Project.objects.filter(Q(owner=user) | Q(members=user)).distinct()


def _ensure_project_access(user, project, detail='没有权限访问该项目'):
    if project is None or user is None or not getattr(user, 'is_authenticated', False):
        return
    if not accessible_execution_projects(user).filter(pk=project.pk).exists():
        raise PermissionDenied(detail)


def _project_from_requirement(requirement):
    return requirement.analysis.document.project_id


class ExecutionAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionAttempt
        fields = (
            'id', 'job', 'variant', 'attempt_number', 'idempotency_key', 'status', 'worker',
            'result', 'retry_reason', 'retry_requested_by', 'retry_requested_at', 'retry_policy',
            'retry_mode', 'retry_decision', 'retry_scheduled_at', 'is_final',
            'error_type', 'error_message', 'started_at', 'finished_at',
            'created_at', 'updated_at',
        )
        read_only_fields = (
            'id', 'profile', 'provider', 'external_event_id', 'payload_hash',
            'payload_summary', 'status', 'job', 'error_message', 'received_at',
            'processed_at', 'retry_requested_by', 'retry_requested_at', 'retry_policy',
            'retry_mode', 'retry_decision', 'retry_scheduled_at', 'is_final',
        )


class ExecutionStepResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionStepResult
        fields = (
            'id', 'attempt', 'case_key', 'step_key', 'idempotency_key', 'status',
            'result', 'error_type', 'error_message', 'duration_ms', 'started_at',
            'finished_at', 'created_at', 'updated_at',
        )
        read_only_fields = fields


class ExecutionArtifactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionArtifact
        fields = (
            'id', 'job', 'attempt', 'step_result', 'idempotency_key', 'uri',
            'content_hash', 'media_type', 'size_bytes', 'retention_policy',
            'retention_until', 'metadata', 'created_at', 'updated_at',
        )
        read_only_fields = fields


class ExecutionDatasetSerializer(serializers.ModelSerializer):
    created_by = UserSimpleSerializer(read_only=True)
    variants_count = serializers.SerializerMethodField()

    class Meta:
        model = ExecutionDataset
        fields = '__all__'
        read_only_fields = ('id', 'created_by', 'created_at', 'updated_at', 'variants_count')
        extra_kwargs = {'secret_refs': {'write_only': True}}

    def get_variants_count(self, obj):
        try:
            from .datasets import expand_dataset
            return len(expand_dataset(obj))
        except Exception:
            return 0

    def validate(self, attrs):
        project = attrs.get('project', getattr(self.instance, 'project', None))
        _ensure_project_access(self.context.get('request').user if self.context.get('request') else None, project,
                               '没有权限管理该项目的数据集')
        return attrs


class ExecutionDataMatrixSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionDataMatrix
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate(self, attrs):
        dataset = attrs.get('dataset', getattr(self.instance, 'dataset', None))
        user = self.context.get('request').user if self.context.get('request') else None
        if dataset is not None:
            _ensure_project_access(user, dataset.project, '没有权限管理该数据集项目')
        return attrs


class ExecutionFixtureSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionFixture
        fields = '__all__'
        read_only_fields = ('id', 'created_by', 'created_at', 'cleaned_at', 'status', 'cleanup_error')


class ExecutionPluginManifestSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionPluginManifest
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate(self, attrs):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        project = attrs.get('project', getattr(self.instance, 'project', None))
        if project is None:
            if user is not None and getattr(user, 'is_authenticated', False) and not (user.is_staff or user.is_superuser):
                raise PermissionDenied('仅管理员可以管理全局插件声明')
        else:
            _ensure_project_access(user, project, '没有权限管理该项目插件声明')
        return attrs


class ExecutionProfileSerializer(serializers.ModelSerializer):
    created_by = UserSimpleSerializer(read_only=True)
    secret_configured = serializers.SerializerMethodField()

    class Meta:
        model = ExecutionProfile
        fields = '__all__'
        extra_kwargs = {'webhook_secret_ref': {'write_only': True}}
        read_only_fields = ('id', 'created_by', 'created_at', 'updated_at', 'secret_configured')

    def get_secret_configured(self, obj):
        import os
        return bool(obj.webhook_secret_ref and os.environ.get(obj.webhook_secret_ref))


class ExecutionRetryPolicySerializer(serializers.ModelSerializer):
    created_by = UserSimpleSerializer(read_only=True)

    class Meta:
        model = ExecutionRetryPolicy
        fields = '__all__'
        read_only_fields = ('id', 'created_by', 'created_at', 'updated_at')


class ExecutionQualityGateEvaluationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionQualityGateEvaluation
        fields = (
            'id', 'attempt', 'evaluation_key', 'job', 'profile', 'stage', 'passed',
            'summary', 'failures', 'reason', 'evaluated_by', 'created_at',
        )
        read_only_fields = fields


class ExecutionFailureReviewSerializer(serializers.ModelSerializer):
    reviewed_by = UserSimpleSerializer(read_only=True)

    class Meta:
        model = ExecutionFailureReview
        fields = '__all__'
        read_only_fields = ('id', 'reviewed_by', 'reviewed_at', 'history', 'created_at', 'updated_at')

class ExecutionAISuggestionSerializer(serializers.ModelSerializer):
    requested_by = UserSimpleSerializer(read_only=True)
    reviewed_by = UserSimpleSerializer(read_only=True)

    class Meta:
        model = ExecutionAISuggestion
        fields = '__all__'
        read_only_fields = (
            'id', 'status', 'suggestion_hash', 'input_redacted', 'output_redacted',
            'target_type', 'target_key', 'source_fingerprint',
            'requested_by', 'reviewed_by', 'reviewed_at', 'review_note',
            'created_at', 'updated_at',
        )

    def to_representation(self, instance):
        result = super().to_representation(instance)
        # The service already redacts values; retain a second serializer boundary.
        from .result_events import redact_result_payload
        result['input_redacted'] = redact_result_payload(result.get('input_redacted') or {})
        result['output_redacted'] = redact_result_payload(result.get('output_redacted') or {})
        result['review_note'] = str(result.get('review_note') or '')[:4000]
        return result


class ExecutionAISuggestionAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionAISuggestionAudit
        fields = (
            'id', 'suggestion', 'action', 'actor', 'previous_status', 'new_status',
            'note', 'metadata', 'created_at',
        )
        read_only_fields = fields


class WebhookEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookEvent
        fields = (
            'id', 'profile', 'provider', 'external_event_id', 'payload_hash',
            'payload_summary', 'status', 'job', 'error_message', 'received_at', 'processed_at',
        )
        read_only_fields = fields


class ExecutionEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionEvent
        fields = '__all__'
        read_only_fields = (
            'id', 'job', 'attempt', 'event_type', 'event_key', 'idempotency_key',
            'case_key', 'step_key', 'status', 'payload', 'occurred_at', 'created_at',
        )


class ExecutionJobVariantSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionJobVariant
        fields = (
            'id', 'job', 'variant_key', 'ordinal', 'payload', 'status', 'result',
            'error_type', 'error_message', 'dispatch_id', 'started_at', 'finished_at',
            'created_at', 'updated_at',
        )
        read_only_fields = fields


class ExecutionJobSerializer(serializers.ModelSerializer):
    """Minimal public contract for creating and observing a job."""

    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all())
    version = serializers.PrimaryKeyRelatedField(
        queryset=Version.objects.all(),
        required=False,
        allow_null=True,
    )
    build = serializers.PrimaryKeyRelatedField(
        queryset=ExecutionBuild.objects.all(),
        required=False,
        allow_null=True,
    )
    profile = serializers.PrimaryKeyRelatedField(
        queryset=ExecutionProfile.objects.all(),
        required=False,
        allow_null=True,
    )
    dataset = serializers.PrimaryKeyRelatedField(
        queryset=ExecutionDataset.objects.all(),
        required=False,
        allow_null=True,
    )
    requested_by = UserSimpleSerializer(read_only=True)
    dataset_snapshot = serializers.JSONField(read_only=True)
    variants = ExecutionJobVariantSerializer(many=True, read_only=True)
    executor_status = serializers.SerializerMethodField()
    execution_supported = serializers.SerializerMethodField()
    attempts_count = serializers.SerializerMethodField()

    class Meta:
        model = ExecutionJob
        fields = (
            'id', 'project', 'dataset', 'dataset_snapshot', 'variants', 'source_type', 'source_id', 'version', 'build', 'profile',
            'build_reference', 'trigger', 'environment_snapshot', 'priority',
            'status', 'cancel_reason', 'attempt', 'worker', 'requested_by',
            'result', 'final_result', 'final_attempt', 'final_status', 'quality_gate_result', 'retry_policy_snapshot', 'finalized_at',
            'error_type', 'error_message', 'idempotency_key',
            'queued_at', 'started_at', 'finished_at', 'cancelled_at',
            'created_at', 'updated_at', 'executor_status',
            'execution_supported', 'attempts_count',
        )
        read_only_fields = (
            'id', 'dataset_snapshot', 'variants', 'status', 'cancel_reason', 'attempt', 'worker', 'requested_by',
            'result', 'final_result', 'final_attempt', 'final_status', 'quality_gate_result', 'retry_policy_snapshot', 'finalized_at',
            'error_type', 'error_message', 'queued_at', 'started_at',
            'finished_at', 'cancelled_at', 'created_at', 'updated_at',
            'executor_status', 'execution_supported', 'attempts_count',
        )
        extra_kwargs = {
            'idempotency_key': {'required': False, 'allow_blank': False},
        }

    def get_executor_status(self, obj):
        # No executor is connected in this additive foundation.  Keep this
        # explicit so clients do not interpret pending as actively dispatched.
        return 'unsupported'

    def get_execution_supported(self, obj):
        return False

    def get_attempts_count(self, obj):
        return obj.attempts.count()

    def validate(self, attrs):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        project = attrs.get('project')
        if project is not None and user is not None:
            if not accessible_execution_projects(user).filter(pk=project.pk).exists():
                raise PermissionDenied('没有权限访问该项目')

        dataset = attrs.get('dataset')
        if dataset is not None:
            if not accessible_execution_projects(user).filter(pk=dataset.project_id).exists():
                raise PermissionDenied('没有权限访问该数据集项目')
            if project is None or dataset.project_id != project.pk:
                raise serializers.ValidationError({'dataset': '数据集不属于所选项目'})
            if not dataset.enabled:
                raise serializers.ValidationError({'dataset': '数据集已禁用'})

        version = attrs.get('version')
        build = attrs.get('build')
        if version is not None and project is not None:
            if not version.projects.filter(pk=project.pk).exists():
                raise serializers.ValidationError({'version': '版本不属于所选项目'})
        if build is not None and project is not None and build.project_id != project.pk:
            raise serializers.ValidationError({'build': '构建不属于所选项目'})
        profile = attrs.get('profile')
        if profile is not None and project is not None and profile.project_id != project.pk:
            raise serializers.ValidationError({'profile': '执行配置不属于所选项目'})
        return attrs

    def create(self, validated_data):
        request = self.context.get('request')
        requested_by = validated_data.pop('requested_by', None)
        if requested_by is None:
            requested_by = getattr(request, 'user', None)
        project = validated_data.pop('project')
        idempotency_key = validated_data.pop('idempotency_key', None)
        job, created = ExecutionJob.objects.create_idempotent(
            project=project,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            defaults=validated_data,
        )
        if created and job.dataset_id:
            job.initialize_dataset_variants()
        self._created = created
        return job


class ExecutionBuildSerializer(serializers.ModelSerializer):
    created_by = UserSimpleSerializer(read_only=True)

    class Meta:
        model = ExecutionBuild
        fields = '__all__'
        read_only_fields = ('id', 'created_by', 'created_at', 'updated_at')

    def validate(self, attrs):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        project = attrs.get('project', getattr(self.instance, 'project', None))
        _ensure_project_access(user, project, '没有权限管理该项目的构建')
        version = attrs.get('version', getattr(self.instance, 'version', None))
        if version is not None and project is not None and not version.projects.filter(pk=project.pk).exists():
            raise serializers.ValidationError({'version': '版本不属于所选项目'})
        return attrs


class RequirementTestCaseLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = RequirementTestCaseLink
        fields = '__all__'
        read_only_fields = ('id', 'created_by', 'created_at')

    def validate(self, attrs):
        requirement = attrs.get('requirement', getattr(self.instance, 'requirement', None))
        test_case = attrs.get('test_case', getattr(self.instance, 'test_case', None))
        project_id = _project_from_requirement(requirement) if requirement is not None else None
        if requirement is not None and test_case is not None and project_id != test_case.project_id:
            raise serializers.ValidationError('需求和测试用例必须属于同一项目')
        user = self.context.get('request').user if self.context.get('request') else None
        project = Project.objects.filter(pk=project_id).first() if project_id is not None else None
        _ensure_project_access(user, project, '没有权限关联该项目')
        return attrs


class TestCaseDefectLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestCaseDefectLink
        fields = '__all__'
        read_only_fields = ('id', 'created_by', 'created_at')

    def validate(self, attrs):
        test_case = attrs.get('test_case', getattr(self.instance, 'test_case', None))
        defect = attrs.get('defect', getattr(self.instance, 'defect', None))
        if test_case is not None and defect is not None and test_case.project_id != defect.project_id:
            raise serializers.ValidationError('测试用例和缺陷必须属于同一项目')
        user = self.context.get('request').user if self.context.get('request') else None
        project = getattr(test_case, 'project', None)
        _ensure_project_access(user, project, '没有权限关联该项目')
        return attrs


class ExecutionRequirementLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionRequirementLink
        fields = '__all__'
        read_only_fields = ('id', 'created_at')

    def validate(self, attrs):
        job = attrs.get('job', getattr(self.instance, 'job', None))
        requirement = attrs.get('requirement', getattr(self.instance, 'requirement', None))
        project_id = _project_from_requirement(requirement) if requirement is not None else None
        if job is not None and requirement is not None and job.project_id != project_id:
            raise serializers.ValidationError('需求和执行任务必须属于同一项目')
        user = self.context.get('request').user if self.context.get('request') else None
        _ensure_project_access(user, getattr(job, 'project', None), '没有权限关联该执行任务')
        return attrs


class ExecutionDefectLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionDefectLink
        fields = '__all__'
        read_only_fields = ('id', 'created_at')

    def validate(self, attrs):
        job = attrs.get('job', getattr(self.instance, 'job', None))
        defect = attrs.get('defect', getattr(self.instance, 'defect', None))
        if job is not None and defect is not None and job.project_id != defect.project_id:
            raise serializers.ValidationError('缺陷和执行任务必须属于同一项目')
        user = self.context.get('request').user if self.context.get('request') else None
        _ensure_project_access(user, getattr(job, 'project', None), '没有权限关联该执行任务')
        return attrs


class TestCaseRevisionSerializer(serializers.ModelSerializer):
    changed_by = UserSimpleSerializer(read_only=True)

    class Meta:
        model = TestCaseRevision
        fields = '__all__'
        read_only_fields = ('id', 'changed_by', 'created_at')


class TestRunCaseHistorySerializer(serializers.ModelSerializer):
    executed_by = UserSimpleSerializer(read_only=True)
    
    class Meta:
        model = TestRunCaseHistory
        fields = ('id', 'status', 'actual_result', 'comments', 'executed_by', 'executed_at')

class TestRunCaseSimpleSerializer(serializers.ModelSerializer):
    testcase = serializers.StringRelatedField()
    class Meta:
        model = TestRunCase
        fields = ('id', 'testcase', 'status')

class TestRunCaseDetailSerializer(serializers.ModelSerializer):
    testcase = serializers.StringRelatedField()
    executed_by = UserSimpleSerializer(read_only=True)
    history = TestRunCaseHistorySerializer(many=True, read_only=True)
    
    class Meta:
        model = TestRunCase
        fields = ('id', 'testcase', 'status', 'priority', 'actual_result', 'comments', 
                 'defects', 'elapsed_time', 'executed_by', 'executed_at', 'created_at', 
                 'updated_at', 'history')

class TestRunSerializer(serializers.ModelSerializer):
    run_cases = TestRunCaseSimpleSerializer(many=True, read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta:
        model = TestRun
        fields = ('id', 'name', 'status', 'assignee', 'progress', 'run_cases')
    
    def get_progress(self, obj):
        return obj.progress_stats


class TestPlanSerializer(serializers.ModelSerializer):
    creator = UserSimpleSerializer(read_only=True)
    projects = serializers.StringRelatedField(many=True, read_only=True)
    version = serializers.StringRelatedField()

    class Meta:
        model = TestPlan
        fields = ('id', 'name', 'projects', 'version', 'creator', 'created_at', 'is_active')


class TestPlanDetailSerializer(serializers.ModelSerializer):
    test_runs = TestRunSerializer(many=True, read_only=True)
    creator = UserSimpleSerializer(read_only=True)
    projects = serializers.StringRelatedField(many=True, read_only=True)
    version = serializers.StringRelatedField()
    testcases = serializers.SerializerMethodField()

    class Meta:
        model = TestPlan
        fields = '__all__'

    def get_testcases(self, obj):
        testcase_map = {}
        for test_run in obj.test_runs.prefetch_related('testcases__project').all():
            for testcase in test_run.testcases.all():
                testcase_map[testcase.id] = {
                    'id': testcase.id,
                    'title': testcase.title,
                    'priority': testcase.priority,
                    'test_type': testcase.test_type,
                    'project__name': testcase.project.name if testcase.project else ''
                }
        return list(testcase_map.values())

class TestRunCaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestRunCase
        fields = '__all__'

    def validate(self, attrs):
        test_run = attrs.get('test_run', getattr(self.instance, 'test_run', None))
        testcase = attrs.get('testcase', getattr(self.instance, 'testcase', None))
        if test_run is not None and testcase is not None and test_run.project_id != testcase.project_id:
            raise serializers.ValidationError({'testcase': '测试用例必须属于执行任务所在项目'})
        user = self.context.get('request').user if self.context.get('request') else None
        _ensure_project_access(user, getattr(test_run, 'project', None), '没有权限管理该执行任务用例')
        return attrs
