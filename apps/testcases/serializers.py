from rest_framework import serializers
from apps.projects.access import user_can_write_project
from .models import TestCase, TestCaseStep, TestCaseAttachment, TestCaseComment, TestCaseImportRecord, TestCaseSyncProfile
from apps.users.serializers import UserDirectorySerializer
from apps.versions.serializers import VersionSimpleSerializer
from .sync import TestCaseSyncError, _resolve_secret_alias

class TestCaseStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestCaseStep
        fields = '__all__'

class TestCaseAttachmentSerializer(serializers.ModelSerializer):
    uploaded_by = UserDirectorySerializer(read_only=True)
    
    class Meta:
        model = TestCaseAttachment
        fields = '__all__'

class TestCaseCommentSerializer(serializers.ModelSerializer):
    author = UserDirectorySerializer(read_only=True)
    
    class Meta:
        model = TestCaseComment
        fields = '__all__'

class ProjectSimpleSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()

class TestCaseSerializer(serializers.ModelSerializer):
    author = UserDirectorySerializer(read_only=True)
    assignee = UserDirectorySerializer(read_only=True)
    project = ProjectSimpleSerializer(read_only=True)
    versions = serializers.SerializerMethodField()
    step_details = TestCaseStepSerializer(many=True, read_only=True)
    attachments = TestCaseAttachmentSerializer(many=True, read_only=True)
    comments = TestCaseCommentSerializer(many=True, read_only=True)
    
    class Meta:
        model = TestCase
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_versions(self, obj):
        versions = obj.versions.filter(projects__id=obj.project_id).distinct()
        return VersionSimpleSerializer(versions, many=True, context=self.context).data

class TestCaseListSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    assignee = serializers.SerializerMethodField()
    project = serializers.SerializerMethodField()
    versions = serializers.SerializerMethodField()
    
    class Meta:
        model = TestCase
        fields = [
            'id', 'external_ref', 'title', 'description', 'preconditions', 'steps', 'expected_result',
            'priority', 'test_type',
            'author', 'assignee', 'project', 'versions', 'tags', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_author(self, obj):
        return {'id': obj.author.id, 'username': obj.author.username} if obj.author else None
    
    def get_assignee(self, obj):
        return {'id': obj.assignee.id, 'username': obj.assignee.username} if obj.assignee else None
    
    def get_project(self, obj):
        return {'id': obj.project.id, 'name': obj.project.name} if obj.project else None
    
    def get_versions(self, obj):
        return [
            {'id': version.id, 'name': version.name, 'is_baseline': version.is_baseline}
            for version in obj.versions.filter(projects__id=obj.project_id).distinct()
        ]

class TestCaseCreateSerializer(serializers.ModelSerializer):
    project_id = serializers.IntegerField(required=False, allow_null=True, help_text="项目ID，可选")
    version_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False, 
        allow_empty=True,
        help_text="关联版本ID列表"
    )
    
    class Meta:
        model = TestCase
        fields = [
            'id', 'title', 'external_ref', 'description', 'preconditions', 'steps', 'expected_result',
            'priority', 'test_type', 'tags', 'project_id', 'version_ids'
        ]
    
    def validate_external_ref(self, value):
        value = str(value or '').strip()
        if not value:
            raise serializers.ValidationError('external_ref 不能为空')
        return value

    def create(self, validated_data):
        version_ids = validated_data.pop('version_ids', [])
        # project_id会在视图的perform_create中处理
        validated_data.pop('project_id', None)
        
        testcase = super().create(validated_data)
        
        # 设置版本关联
        if version_ids:
            testcase.versions.set(version_ids)
        
        return testcase

class TestCaseUpdateSerializer(serializers.ModelSerializer):
    project_id = serializers.IntegerField(required=False, allow_null=True, help_text="项目ID，可选")
    version_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False, 
        allow_empty=True,
        help_text="关联版本ID列表"
    )
    
    class Meta:
        model = TestCase
        fields = [
            'title', 'external_ref', 'description', 'preconditions', 'steps', 'expected_result',
            'priority', 'test_type', 'tags', 'project_id', 'version_ids'
        ]
    
    def validate_external_ref(self, value):
        value = str(value or '').strip()
        if not value:
            raise serializers.ValidationError('external_ref 不能为空')
        return value

    def update(self, instance, validated_data):
        version_ids = validated_data.pop('version_ids', None)
        # project_id会在视图中处理
        validated_data.pop('project_id', None)
        
        instance = super().update(instance, validated_data)
        
        # 更新版本关联
        if version_ids is not None:
            instance.versions.set(version_ids)

        return instance


class TestCaseImportRecordListSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = TestCaseImportRecord
        fields = [
            'id', 'import_no', 'project', 'project_name', 'status', 'progress',
            'total_rows', 'success_count', 'failed_count', 'skip_count',
            'error_message', 'template_version', 'created_by_name',
            'created_at', 'completed_at'
        ]


class TestCaseImportRecordDetailSerializer(TestCaseImportRecordListSerializer):
    failure_report_url = serializers.SerializerMethodField()

    class Meta(TestCaseImportRecordListSerializer.Meta):
        fields = TestCaseImportRecordListSerializer.Meta.fields + [
            'failure_details', 'failure_report_file', 'failure_report_url'
        ]

    def get_failure_report_url(self, obj):
        if obj.failure_report_file:
            return obj.failure_report_file.url
        return None


class TestCaseSyncProfileSerializer(serializers.ModelSerializer):
    created_by = UserDirectorySerializer(read_only=True)
    secret_configured = serializers.SerializerMethodField()

    class Meta:
        model = TestCaseSyncProfile
        fields = '__all__'
        read_only_fields = (
            'id', 'created_by', 'last_commit_sha', 'last_sync_at', 'created_at',
            'updated_at', 'secret_configured', 'sync_status', 'sync_task_id',
            'sync_result', 'sync_error',
        )
        extra_kwargs = {'token_env_ref': {'write_only': True}}

    def validate_project(self, value):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False) or not user_can_write_project(user, value):
            raise serializers.ValidationError('项目不存在或无写入权限')
        return value

    def validate_token_env_ref(self, value):
        value = str(value or '').strip()
        if not value:
            return value
        try:
            _resolve_secret_alias(value)
        except TestCaseSyncError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

    def validate(self, attrs):
        # On PATCH the provider may be omitted; aliases remain server-owned.
        alias = attrs.get('token_env_ref')
        if alias:
            try:
                _resolve_secret_alias(alias)
            except TestCaseSyncError as exc:
                raise serializers.ValidationError({'token_env_ref': str(exc)}) from exc
        return super().validate(attrs)

    def get_secret_configured(self, obj):
        import os
        if not obj.token_env_ref:
            return False
        try:
            env_name = _resolve_secret_alias(obj.token_env_ref)
        except TestCaseSyncError:
            return False
        return bool(os.environ.get(env_name))
