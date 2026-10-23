from django.http import FileResponse
from rest_framework import generics, permissions, status, pagination
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from django.db import models
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.views import APIView

from .models import TestCase, TestCaseStep, TestCaseAttachment, TestCaseComment, TestCaseImportRecord, TestCaseSyncProfile
from .serializers import (
    TestCaseSerializer, TestCaseListSerializer, TestCaseCreateSerializer, TestCaseUpdateSerializer,
    TestCaseImportRecordListSerializer, TestCaseImportRecordDetailSerializer, TestCaseSyncProfileSerializer
)
from apps.projects.models import Project
from .services import TestCaseImportTemplateService, TestCaseExcelImportService
from .tasks import import_testcases_from_excel
from apps.executions.models import TestCaseRevision
from .sync import TestCaseSyncError, sync_profile

class TestCasePagination(pagination.PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class TestCaseImportRecordPagination(pagination.PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


def _ensure_external_ref_available(project, external_ref, *, exclude_id=None):
    if not external_ref:
        return
    queryset = TestCase.objects.filter(project=project, external_ref=external_ref)
    if exclude_id is not None:
        queryset = queryset.exclude(pk=exclude_id)
    if queryset.exists():
        raise ValidationError({'external_ref': '该项目中已存在相同的 external_ref'})


def get_user_accessible_projects(user):
    return Project.objects.filter(
        models.Q(owner=user) | models.Q(members=user)
    ).distinct()

class TestCaseSyncProfileListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = TestCaseSyncProfileSerializer

    def get_queryset(self):
        return TestCaseSyncProfile.objects.filter(
            project__in=get_user_accessible_projects(self.request.user)
        ).select_related('project', 'created_by')

    def perform_create(self, serializer):
        project = serializer.validated_data.get('project')
        if not get_user_accessible_projects(self.request.user).filter(pk=project.pk).exists():
            raise ValidationError({'project': '项目不存在或无权限'})
        serializer.save(created_by=self.request.user)


class TestCaseSyncProfileDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = TestCaseSyncProfileSerializer

    def get_queryset(self):
        return TestCaseSyncProfile.objects.filter(
            project__in=get_user_accessible_projects(self.request.user)
        ).select_related('project', 'created_by')

    def perform_update(self, serializer):
        serializer.save(project=serializer.instance.project)


class TestCaseSyncProfileSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        profile = TestCaseSyncProfile.objects.filter(
            pk=pk,
            project__in=get_user_accessible_projects(request.user),
        ).first()
        if profile is None:
            return Response({'error': '同步配置不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
        try:
            summary = sync_profile(profile)
        except TestCaseSyncError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'profile_id': summary.profile_id,
            'provider': summary.provider,
            'commit': summary.commit,
            'files': summary.files,
            'created': summary.created,
            'updated': summary.updated,
            'skipped': summary.skipped,
            'conflicts': summary.conflicts,
            'warnings': summary.warnings,
        })


class TestCaseListCreateView(generics.ListCreateAPIView):
    queryset = TestCase.objects.all()
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = TestCasePagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['priority', 'test_type']
    search_fields = ['title', 'description', 'external_ref']
    ordering_fields = ['created_at', 'updated_at', 'priority']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return TestCaseCreateSerializer
        return TestCaseListSerializer

    def get_queryset(self):
        user = self.request.user
        accessible_projects = get_user_accessible_projects(user)
        queryset = TestCase.objects.filter(
            project__in=accessible_projects
        ).select_related(
            'author', 'assignee', 'project'
        ).prefetch_related(
            'versions'
        ).distinct()
        # 支持多项目过滤：project=1,2,3（单值 project=1 同样兼容）
        project_param = self.request.query_params.get('project')
        if project_param:
            project_ids = [int(pid) for pid in project_param.split(',') if pid.strip().isdigit()]
            if project_ids:
                queryset = queryset.filter(project_id__in=project_ids)
        return queryset
    
    def get_user_accessible_projects(self, user):
        """获取用户有权限访问的项目"""
        return get_user_accessible_projects(user)
    
    def perform_create(self, serializer):
        user = self.request.user
        project_id = self.request.data.get('project_id')
        
        # 获取用户有权限的项目
        accessible_projects = self.get_user_accessible_projects(user)
        
        if project_id:
            # 检查指定的项目是否存在且用户有权限
            try:
                project = accessible_projects.get(id=project_id)
            except Project.DoesNotExist:
                # 如果指定项目不存在或无权限，使用第一个可访问的项目
                project = accessible_projects.first()
                if not project:
                    # 如果用户没有任何项目，创建默认项目
                    project = Project.objects.create(
                        name="默认项目",
                        owner=user,
                        description='系统自动创建的默认项目'
                    )
        else:
            # 没有指定项目，使用第一个可访问的项目
            project = accessible_projects.first()
            if not project:
                # 如果用户没有任何项目，创建默认项目
                project = Project.objects.create(
                    name="默认项目",
                    owner=user,
                    description='系统自动创建的默认项目'
                )
        
        external_ref = serializer.validated_data.get('external_ref')
        _ensure_external_ref_available(project, external_ref)
        testcase = serializer.save(author=user, project=project)
        _record_test_case_revision(testcase, user)

def _test_case_snapshot(test_case):
    return {
        'title': test_case.title,
        'external_ref': test_case.external_ref,
        'description': test_case.description,
        'preconditions': test_case.preconditions,
        'steps': test_case.steps,
        'expected_result': test_case.expected_result,
        'priority': test_case.priority,
        'status': test_case.status,
        'test_type': test_case.test_type,
        'tags': test_case.tags,
        'project_id': test_case.project_id,
        'version_ids': list(test_case.versions.values_list('id', flat=True)),
    }


def _record_test_case_revision(test_case, user):
    latest = TestCaseRevision.objects.filter(test_case=test_case).order_by('-revision').values_list('revision', flat=True).first() or 0
    TestCaseRevision.objects.create(
        test_case=test_case,
        revision=latest + 1,
        snapshot=_test_case_snapshot(test_case),
        changed_by=user,
    )


class TestCaseDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = TestCase.objects.all()
    permission_classes = [permissions.IsAuthenticated]
    
    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return TestCaseUpdateSerializer
        return TestCaseSerializer
    
    def get_queryset(self):
        user = self.request.user
        accessible_projects = get_user_accessible_projects(user)
        return TestCase.objects.filter(
            project__in=accessible_projects
        ).select_related(
            'author', 'assignee', 'project'
        ).prefetch_related(
            'versions', 'step_details', 'attachments', 'comments'
        )
    
    def get_user_accessible_projects(self, user):
        """获取用户有权限访问的项目"""
        return get_user_accessible_projects(user)
    
    def perform_update(self, serializer):
        user = self.request.user
        project_id = self.request.data.get('project_id')
        project = serializer.instance.project

        if project_id:
            accessible_projects = self.get_user_accessible_projects(user)
            try:
                project = accessible_projects.get(id=project_id)
            except Project.DoesNotExist:
                project = serializer.instance.project

        external_ref = serializer.validated_data.get('external_ref', serializer.instance.external_ref)
        _ensure_external_ref_available(project, external_ref, exclude_id=serializer.instance.pk)
        updated = serializer.save(project=project)
        _record_test_case_revision(updated, user)


class TestCaseImportTemplateDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        template_stream = TestCaseImportTemplateService.build_template()
        return FileResponse(
            template_stream,
            as_attachment=True,
            filename='testcase_import_template_v1.xlsx',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )


class TestCaseImportRecordListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = TestCaseImportRecordPagination
    parser_classes = [MultiPartParser, FormParser]

    def get_serializer_class(self):
        return TestCaseImportRecordListSerializer

    def get_queryset(self):
        return TestCaseImportRecord.objects.filter(
            project__in=get_user_accessible_projects(self.request.user)
        ).select_related('project', 'created_by')

    def create(self, request, *args, **kwargs):
        project_id = request.data.get('project_id')
        import_file = request.FILES.get('file')

        if not project_id:
            return Response({'error': '请选择导入项目'}, status=status.HTTP_400_BAD_REQUEST)
        if not import_file:
            return Response({'error': '请上传 Excel 文件'}, status=status.HTTP_400_BAD_REQUEST)
        if not import_file.name.lower().endswith('.xlsx'):
            return Response({'error': '仅支持 .xlsx 格式文件'}, status=status.HTTP_400_BAD_REQUEST)

        accessible_projects = get_user_accessible_projects(request.user)
        try:
            project = accessible_projects.get(id=project_id)
        except Project.DoesNotExist:
            return Response({'error': '项目不存在或无权限访问'}, status=status.HTTP_403_FORBIDDEN)

        record = TestCaseImportRecord.objects.create(
            import_no=TestCaseExcelImportService.generate_import_no(),
            project=project,
            import_file=import_file,
            created_by=request.user,
            template_version='v1'
        )

        celery_task = import_testcases_from_excel.delay(record.id)
        record.celery_task_id = celery_task.id
        record.save(update_fields=['celery_task_id', 'updated_at'])

        serializer = self.get_serializer(record)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class TestCaseImportRecordDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = TestCaseImportRecordDetailSerializer
    queryset = TestCaseImportRecord.objects.select_related('project', 'created_by')

    def get_queryset(self):
        return super().get_queryset().filter(
            project__in=get_user_accessible_projects(self.request.user)
        )


class TestCaseImportFailureReportDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        record = TestCaseImportRecord.objects.filter(
            pk=pk,
            project__in=get_user_accessible_projects(request.user)
        ).first()

        if not record:
            return Response({'error': '导入记录不存在'}, status=status.HTTP_404_NOT_FOUND)
        if not record.failure_report_file:
            return Response({'error': '当前记录没有失败明细文件'}, status=status.HTTP_404_NOT_FOUND)

        return FileResponse(
            record.failure_report_file.open('rb'),
            as_attachment=True,
            filename=record.failure_report_file.name.split('/')[-1],
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
