from django.db.models import Q
from rest_framework import serializers
from .models import Version
from apps.users.serializers import UserDirectorySerializer
from apps.projects.models import Project
from apps.projects.serializers import ProjectSimpleSerializer

class VersionSerializer(serializers.ModelSerializer):
    """版本序列化器"""
    created_by = UserDirectorySerializer(read_only=True)
    projects = serializers.SerializerMethodField()
    testcases_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Version
        fields = ['id', 'name', 'description', 'is_baseline', 'projects', 'created_by', 'created_at', 'testcases_count']
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def _visible_projects(self, obj):
        projects = obj.projects.all()
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False):
            return projects.none()
        if user.is_staff or user.is_superuser:
            return projects
        visible_ids = Project.objects.filter(
            Q(owner=user) | Q(members=user)
        ).values('pk')
        return projects.filter(pk__in=visible_ids).distinct()

    def get_projects(self, obj):
        return ProjectSimpleSerializer(
            self._visible_projects(obj), many=True, context=self.context,
        ).data

    def get_testcases_count(self, obj):
        return obj.testcases.filter(
            project__in=self._visible_projects(obj).values('pk')
        ).count()

class VersionCreateSerializer(serializers.ModelSerializer):
    """版本创建序列化器"""
    project_ids = serializers.ListField(child=serializers.IntegerField(), write_only=True)
    
    class Meta:
        model = Version
        fields = ['id', 'name', 'description', 'is_baseline', 'project_ids']
        read_only_fields = ['id']
    
    def validate_project_ids(self, value):
        from apps.projects.models import Project
        if not value:
            raise serializers.ValidationError("至少需要关联一个项目")
        
        if len(set(value)) != len(value):
            raise serializers.ValidationError('项目不能重复')

        # 检查所有项目是否存在
        existing_projects = Project.objects.filter(id__in=value)
        if existing_projects.count() != len(value):
            raise serializers.ValidationError("部分项目不存在")
        
        return value

    def create(self, validated_data):
        project_ids = validated_data.pop('project_ids')
        version = super().create(validated_data)
        version.projects.set(project_ids)
        return version

    def update(self, instance, validated_data):
        project_ids = validated_data.pop('project_ids', None)
        instance = super().update(instance, validated_data)
        if project_ids is not None:
            instance.projects.set(project_ids)
        return instance

class VersionSimpleSerializer(serializers.ModelSerializer):
    """版本简单序列化器，用于在测试用例中显示"""
    class Meta:
        model = Version
        fields = ['id', 'name', 'is_baseline']
