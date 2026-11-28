from rest_framework import generics, permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from django.db import models, transaction
from rest_framework.exceptions import PermissionDenied
from apps.projects.access import user_can_write_project, writable_projects
from .models import Version
from .serializers import VersionSerializer, VersionCreateSerializer
from apps.projects.models import Project


def accessible_projects(user):
    if user.is_staff or user.is_superuser:
        return Project.objects.all()
    return Project.objects.filter(
        models.Q(owner=user) | models.Q(members=user)
    ).distinct()


# 版本管理视图
class VersionListCreateView(generics.ListCreateAPIView):
    """版本列表和创建视图"""
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_baseline']  # 移除projects，手动处理
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'name']
    ordering = ['-created_at']
    
    def get_serializer_class(self):
        if self.request.method == 'POST':
            return VersionCreateSerializer
        return VersionSerializer
    
    def get_queryset(self):
        accessible = accessible_projects(self.request.user)
        queryset = Version.objects.filter(projects__in=accessible).distinct()
        
        # 手动处理项目筛选
        project_id = self.request.query_params.get('projects')
        if project_id and project_id.strip():  # 检查是否为空或空字符串
            try:
                project_id = int(project_id)
                queryset = queryset.filter(projects__id=project_id)
            except (ValueError, TypeError):
                # 如果无法转换为整数，忽略此筛选条件
                pass
        
        return queryset
    
    def perform_create(self, serializer):
        user = self.request.user
        project_ids = serializer.validated_data.get('project_ids')
        
        writable_project_ids = set(
            writable_projects(user, Project.objects.filter(pk__in=project_ids))
            .values_list('pk', flat=True)
        )
        requested_project_ids = set(project_ids)
        if requested_project_ids != writable_project_ids:
            raise PermissionDenied('没有权限在所有指定项目创建版本')

        with transaction.atomic():
            serializer.save(created_by=user)

class VersionDetailView(generics.RetrieveUpdateDestroyAPIView):
    """版本详情视图"""
    serializer_class = VersionSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        return Version.objects.filter(
            projects__in=accessible_projects(self.request.user)
        ).distinct()

    def _require_write_scope(self, instance):
        projects = list(instance.projects.all())
        if not projects or any(
            not user_can_write_project(self.request.user, project)
            for project in projects
        ):
            raise PermissionDenied('没有权限修改所有关联项目中的版本')

    def perform_update(self, serializer):
        self._require_write_scope(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._require_write_scope(instance)
        instance.delete()

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_project_versions(request, project_id):
    """获取指定项目的版本列表"""
    user = request.user
    
    # 检查项目权限
    accessible = accessible_projects(user)
    if not accessible.filter(pk=project_id).exists():
        return Response({'error': '没有权限访问该项目'}, status=status.HTTP_403_FORBIDDEN)
    
    versions = Version.objects.filter(projects__id=project_id).order_by('-created_at')
    serializer = VersionSerializer(versions, many=True, context={'request': request})
    return Response(serializer.data)
