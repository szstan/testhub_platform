from django.db.models import Q
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from apps.projects.models import Project

from .models import TestReport


def accessible_report_projects(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return Project.objects.none()
    if user.is_staff or user.is_superuser:
        return Project.objects.all()
    return Project.objects.filter(Q(owner=user) | Q(members=user)).distinct()


class TestReportSerializer(serializers.ModelSerializer):
    generated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = TestReport
        fields = '__all__'
        read_only_fields = ('created_at', 'generated_by')

    def validate(self, attrs):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        project = attrs.get('project', getattr(self.instance, 'project', None))
        if (
            project is not None
            and user is not None
            and getattr(user, 'is_authenticated', False)
            and not accessible_report_projects(user).filter(pk=project.pk).exists()
        ):
            raise PermissionDenied('没有权限管理该项目的报告')

        execution = attrs.get('execution', getattr(self.instance, 'execution', None))
        if execution is not None and project is not None and execution.project_id != project.pk:
            raise serializers.ValidationError({'execution': '执行记录不属于所选项目'})

        execution_job = attrs.get('execution_job', getattr(self.instance, 'execution_job', None))
        if execution_job is not None and project is not None and execution_job.project_id != project.pk:
            raise serializers.ValidationError({'execution_job': '统一执行任务不属于所选项目'})

        if execution is not None and execution_job is not None and execution.project_id != execution_job.project_id:
            raise serializers.ValidationError('执行记录和统一执行任务必须属于同一项目')
        return attrs
