from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema
from django.db import models
from . import schema as schema
from .models import Project

@extend_schema(responses={
    200: schema.ProjectsUserDropdownResponseSerializer,
    401: schema.ProjectsAuthFailureResponseSerializer,
})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def user_projects_list(request):
    """获取用户有权限访问的项目列表，用于下拉选择"""
    user = request.user
    projects = Project.objects.filter(
        models.Q(owner=user) | models.Q(members=user)
    ).distinct().values('id', 'name', 'status').order_by('name')
    
    return Response({
        'results': list(projects)
    })