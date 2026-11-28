from django.http import FileResponse
from datetime import timedelta
from rest_framework import generics, permissions, status, pagination
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from django.db import models, transaction
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.views import APIView
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, NotFound
import uuid

from .models import TestCase, TestCaseStep, TestCaseAttachment, TestCaseComment, TestCaseImportRecord, TestCaseSyncProfile
from .serializers import (
    TestCaseSerializer, TestCaseListSerializer, TestCaseCreateSerializer, TestCaseUpdateSerializer,
    TestCaseImportRecordListSerializer, TestCaseImportRecordDetailSerializer, TestCaseSyncProfileSerializer
)
from apps.projects.models import Project
from apps.projects.access import user_can_write_project, writable_projects
from apps.versions.models import Version
from .services import TestCaseImportTemplateService, TestCaseExcelImportService
from .tasks import import_testcases_from_excel, sync_testcase_profile
from .revisions import record_test_case_revision
from .sync import MAX_SYNC_SECONDS

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
    if user.is_staff or user.is_superuser:
        return Project.objects.all()
    return Project.objects.filter(
        models.Q(owner=user) | models.Q(members=user)
    ).distinct()


def _validate_versions_for_project(project, version_ids):
    if version_ids is None:
        return
    requested_ids = set(version_ids)
    if len(requested_ids) != len(version_ids):
        raise ValidationError({'version_ids': '版本不能重复'})
    allowed_ids = set(
        Version.objects.filter(projects=project, pk__in=requested_ids)
        .values_list('pk', flat=True)
    )
    if allowed_ids != requested_ids:
        raise ValidationError({'version_ids': '所有版本必须属于所选项目'})

class TestCaseSyncProfileListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = TestCaseSyncProfileSerializer
    pagination_class = TestCasePagination

    def get_queryset(self):
        return TestCaseSyncProfile.objects.filter(
            project__in=get_user_accessible_projects(self.request.user)
        ).select_related('project', 'created_by').order_by('pk')

    def perform_create(self, serializer):
        project = serializer.validated_data.get('project')
        if not user_can_write_project(self.request.user, project):
            raise PermissionDenied('没有权限在该项目配置同步')
        serializer.save(created_by=self.request.user)


class TestCaseSyncProfileDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = TestCaseSyncProfileSerializer

    def get_queryset(self):
        return TestCaseSyncProfile.objects.filter(
            project__in=get_user_accessible_projects(self.request.user)
        ).select_related('project', 'created_by')

    def _lock_profile(self, profile_id):
        current = TestCaseSyncProfile.objects.select_for_update().filter(pk=profile_id).first()
        if current is None:
            raise NotFound()
        return current

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        visible = self.get_object()
        current = self._lock_profile(visible.pk)
        serializer = self.get_serializer(current, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    def perform_update(self, serializer):
        profile = serializer.instance
        if not user_can_write_project(self.request.user, profile.project):
            raise PermissionDenied('没有权限修改该项目的同步配置')
        if profile.sync_status in {'queued', 'running'}:
            raise ValidationError('同步任务进行中，请等待完成或重新提交过期任务')
        serializer.save(project=profile.project)

    @transaction.atomic
    def perform_destroy(self, instance):
        current = self._lock_profile(instance.pk)
        if not user_can_write_project(self.request.user, current.project):
            raise PermissionDenied('没有权限删除该项目的同步配置')
        if current.sync_status in {'queued', 'running'}:
            raise ValidationError('同步任务进行中，不能删除同步配置')
        current.delete()


class TestCaseSyncProfileSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        with transaction.atomic():
            profile = TestCaseSyncProfile.objects.select_for_update().filter(
                pk=pk,
                project__in=get_user_accessible_projects(request.user),
            ).select_related('project').first()
            if profile is None:
                return Response({'error': '同步配置不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
            if not user_can_write_project(request.user, profile.project):
                raise PermissionDenied('没有权限同步该项目')
            stale_before = timezone.now() - timedelta(seconds=MAX_SYNC_SECONDS + 60)
            if profile.sync_status in {'queued', 'running'} and profile.updated_at > stale_before:
                return Response({
                    'profile_id': profile.pk,
                    'task_id': profile.sync_task_id,
                    'status': profile.sync_status,
                }, status=status.HTTP_202_ACCEPTED)

            task_id = uuid.uuid4().hex
            profile.sync_status = 'queued'
            profile.sync_task_id = task_id
            profile.sync_result = {}
            profile.sync_error = ''
            profile.save(update_fields=[
                'sync_status', 'sync_task_id', 'sync_result', 'sync_error', 'updated_at',
            ])

        publish_errors = []

        def publish():
            try:
                sync_testcase_profile.apply_async(
                    args=(profile.pk, request.user.pk), task_id=task_id, retry=False,
                )
            except Exception:
                TestCaseSyncProfile.objects.filter(
                    pk=profile.pk, sync_task_id=task_id, sync_status='queued',
                ).update(
                    sync_status='failed', sync_error='同步任务提交失败', updated_at=timezone.now(),
                )
                publish_errors.append(True)

        transaction.on_commit(publish)
        if publish_errors:
            return Response({'error': '同步任务提交失败'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({
            'profile_id': profile.pk,
            'task_id': task_id,
            'status': 'queued',
        }, status=status.HTTP_202_ACCEPTED)


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
        project_id = serializer.validated_data.get('project_id')
        with transaction.atomic():
            if project_id is not None:
                project = writable_projects(user).filter(pk=project_id).first()
                if project is None:
                    raise PermissionDenied('项目不存在或无写入权限')
            else:
                project = writable_projects(user).order_by('pk').first()
                if project is None:
                    project = Project.objects.create(
                        name='默认项目', owner=user, description='系统自动创建的默认项目'
                    )

            _validate_versions_for_project(
                project, serializer.validated_data.get('version_ids', []),
            )
            _ensure_external_ref_available(
                project, serializer.validated_data.get('external_ref'),
            )
            testcase = serializer.save(author=user, project=project)
            record_test_case_revision(testcase, user)


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
        with transaction.atomic():
            testcase = TestCase.objects.select_for_update().filter(
                pk=serializer.instance.pk,
            ).first()
            if testcase is None:
                raise NotFound()
            serializer.instance = testcase
            if not user_can_write_project(user, testcase.project):
                raise PermissionDenied('没有权限修改该项目的测试用例')

            project_id = serializer.validated_data.get('project_id')
            project = testcase.project
            if project_id is not None:
                project = writable_projects(user).filter(pk=project_id).first()
                if project is None:
                    raise PermissionDenied('目标项目不存在或无写入权限')
            if not user_can_write_project(user, project):
                raise PermissionDenied('没有权限修改目标项目')
            if project.pk != testcase.project_id and (
                testcase.testsuitecase_set.exists() or testcase.test_runs.exists()
            ):
                raise ValidationError({
                    'project_id': '测试用例已关联测试套件或执行记录，不能移动项目',
                })

            version_ids = serializer.validated_data.get('version_ids')
            if project.pk != testcase.project_id and version_ids is None:
                version_ids = list(testcase.versions.values_list('pk', flat=True))
            _validate_versions_for_project(project, version_ids)
            external_ref = serializer.validated_data.get('external_ref', testcase.external_ref)
            _ensure_external_ref_available(
                project, external_ref, exclude_id=testcase.pk,
            )
            updated = serializer.save(project=project)
            record_test_case_revision(updated, user)

    def perform_destroy(self, instance):
        if not user_can_write_project(self.request.user, instance.project):
            raise PermissionDenied('没有权限删除该项目的测试用例')
        instance.delete()


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

        accessible_projects = writable_projects(request.user)
        project = accessible_projects.filter(pk=project_id).first()
        if project is None:
            return Response({'error': '项目不存在或无写入权限'}, status=status.HTTP_403_FORBIDDEN)

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
