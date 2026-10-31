from rest_framework import viewsets, status, filters
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.pagination import PageNumberPagination
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.db.models import BigIntegerField, CharField, Count, Max, Min, Q, Sum, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce
from .models import (
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
    ExecutionNotificationEvent,
    ExecutionNotificationSubscription,
    ExecutionPluginManifest,
    ExecutionProfile,
    ExecutionQualityGateEvaluation,
    ExecutionRetryPolicy,
    ExecutionSourceBinding,
    ExecutionFailureReview,
    ExecutionAISuggestion,
    ExecutionAISuggestionAudit,
    ExecutionRequirementLink,
    WebhookEvent,
    ExecutionStepResult,
    IdempotencyConflict,
    RequirementTestCaseLink,
    TestCaseDefectLink,
    TestCaseRevision,
    TestPlan,
    TestRun,
    TestRunCase,
    TestRunCaseHistory,
)
from .serializers import (
    ExecutionArtifactSerializer,
    ExecutionAttemptSerializer,
    ExecutionBuildSerializer,
    ExecutionDataMatrixSerializer,
    ExecutionDatasetSerializer,
    ExecutionDefectLinkSerializer,
    ExecutionEventSerializer,
    ExecutionFixtureSerializer,
    ExecutionJobSerializer,
    ExecutionJobVariantSerializer,
    ExecutionNotificationEventSerializer,
    ExecutionNotificationSubscriptionSerializer,
    ExecutionPluginManifestSerializer,
    ExecutionProfileSerializer,
    ExecutionQualityGateEvaluationSerializer,
    ExecutionRetryPolicySerializer,
    ExecutionSourceBindingSerializer,
    ExecutionAISuggestionSerializer,
    ExecutionFailureReviewSerializer,
    ExecutionAISuggestionAuditSerializer,
    ExecutionRequirementLinkSerializer,
    ExecutionStepResultSerializer,
    WebhookEventSerializer,
    RequirementTestCaseLinkSerializer,
    TestCaseDefectLinkSerializer,
    TestCaseRevisionSerializer,
    TestPlanSerializer,
    TestRunSerializer,
    TestRunCaseSerializer,
    TestPlanDetailSerializer,
    TestRunCaseDetailSerializer,
    TestRunCaseHistorySerializer,
    accessible_execution_projects,
)
from .state_machine import InvalidExecutionTransition
from .adapters import AdapterConflict, UnsupportedExecutorError, get_executor_adapter
from .result_events import ResultEventError, ResultEventIngestor, redact_result_payload
from .run_comparison import compare_jobs, evaluate_release_gate
from .failure_intelligence import analyze_job_failures
from .ci import WebhookVerificationError, process_webhook, verify_webhook_request
from .result_import import ResultImportError, import_results
from .policies import (
    AISuggestionDisabled,
    GovernanceConflict,
    check_dispatch_governance,
    record_ai_suggestion,
    review_ai_suggestion,
)
from apps.testcases.models import TestCase
from apps.projects.models import Project
from apps.requirement_analysis.models import BusinessRequirement
from apps.defects.models import Defect
from apps.users.permissions import CIServiceTokenPermission
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from .openapi import (
    CI_WEBHOOK_PARAMETERS,
    EXECUTION_EVIDENCE_PARAMETERS,
    EXECUTION_METRICS_PARAMETERS,
    CIWebhookResponseSerializer,
    ErrorResponseSerializer,
    ExecutionEvidenceExplorerResponseSerializer,
    ExecutionMetricsResponseSerializer,
    ImportErrorResponseSerializer,
    QueryValidationErrorSerializer,
    ResultImportRequestSerializer,
    ResultImportResponseSerializer,
)


def _integer_parameter(params, name, *, default=None, minimum=1, maximum=2 ** 63 - 1, required=False):
    value = params.get(name)
    if value is None:
        if required:
            raise ValidationError({name: '参数不能为空'})
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, str))
        or not str(value).isascii()
        or not str(value).isdecimal()
        or len(str(value)) > 19
    ):
        raise ValidationError({name: '参数必须为有效整数'})
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValidationError({name: f'参数必须在 {minimum} 到 {maximum} 之间'})
    return number


class ExecutionPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

    def get_page_size(self, request):
        return _integer_parameter(
            request.query_params, 'page_size', default=self.page_size, maximum=self.max_page_size,
        )


def get_accessible_projects(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return Project.objects.none()
    if user.is_staff or user.is_superuser:
        return Project.objects.all()
    return Project.objects.filter(
        Q(owner=user) | Q(members=user)
    ).distinct()

class ResultImportView(APIView):
    """Import JUnit or Allure results into one idempotent CI execution job."""

    permission_classes = [IsAuthenticated, CIServiceTokenPermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(
        request={"multipart/form-data": ResultImportRequestSerializer},
        responses={
            200: ResultImportResponseSerializer,
            201: ResultImportResponseSerializer,
            400: ImportErrorResponseSerializer,
            401: ErrorResponseSerializer,
            403: ImportErrorResponseSerializer,
            404: ImportErrorResponseSerializer,
            415: ErrorResponseSerializer,
        },
    )
    def post(self, request):
        project_id = _integer_parameter(request.data, 'project_id', required=True)
        result_format = request.data.get('format')
        external_run_id = request.data.get('external_run_id')
        build_reference = request.data.get('build_reference', '')
        uploaded = request.FILES.get('file')
        if not project_id or not result_format or not external_run_id or uploaded is None:
            return Response(
                {'detail': 'project_id、format、external_run_id 和 file 均为必填项'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if getattr(request.auth, 'project_id', None) and int(project_id) != request.auth.project_id:
            return Response({'detail': 'CI token 无权访问该项目'}, status=status.HTTP_403_FORBIDDEN)
        project = accessible_execution_projects(request.user).filter(pk=project_id).first()
        if project is None:
            return Response({'detail': '项目不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
        payload = uploaded.read(20 * 1024 * 1024 + 1)
        try:
            summary = import_results(
                project=project,
                payload=payload,
                result_format=result_format,
                external_run_id=external_run_id,
                requested_by=request.user,
                build_reference=build_reference,
                filename=uploaded.name,
            )
        except ResultImportError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                'job_id': summary.job.id,
                'created': summary.created,
                'format': summary.format,
                'external_run_id': summary.external_run_id,
                'total': summary.total,
                'passed': summary.passed,
                'failed': summary.failed,
                'skipped': summary.skipped,
                'matched': summary.matched,
                'unmatched': summary.unmatched,
                'status': summary.job.final_status or summary.job.status,
            },
            status=status.HTTP_201_CREATED if summary.created else status.HTTP_200_OK,
        )


class ExecutionMetricsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=EXECUTION_METRICS_PARAMETERS,
        responses={
            200: ExecutionMetricsResponseSerializer,
            400: QueryValidationErrorSerializer,
            401: ErrorResponseSerializer,
        },
    )
    def get(self, request):
        days = _integer_parameter(request.query_params, 'days', default=30, maximum=365)
        since = timezone.now() - timedelta(days=days)
        projects = accessible_execution_projects(request.user)
        project_id = _integer_parameter(request.query_params, 'project')
        if project_id is not None:
            projects = projects.filter(pk=project_id)
        jobs_queryset = ExecutionJob.objects.filter(
            project__in=projects, created_at__gte=since,
        ).order_by()
        total_jobs = jobs_queryset.count()
        ci_summary = jobs_queryset.filter(source_type='ci').annotate(
            unmatched_count=Coalesce(
                Cast(KeyTextTransform('unmatched', 'result'), BigIntegerField()),
                Value(0), output_field=BigIntegerField(),
            ),
        ).aggregate(
            total=Count('pk'),
            associated=Count('pk', filter=Q(unmatched_count=0)),
            unmatched=Sum('unmatched_count', default=0),
        )
        evaluations = ExecutionQualityGateEvaluation.objects.filter(
            job__in=jobs_queryset.values('pk'), stage='release',
        ).aggregate(total=Count('pk'), passed=Count('pk', filter=Q(passed=True)))
        failure_categories = dict(
            ExecutionFailureReview.objects.filter(job__in=jobs_queryset.values('pk'))
            .order_by().values('category').annotate(total=Count('pk'))
            .values_list('category', 'total')
        )
        from apps.quality_intelligence.models import QualityTestProfile
        flaky_count = QualityTestProfile.objects.filter(
            project_type='PROJECT',
            project_id__in=projects.annotate(scope_id=Cast('pk', CharField())).values('scope_id'),
            reliability_status='FLAKY',
        ).count()

        recent_runs = [
            {
                'id': job.pk,
                'project': job.project_id,
                'source_type': job.source_type,
                'source_id': job.source_id,
                'build_reference': job.build_reference,
                'status': job.final_status or job.status,
                'quality_gate': redact_result_payload(job.quality_gate_result or {}),
                'created_at': job.created_at,
                'finished_at': job.finished_at,
                'error_message': redact_result_payload(job.error_message or '')[:512],
            }
            for job in jobs_queryset.order_by('-created_at', '-pk').only(
                'pk', 'project_id', 'source_type', 'source_id', 'build_reference',
                'final_status', 'status', 'quality_gate_result', 'created_at',
                'finished_at', 'error_message',
            )[:10]
        ]
        route_readiness = {}
        for source_type in ('api', 'ui', 'app', 'performance'):
            try:
                adapter = get_executor_adapter(source_type)
                dispatcher = getattr(adapter, 'dispatcher', None)
                supports = getattr(dispatcher, 'supports', None)
                route_readiness[source_type] = bool(callable(supports) and supports(source_type))
            except UnsupportedExecutorError:
                route_readiness[source_type] = False

        binding_counts = dict.fromkeys(('api', 'ui', 'app', 'performance'), 0)
        binding_counts.update(dict(
            ExecutionSourceBinding.objects.filter(project__in=projects, enabled=True)
            .order_by().values('source_type').annotate(total=Count('pk'))
            .values_list('source_type', 'total')
        ))
        last_webhook = WebhookEvent.objects.filter(
            profile__project__in=projects,
        ).only('pk', 'provider', 'status', 'received_at', 'job_id').order_by('-received_at', '-pk').first()
        last_ci_job = ExecutionJob.objects.filter(
            project__in=projects,
            source_type='ci',
        ).only('pk', 'final_status', 'status', 'created_at', 'finished_at').order_by('-created_at', '-pk').first()
        integration_health = {
            'webhook_profiles': ExecutionProfile.objects.filter(
                project__in=projects,
                webhook_enabled=True,
            ).count(),
            'last_webhook': {
                'id': last_webhook.pk,
                'provider': last_webhook.provider,
                'status': last_webhook.status,
                'received_at': last_webhook.received_at,
                'job_id': last_webhook.job_id,
            } if last_webhook else None,
            'last_ci_job': {
                'id': last_ci_job.pk,
                'status': last_ci_job.final_status or last_ci_job.status,
                'created_at': last_ci_job.created_at,
                'finished_at': last_ci_job.finished_at,
            } if last_ci_job else None,
        }
        from apps.quality_intelligence.models import QualityEvidence, QualityFailure, QualityFailureCluster, QualityRun
        from apps.quality_intelligence.permissions import quality_project_filter
        all_jobs = ExecutionJob.objects.filter(project__in=projects)
        artifact_scope = Q(job__project__in=projects) | Q(attempt__job__project__in=projects) | Q(step_result__attempt__job__project__in=projects)
        artifact_queryset = ExecutionArtifact.objects.filter(artifact_scope).distinct()
        artifact_summary = artifact_queryset.aggregate(
            count=Count('id'),
            bytes=Sum('size_bytes'),
            oldest=Min('created_at'),
            latest=Max('created_at'),
        )
        now = timezone.now()
        quality_runs_visible = quality_project_filter(QualityRun.objects.all(), request.user)
        unmapped_quality_runs = 0
        if project_id:
            mapped_run_filter = Q(project_type='PROJECT', project_id=str(project_id))
            quality_runs = quality_runs_visible.filter(mapped_run_filter)
            unmapped_quality_runs = quality_runs_visible.exclude(mapped_run_filter).count()
        else:
            quality_runs = quality_runs_visible
        quality_run_count = quality_runs.count()
        quality_evidence_count = QualityEvidence.objects.filter(run__in=quality_runs).count()
        quality_failure_count = QualityFailure.objects.filter(run__in=quality_runs).count()
        clusters_visible = quality_project_filter(QualityFailureCluster.objects.all(), request.user)
        unmapped_quality_clusters = 0
        if project_id:
            mapped_cluster_filter = Q(mapped_project_id=project_id) | Q(project_type='PROJECT', project_id=str(project_id))
            quality_cluster_count = clusters_visible.filter(mapped_cluster_filter).count()
            unmapped_quality_clusters = clusters_visible.exclude(mapped_cluster_filter).count()
        else:
            quality_cluster_count = clusters_visible.count()
        scale = {
            'execution_jobs': all_jobs.count(),
            'execution_artifacts': int(artifact_summary['count'] or 0),
            'execution_artifact_bytes': int(artifact_summary['bytes'] or 0),
            'expired_execution_artifacts': artifact_queryset.filter(retention_until__isnull=False, retention_until__lt=now).count(),
            'execution_oldest_created_at': artifact_summary['oldest'],
            'execution_latest_created_at': artifact_summary['latest'],
            'quality_runs': quality_run_count,
            'quality_evidence': quality_evidence_count,
            'quality_failures': quality_failure_count,
            'quality_failure_clusters': quality_cluster_count,
            'unmapped_quality_runs': unmapped_quality_runs,
            'unmapped_quality_clusters': unmapped_quality_clusters,
            'thresholds': {
                'execution_jobs': getattr(settings, 'EXECUTION_SCALE_WARNING_JOBS', 100000),
                'execution_artifacts': getattr(settings, 'EXECUTION_SCALE_WARNING_ARTIFACTS', 100000),
                'execution_artifact_bytes': getattr(settings, 'EXECUTION_SCALE_WARNING_ARTIFACT_BYTES', 107374182400),
                'quality_runs': getattr(settings, 'QI_SCALE_WARNING_RUNS', 100000),
                'quality_evidence': getattr(settings, 'QI_SCALE_WARNING_EVIDENCE', 250000),
            },
        }
        scale['warnings'] = [
            key for key, value in (
                ('execution_jobs', scale['execution_jobs']),
                ('execution_artifacts', scale['execution_artifacts']),
                ('execution_artifact_bytes', scale['execution_artifact_bytes']),
                ('quality_runs', scale['quality_runs']),
                ('quality_evidence', scale['quality_evidence']),
            ) if value >= scale['thresholds'][key]
        ]
        return Response({
            'window_days': days,
            'total_jobs': total_jobs,
            'ci_jobs': ci_summary['total'],
            'ci_association_rate': round(ci_summary['associated'] / ci_summary['total'] * 100, 2) if ci_summary['total'] else 0,
            'unmatched_results': ci_summary['unmatched'],
            'gate_evaluations': evaluations['total'],
            'gate_pass_rate': round(evaluations['passed'] / evaluations['total'] * 100, 2) if evaluations['total'] else 0,
            'failure_categories': failure_categories,
            'flaky_profiles': flaky_count,
            'recent_runs': recent_runs,
            'route_readiness': route_readiness,
            'enabled_bindings': binding_counts,
            'integration_health': integration_health,
            'scale': scale,
        })


_EVIDENCE_MAX_OFFSET = 100_000


class ExecutionEvidenceExplorerView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=EXECUTION_EVIDENCE_PARAMETERS,
        responses={
            200: ExecutionEvidenceExplorerResponseSerializer,
            400: OpenApiResponse(
                response=QueryValidationErrorSerializer,
                description="Invalid query parameters, including a computed artifact or quality offset greater than 100,000.",
            ),
            401: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
        },
    )
    def get(self, request):
        params = request.query_params
        project_id = _integer_parameter(params, 'project')
        job_id = _integer_parameter(params, 'job')
        attempt_id = _integer_parameter(params, 'attempt')
        quality_run_id = _integer_parameter(params, 'quality_run')
        limit = _integer_parameter(params, 'limit', default=100, maximum=200)
        page_size = _integer_parameter(params, 'page_size', default=limit, maximum=200)
        page = _integer_parameter(params, 'page', default=1, maximum=1000000)
        artifact_page = _integer_parameter(params, 'artifact_page', default=page, maximum=1000000)
        quality_page = _integer_parameter(params, 'quality_page', default=page, maximum=1000000)
        artifact_size = _integer_parameter(params, 'artifact_page_size', default=page_size, maximum=200)
        quality_size = _integer_parameter(params, 'quality_page_size', default=page_size, maximum=200)
        artifact_offset = (artifact_page - 1) * artifact_size
        quality_offset = (quality_page - 1) * quality_size
        if artifact_offset > _EVIDENCE_MAX_OFFSET or quality_offset > _EVIDENCE_MAX_OFFSET:
            raise ValidationError({'page': 'Requested evidence page exceeds the scan limit; narrow the filters'})
        accessible = accessible_execution_projects(request.user)
        if project_id is not None:
            if not accessible.filter(pk=project_id).exists():
                return Response({'detail': '项目不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
            accessible = accessible.filter(pk=project_id)
        artifact_query = ExecutionArtifact.objects.filter(
            Q(job__project__in=accessible)
            | Q(attempt__job__project__in=accessible)
            | Q(step_result__attempt__job__project__in=accessible)
        ).filter(
            Q(job__isnull=True) | Q(job__project__in=accessible),
            Q(attempt__isnull=True) | Q(attempt__job__project__in=accessible),
            Q(step_result__isnull=True) | Q(step_result__attempt__job__project__in=accessible),
        ).distinct()
        if job_id is not None:
            artifact_query = artifact_query.filter(
                Q(job_id=job_id) | Q(attempt__job_id=job_id) | Q(step_result__attempt__job_id=job_id),
            )
        if attempt_id is not None:
            artifact_query = artifact_query.filter(Q(attempt_id=attempt_id) | Q(step_result__attempt_id=attempt_id))
        if params.get('case_key'):
            artifact_query = artifact_query.filter(step_result__case_key=params['case_key'])
        # These filters intentionally have separate vocabularies/tables.
        if params.get('media_type'):
            artifact_query = artifact_query.filter(media_type=params['media_type'])
        artifact_total = artifact_query.count()
        artifacts = (
            list(artifact_query.order_by('-created_at', '-pk')[artifact_offset:artifact_offset + artifact_size])
            if artifact_offset < artifact_total else []
        )

        quality_rows, quality_total = [], 0
        if getattr(settings, 'QUALITY_INTELLIGENCE_ENABLED', False):
            from apps.quality_intelligence.models import QualityEvidence, QualityRun
            from apps.quality_intelligence.permissions import quality_project_filter
            from apps.quality_intelligence.serializers import QualityEvidenceSerializer
            runs = quality_project_filter(QualityRun.objects.all(), request.user)
            if project_id is not None:
                runs = runs.filter(project_type='PROJECT', project_id=str(project_id))
            if quality_run_id is not None:
                runs = runs.filter(pk=quality_run_id)
            evidence_query = QualityEvidence.objects.filter(run__in=runs)
            if params.get('evidence_type'):
                evidence_query = evidence_query.filter(evidence_type=params['evidence_type'])
            if params.get('availability'):
                evidence_query = evidence_query.filter(availability=params['availability'])
            quality_total = evidence_query.count()
            if quality_offset < quality_total:
                quality_rows = QualityEvidenceSerializer(
                    evidence_query.order_by('-created_at', '-pk')[quality_offset:quality_offset + quality_size], many=True,
                ).data

        return Response({
            'execution_artifacts': ExecutionArtifactSerializer(artifacts, many=True).data,
            'quality_evidence': quality_rows,
            'counts': {'execution_artifacts': artifact_total, 'quality_evidence': quality_total},
            'pagination': {
                'execution_artifacts': {
                    'total': artifact_total, 'page': artifact_page, 'page_size': artifact_size,
                    'has_next': artifact_offset + len(artifacts) < artifact_total,
                },
                'quality_evidence': {
                    'total': quality_total, 'page': quality_page, 'page_size': quality_size,
                    'has_next': (quality_page - 1) * quality_size + len(quality_rows) < quality_total,
                },
            },
        })


class TestPlanViewSet(viewsets.ModelViewSet):
    """
    测试计划视图集
    """
    queryset = TestPlan.objects.all().order_by('-created_at')
    serializer_class = TestPlanSerializer

    def get_queryset(self):
        return self.queryset.filter(
            projects__in=get_accessible_projects(self.request.user)
        ).distinct()

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TestPlanDetailSerializer
        return TestPlanSerializer

    def _validate_project_ids(self, project_ids):
        try:
            requested_ids = {int(project_id) for project_id in project_ids}
        except (TypeError, ValueError):
            raise ValidationError({'projects': '项目 ID 格式错误'})

        accessible_ids = set(
            get_accessible_projects(self.request.user).values_list('id', flat=True)
        )
        if not requested_ids.issubset(accessible_ids):
            raise PermissionDenied('没有权限访问部分项目')
        return requested_ids

    def _sync_test_runs(self, test_plan, project_ids, testcase_ids):
        if not project_ids:
            test_plan.projects.clear()
            test_plan.test_runs.all().delete()
            return

        valid_projects = list(
            Project.objects.filter(id__in=project_ids).filter(
                id__in=get_accessible_projects(self.request.user).values('id')
            )
        )
        valid_project_ids = [project.id for project in valid_projects]
        test_plan.projects.set(valid_project_ids)

        testcase_queryset = TestCase.objects.filter(
            id__in=testcase_ids,
            project_id__in=valid_project_ids
        ).select_related('project')
        testcase_map = {testcase.id: testcase for testcase in testcase_queryset}

        existing_runs = {test_run.project_id: test_run for test_run in test_plan.test_runs.all()}

        for project_id, test_run in list(existing_runs.items()):
            if project_id not in valid_project_ids:
                test_run.delete()
                existing_runs.pop(project_id, None)

        for project in valid_projects:
            test_run = existing_runs.get(project.id)
            if not test_run:
                test_run = TestRun.objects.create(
                    name=f"{test_plan.name} - {project.name} Execution",
                    test_plan=test_plan,
                    project=project,
                    version=test_plan.version,
                    creator=test_plan.creator,
                    assignee=test_plan.creator
                )
                existing_runs[project.id] = test_run
            else:
                test_run.name = f"{test_plan.name} - {project.name} Execution"
                test_run.version = test_plan.version
                test_run.save(update_fields=['name', 'version', 'updated_at'])

            run_testcases = [testcase for testcase in testcase_queryset if testcase.project_id == project.id]
            TestRunCase.objects.filter(test_run=test_run).exclude(
                testcase_id__in=[testcase.id for testcase in run_testcases]
            ).delete()

            existing_case_ids = set(
                TestRunCase.objects.filter(test_run=test_run).values_list('testcase_id', flat=True)
            )
            new_cases = [
                TestRunCase(
                    test_run=test_run,
                    testcase=testcase,
                    priority=testcase.priority
                )
                for testcase in run_testcases
                if testcase.id not in existing_case_ids
            ]
            if new_cases:
                TestRunCase.objects.bulk_create(new_cases)

            test_run.testcases.set([testcase.id for testcase in run_testcases])

    def perform_create(self, serializer):
        # 在创建TestPlan时，设置creator并自动为每个项目创建TestRun和TestRunCase
        project_ids = self.request.data.get('projects', [])
        testcase_ids = self.request.data.get('testcases', [])
        self._validate_project_ids(project_ids)

        version_id = self.request.data.get('version')
        version = None
        if version_id:
            from apps.versions.models import Version
            try:
                version = Version.objects.get(id=version_id)
            except Version.DoesNotExist:
                pass

        test_plan = serializer.save(creator=self.request.user, version=version)
        self._sync_test_runs(test_plan, project_ids, testcase_ids)

    @action(detail=False, methods=['get'])
    def testcases_by_projects(self, request):
        """
        根据项目获取测试用例
        """
        project_ids = request.query_params.getlist('project_ids')
        if not project_ids:
            return Response({
                'error': '请先选择项目',
                'detail': '请先选择项目后再选择测试用例'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # 过滤数字字符串和空值
            project_ids = [int(pid) for pid in project_ids if pid and pid.isdigit()]
            
            if not project_ids:
                return Response({
                    'error': '无效的项目 ID',
                    'detail': '请选择有效的项目'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # 获取指定项目的测试用例
            testcases = TestCase.objects.filter(
                project_id__in=project_ids,
                status__in=['draft', 'active']  # 包含草稿和激活状态的测试用例
            ).filter(
                project_id__in=get_accessible_projects(request.user).values('id')
            ).values('id', 'title', 'priority', 'test_type', 'project__name')
            
            return Response({
                'results': list(testcases)
            })
            
        except ValueError:
            return Response({
                'error': '项目 ID 格式错误',
                'detail': '请提供有效的项目 ID'
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'error': '获取测试用例失败',
                'detail': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def perform_update(self, serializer):
        # 在更新TestPlan时，保留未提交字段，避免普通 PATCH 清空关联项目/用例
        current_plan = serializer.instance
        if 'version' in self.request.data:
            version_id = self.request.data.get('version')
            version = None
            if version_id:
                from apps.versions.models import Version
                try:
                    version = Version.objects.get(id=version_id)
                except Version.DoesNotExist:
                    raise ValidationError({'version': '版本不存在'})
        else:
            version = current_plan.version

        if 'projects' in self.request.data:
            project_ids = self.request.data.get('projects') or []
        else:
            project_ids = list(current_plan.projects.values_list('id', flat=True))
        if 'testcases' in self.request.data:
            testcase_ids = self.request.data.get('testcases') or []
        else:
            testcase_ids = list(current_plan.test_runs.values_list(
                'run_cases__testcase_id', flat=True
            ).distinct())
        self._validate_project_ids(project_ids)
        if version is not None and not version.projects.filter(id__in=project_ids).exists():
            raise ValidationError({'version': '版本不属于所选项目'})

        with transaction.atomic():
            test_plan = serializer.save(version=version)
            self._sync_test_runs(test_plan, project_ids, testcase_ids)

            if 'assignees' in self.request.data:
                assignee_ids = self.request.data.get('assignees') or []
                test_plan.assignees.set(assignee_ids)


class TestRunViewSet(viewsets.ModelViewSet):
    """
    测试执行视图集
    """
    queryset = TestRun.objects.all().order_by('-created_at')
    serializer_class = TestRunSerializer

    def get_queryset(self):
        return self.queryset.filter(
            project__in=get_accessible_projects(self.request.user)
        ).distinct()

class TestRunCaseViewSet(viewsets.ModelViewSet):
    """
    测试执行用例视图集
    """
    queryset = TestRunCase.objects.all()
    serializer_class = TestRunCaseSerializer

    def get_queryset(self):
        return self.queryset.filter(
            test_run__project__in=get_accessible_projects(self.request.user)
        ).distinct()

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TestRunCaseDetailSerializer
        return TestRunCaseSerializer

    @action(detail=True, methods=['patch'])
    def update_status(self, request, pk=None):
        """
        更新单个用例的执行状态，并自动创建历史记录
        """
        run_case = self.get_object()
        new_status = request.data.get('status')
        actual_result = request.data.get('actual_result', '')
        comments = request.data.get('comments', '')
        
        if not new_status:
            return Response({'error': 'Status is required'}, status=status.HTTP_400_BAD_REQUEST)
        valid_statuses = {value for value, _label in TestRunCase.STATUS_CHOICES}
        if not isinstance(new_status, str) or new_status not in valid_statuses:
            return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)
        
        # 创建历史记录
        TestRunCaseHistory.objects.create(
            run_case=run_case,
            status=new_status,
            actual_result=actual_result,
            comments=comments,
            executed_by=request.user,
            executed_at=timezone.now()
        )
        
        # 更新执行用例状态
        run_case.status = new_status
        run_case.actual_result = actual_result
        run_case.comments = comments
        run_case.executed_by = request.user
        run_case.executed_at = timezone.now()
        run_case.save()
        
        return Response(TestRunCaseDetailSerializer(run_case).data)

    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        """
        获取用例执行历史记录
        """
        run_case = self.get_object()
        history = run_case.history.all().order_by('-executed_at')
        serializer = TestRunCaseHistorySerializer(history, many=True)
        return Response(serializer.data)

class TestRunCaseHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    测试执行历史视图集（只读）
    """
    queryset = TestRunCaseHistory.objects.all().order_by('-executed_at')
    serializer_class = TestRunCaseHistorySerializer

    def get_queryset(self):
        return self.queryset.filter(
            run_case__test_run__project__in=get_accessible_projects(self.request.user)
        ).distinct()


class ExecutionJobViewSet(viewsets.ModelViewSet):
    """Minimal scoped API for durable execution requests.

    The endpoint intentionally records a pending job only.  Dispatching to an
    executor is a later adapter concern and is reported explicitly as
    ``unsupported`` in create/read responses.
    """

    queryset = ExecutionJob.objects.select_related('project', 'dataset', 'version', 'requested_by').prefetch_related('variants')
    serializer_class = ExecutionJobSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'source_type', 'trigger', 'priority', 'project']
    search_fields = ['source_id', 'build_reference', 'error_message', 'worker']
    ordering_fields = ['created_at', 'updated_at', 'queued_at', 'started_at', 'finished_at', 'priority', 'status']
    ordering = ['-created_at']
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        queryset = self.queryset.filter(
            project__in=accessible_execution_projects(self.request.user)
        ).distinct()
        if getattr(self.request.auth, 'project_id', None):
            queryset = queryset.filter(project_id=self.request.auth.project_id)
        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset()).annotate(_attempts_count=Count('attempts', distinct=True))
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(queryset, many=True).data)

    def get_serializer(self, *args, **kwargs):
        if args and 'data' not in kwargs:
            from .source_contract import SourceBindingError, parse_source_id
            instances = list(args[0]) if kwargs.get('many') else [args[0]]
            references = set()
            for job in instances:
                if job.source_type not in {'api', 'ui', 'app', 'performance'}:
                    continue
                try:
                    ref = parse_source_id(job.source_type, job.source_id)
                except SourceBindingError:
                    continue
                references.add((job.project_id, ref.source_type, ref.canonical))
            context = kwargs.setdefault('context', self.get_serializer_context())
            bindings = {}
            if references:
                # Inputs are limited to this response page, never the full job
                # history; the serializer must not issue per-row lookups.
                bindings = {
                    (project_id, source_type, source_id): enabled
                    for project_id, source_type, source_id, enabled in ExecutionSourceBinding.objects.filter(
                        project_id__in={item[0] for item in references},
                        source_type__in={item[1] for item in references},
                        source_id__in={item[2] for item in references},
                    ).values_list('project_id', 'source_type', 'source_id', 'enabled')
                }
            context['source_binding_statuses'] = bindings
            if kwargs.get('many'):
                args = (instances, *args[1:])
        return super().get_serializer(*args, **kwargs)

    def create(self, request, *args, **kwargs):
        header_key = request.headers.get('Idempotency-Key') or request.META.get(
            'HTTP_IDEMPOTENCY_KEY'
        )
        body_key = request.data.get('idempotency_key')
        if header_key and body_key and header_key != body_key:
            return Response(
                {'detail': 'Idempotency-Key header does not match request body'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        project_id = _integer_parameter(request.data, 'project')
        if getattr(request.auth, 'project_id', None) and project_id and project_id != request.auth.project_id:
            return Response({'detail': 'CI token 无权访问该项目'}, status=status.HTTP_403_FORBIDDEN)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if header_key:
                job = serializer.save(idempotency_key=header_key)
            else:
                job = serializer.save()
        except IdempotencyConflict as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)

        response_serializer = self.get_serializer(job)
        response_status = (
            status.HTTP_201_CREATED if getattr(serializer, '_created', True)
            else status.HTTP_200_OK
        )
        return Response(response_serializer.data, status=response_status)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Record a cancellation intent without invoking an executor."""
        reason = request.data.get('reason', '') if isinstance(request.data, dict) else ''
        reason = str(reason or '').strip()
        with transaction.atomic():
            job = self.get_queryset().select_for_update().get(pk=pk)
            if job.status in {
                'cancelled', 'passed', 'succeeded', 'completed', 'failed',
                'blocked', 'unsupported',
            }:
                if job.status != 'cancelled':
                    return Response(
                        {'detail': '该执行任务已结束，无法取消', 'status': job.status},
                        status=status.HTTP_409_CONFLICT,
                    )
            else:
                try:
                    job.request_cancel(reason)
                except InvalidExecutionTransition as exc:
                    return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)

        return Response(self.get_serializer(job).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='dispatch')
    def dispatch_job(self, request, pk=None):
        """Dispatch through the source adapter when governance allows it."""
        job = self.get_object()
        governance = check_dispatch_governance(
            job,
            override=bool(request.data.get('quarantine_override')),
            override_reason=str(request.data.get('override_reason') or ''),
            actor=request.user,
        )
        if not governance.get('allowed'):
            return Response(
                {
                    'detail': '执行任务被治理策略阻止',
                    'governance': {
                        'reason': governance.get('reason'),
                        'quarantine_id': getattr(governance.get('quarantine'), 'pk', None),
                        'details': governance.get('details', {}),
                    },
                },
                status=status.HTTP_409_CONFLICT,
            )
        try:
            adapter = get_executor_adapter(job.source_type)
        except UnsupportedExecutorError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        try:
            receipt = adapter.submit(
                job,
                worker=str(request.data.get('worker') or ''),
                quarantine_override=bool(request.data.get('quarantine_override')),
                override_reason=str(request.data.get('override_reason') or ''),
                requested_by=request.user,
            )
        except AdapterConflict as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        job.refresh_from_db()
        response_status = status.HTTP_202_ACCEPTED if receipt.supported else status.HTTP_409_CONFLICT
        return Response(
            {
                'job': self.get_serializer(job).data,
                'receipt': {
                    'attempt_id': receipt.attempt_id,
                    'attempt_number': receipt.attempt_number,
                    'status': receipt.status,
                    'supported': receipt.supported,
                    'message': receipt.message,
                    'details': receipt.details,
                },
                'governance': {
                    'allowed': True,
                    'reason': governance.get('reason'),
                },
            },
            status=response_status,
        )

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        """Queue a new attempt and retain the operator's retry reason."""
        job = self.get_object()
        try:
            adapter = get_executor_adapter(job.source_type)
        except UnsupportedExecutorError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        reason = str(request.data.get('reason') or 'manual retry requested')[:4000]
        automatic = bool(request.data.get('automatic'))
        try:
            receipt = adapter.retry(
                job,
                reason=reason,
                requested_by=request.user,
                automatic=automatic,
            )
        except AdapterConflict as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        job.refresh_from_db()
        return Response(
            {
                'job': self.get_serializer(job).data,
                'receipt': {
                    'attempt_id': receipt.attempt_id,
                    'attempt_number': receipt.attempt_number,
                    'status': receipt.status,
                    'supported': receipt.supported,
                    'message': receipt.message,
                    'details': receipt.details,
                },
            },
            status=status.HTTP_202_ACCEPTED,
        )
    @action(detail=True, methods=['post'], url_path='failure-review')
    def failure_review(self, request, pk=None):
        job = self.get_object()
        case_key = str(request.data.get('case_key') or '').strip()[:255]
        signature = str(request.data.get('signature') or '').strip()[:128]
        category = str(request.data.get('category') or 'UNKNOWN').upper()
        note = str(request.data.get('note') or '')[:4000]
        valid = {choice[0] for choice in ExecutionFailureReview.CATEGORY_CHOICES}
        if not case_key or not signature or category not in valid:
            return Response({'detail': 'case_key、signature 和有效 category 均为必填项'}, status=status.HTTP_400_BAD_REQUEST)
        review, created = ExecutionFailureReview.objects.get_or_create(
            job=job,
            case_key=case_key,
            signature=signature,
            defaults={'category': category, 'note': note, 'reviewed_by': request.user, 'reviewed_at': timezone.now()},
        )
        if not created:
            review.history = [*(review.history or []), {
                'category': review.category,
                'note': review.note,
                'reviewed_by': review.reviewed_by_id,
                'reviewed_at': review.reviewed_at.isoformat() if review.reviewed_at else None,
            }][-20:]
            review.category = category
            review.note = note
            review.reviewed_by = request.user
            review.reviewed_at = timezone.now()
            review.save(update_fields=['category', 'note', 'reviewed_by', 'reviewed_at', 'history', 'updated_at'])
        return Response(ExecutionFailureReviewSerializer(review).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='failure-analysis')
    def failure_analysis(self, request, pk=None):
        job = self.get_object()
        return Response(analyze_job_failures(job))

    @action(detail=True, methods=['get'], url_path='ci-status')
    def ci_status(self, request, pk=None):
        job = self.get_object()
        status_value = job.final_status or job.status
        gate = job.quality_gate_result or {}
        terminal = status_value in {
            'cancelled', 'passed', 'succeeded', 'completed', 'failed', 'blocked', 'unsupported',
        }
        exit_code = None
        if terminal:
            exit_code = 0 if status_value in {'passed', 'succeeded', 'completed'} and gate.get('passed', True) else 1
        return Response({
            'job_id': job.pk,
            'status': status_value,
            'terminal': terminal,
            'exit_code': exit_code,
            'quality_gate': gate,
            'final_result': job.final_result or {},
            'error_message': job.error_message or '',
        })

    @action(detail=True, methods=['post'], url_path='compare')
    def compare(self, request, pk=None):
        job = self.get_object()
        baseline_id = _integer_parameter(request.data, 'baseline_job_id', required=True)
        baseline = self.get_queryset().filter(pk=baseline_id).first()
        if baseline is None:
            return Response({'detail': 'baseline_job_id 不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
        try:
            result = compare_jobs(job, baseline)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=['post'], url_path='release-gate')
    def release_gate(self, request, pk=None):
        job = self.get_object()
        baseline_id = _integer_parameter(request.data, 'baseline_job_id')
        baseline = None
        if baseline_id:
            baseline = self.get_queryset().filter(pk=baseline_id).first()
            if baseline is None:
                return Response({'detail': 'baseline_job_id 不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
        try:
            max_failed = _integer_parameter(request.data, 'max_failed', default=0, minimum=0)
            max_unmatched = _integer_parameter(request.data, 'max_unmatched', default=0, minimum=0)
            policy_version = str(request.data.get('policy_version') or '').strip()[:128]
            policy_snapshot = request.data.get('policy_snapshot') or {}
            metric_thresholds = request.data.get('metric_thresholds') or {}
            if not isinstance(policy_snapshot, dict):
                raise ValueError('policy_snapshot 必须是对象')
            if not isinstance(metric_thresholds, dict):
                raise ValueError('metric_thresholds 必须是对象')
            result = evaluate_release_gate(
                job,
                baseline=baseline,
                max_failed=max_failed,
                max_unmatched=max_unmatched,
                policy_version=policy_version,
                policy_snapshot=policy_snapshot,
                metric_thresholds=metric_thresholds,
                evaluated_by=request.user,
            )
        except (TypeError, ValueError) as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if not result['passed']:
            from .notifications import emit_project_notification
            emit_project_notification(
                job.project,
                'gate_failed',
                f'gate:{job.pk}:{result.get("evaluation_id")}',
                {
                    'job_id': job.pk,
                    'project_id': job.project_id,
                    'policy_version': result.get('policy_version', ''),
                    'failures': result.get('failures', []),
                },
            )
        return Response(result, status=status.HTTP_200_OK if result['passed'] else status.HTTP_409_CONFLICT)

    @action(detail=True, methods=['post'], url_path='events')
    def events(self, request, pk=None):
        job = self.get_object()
        event_type = request.data.get('event_type')
        payload = request.data.get('payload') or {}
        idempotency_key = request.headers.get('Idempotency-Key') or request.data.get('idempotency_key', '')
        try:
            receipt = ResultEventIngestor.ingest(
                job,
                event_type,
                payload,
                idempotency_key=str(idempotency_key or ''),
            )
        except ResultEventError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'event_id': receipt.event_id,
            'created': receipt.created,
            'status': receipt.status,
            'attempt_id': receipt.attempt_id,
            'event_type': receipt.event_type,
        }, status=status.HTTP_201_CREATED if receipt.created else status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='timeline')
    def timeline(self, request, pk=None):
        """Return one permission-scoped job with attempts, steps and artifacts."""
        job = self.get_object()
        attempts = list(job.attempts.order_by('attempt_number'))
        steps = list(ExecutionStepResult.objects.filter(
            attempt__job=job,
        ).select_related('attempt').order_by('attempt_id', 'case_key', 'step_key'))
        artifacts = list(ExecutionArtifact.objects.filter(
            Q(job=job) | Q(attempt__job=job) | Q(step_result__attempt__job=job)
        ).distinct().order_by('-created_at'))
        ai_suggestions = list(job.ai_suggestions.order_by('-created_at'))
        return Response({
            'job': self.get_serializer(job).data,
            'attempts': ExecutionAttemptSerializer(attempts, many=True).data,
            'variants': ExecutionJobVariantSerializer(job.variants.order_by('ordinal'), many=True).data,
            'steps': ExecutionStepResultSerializer(steps, many=True).data,
            'artifacts': ExecutionArtifactSerializer(artifacts, many=True).data,
            'ai_suggestions': ExecutionAISuggestionSerializer(ai_suggestions, many=True).data,
        })


class ExecutionJobVariantViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only per-dataset-variant status and result projection."""

    queryset = ExecutionJobVariant.objects.select_related('job', 'job__project', 'job__dataset')
    serializer_class = ExecutionJobVariantSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['job', 'status', 'variant_key']
    ordering = ['job_id', 'ordinal']

    def get_queryset(self):
        return self.queryset.filter(
            job__project__in=accessible_execution_projects(self.request.user)
        ).distinct()


class ExecutionAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only attempt history scoped through the owning project."""

    queryset = ExecutionAttempt.objects.select_related('job', 'job__project')
    serializer_class = ExecutionAttemptSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['job', 'status', 'worker']
    ordering_fields = ['created_at', 'started_at', 'finished_at', 'attempt_number', 'status']
    ordering = ['job_id', 'attempt_number']

    def get_queryset(self):
        return self.queryset.filter(
            job__project__in=accessible_execution_projects(self.request.user)
        ).distinct()


class ExecutionStepResultViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only stable case/step results scoped through the owning project."""

    queryset = ExecutionStepResult.objects.select_related('attempt', 'attempt__job__project')
    serializer_class = ExecutionStepResultSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['attempt', 'status', 'case_key', 'step_key']
    ordering_fields = ['created_at', 'started_at', 'finished_at', 'duration_ms', 'status']
    ordering = ['attempt_id', 'case_key', 'step_key']

    def get_queryset(self):
        return self.queryset.filter(
            attempt__job__project__in=accessible_execution_projects(self.request.user)
        ).distinct()


class ExecutionArtifactViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only artifact references scoped through the owning project."""

    queryset = ExecutionArtifact.objects.select_related('job', 'attempt', 'step_result')
    serializer_class = ExecutionArtifactSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['job', 'attempt', 'media_type', 'retention_policy']
    ordering_fields = ['created_at', 'retention_until', 'size_bytes']
    ordering = ['-created_at']

    def get_queryset(self):
        accessible = accessible_execution_projects(self.request.user)
        return self.queryset.filter(
            Q(job__project__in=accessible)
            | Q(attempt__job__project__in=accessible)
            | Q(step_result__attempt__job__project__in=accessible)
        ).distinct()


class ExecutionEventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ExecutionEvent.objects.select_related('job', 'attempt')
    serializer_class = ExecutionEventSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['job', 'attempt', 'event_type', 'status', 'case_key', 'step_key']
    ordering = ['created_at', 'id']

    def get_queryset(self):
        return self.queryset.filter(
            job__project__in=accessible_execution_projects(self.request.user)
        ).distinct()


class ExecutionBuildViewSet(viewsets.ModelViewSet):
    queryset = ExecutionBuild.objects.select_related('project', 'version', 'created_by')
    serializer_class = ExecutionBuildSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['project', 'version', 'status', 'branch', 'environment']
    search_fields = ['name', 'build_number', 'branch', 'commit_sha']
    ordering_fields = ['created_at', 'updated_at', 'build_number', 'status']
    ordering = ['-created_at']

    def get_queryset(self):
        return self.queryset.filter(
            project__in=accessible_execution_projects(self.request.user)
        ).distinct()

    def perform_create(self, serializer):
        project = serializer.validated_data['project']
        version = serializer.validated_data.get('version')
        if version is not None and not version.projects.filter(pk=project.pk).exists():
            raise ValidationError({'version': '版本不属于所选项目'})
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=['get'])
    def coverage(self, request):
        from .control_plane import requirement_coverage
        from apps.versions.models import Version
        project_id = _integer_parameter(request.query_params, 'project', required=True)
        version_id = _integer_parameter(request.query_params, 'version')
        if not accessible_execution_projects(request.user).filter(pk=project_id).exists():
            raise PermissionDenied('没有权限访问该项目')
        if version_id is not None and not Version.objects.filter(pk=version_id, projects__pk=project_id).exists():
            raise ValidationError({'version': '版本不属于所选项目'})
        return Response(requirement_coverage(project_id, version_id))


class RequirementTestCaseLinkViewSet(viewsets.ModelViewSet):
    queryset = RequirementTestCaseLink.objects.select_related('requirement', 'test_case', 'created_by')
    serializer_class = RequirementTestCaseLinkSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['requirement', 'test_case']
    ordering = ['-created_at']

    def get_queryset(self):
        accessible = accessible_execution_projects(self.request.user)
        return self.queryset.filter(
            test_case__project__in=accessible,
            requirement__analysis__document__project__in=accessible,
        ).distinct()

    def perform_create(self, serializer):
        requirement = serializer.validated_data['requirement']
        test_case = serializer.validated_data['test_case']
        if requirement.analysis.document.project_id != test_case.project_id:
            raise ValidationError('需求和测试用例必须属于同一项目')
        if not accessible_execution_projects(self.request.user).filter(pk=test_case.project_id).exists():
            raise PermissionDenied('没有权限关联该项目')
        serializer.save(created_by=self.request.user)


class TestCaseDefectLinkViewSet(viewsets.ModelViewSet):
    queryset = TestCaseDefectLink.objects.select_related('test_case', 'defect', 'created_by')
    serializer_class = TestCaseDefectLinkSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['test_case', 'defect']
    ordering = ['-created_at']

    def get_queryset(self):
        return self.queryset.filter(
            test_case__project__in=accessible_execution_projects(self.request.user),
            defect__project__in=accessible_execution_projects(self.request.user),
        ).distinct()

    def perform_create(self, serializer):
        test_case = serializer.validated_data['test_case']
        defect = serializer.validated_data['defect']
        if test_case.project_id != defect.project_id:
            raise ValidationError('测试用例和缺陷必须属于同一项目')
        if not accessible_execution_projects(self.request.user).filter(pk=test_case.project_id).exists():
            raise PermissionDenied('没有权限关联该项目')
        serializer.save(created_by=self.request.user)


class ExecutionRequirementLinkViewSet(viewsets.ModelViewSet):
    queryset = ExecutionRequirementLink.objects.select_related('job', 'requirement')
    serializer_class = ExecutionRequirementLinkSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['job', 'requirement', 'relation_type']

    def get_queryset(self):
        return self.queryset.filter(
            job__project__in=accessible_execution_projects(self.request.user)
        ).distinct()

    def perform_create(self, serializer):
        job = serializer.validated_data['job']
        requirement = serializer.validated_data['requirement']
        if requirement.analysis.document.project_id != job.project_id:
            raise ValidationError('需求和执行任务必须属于同一项目')
        serializer.save()


class ExecutionDefectLinkViewSet(viewsets.ModelViewSet):
    queryset = ExecutionDefectLink.objects.select_related('job', 'defect')
    serializer_class = ExecutionDefectLinkSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['job', 'defect', 'relation_type']

    def get_queryset(self):
        return self.queryset.filter(
            job__project__in=accessible_execution_projects(self.request.user)
        ).distinct()

    def perform_create(self, serializer):
        job = serializer.validated_data['job']
        defect = serializer.validated_data['defect']
        if defect.project_id != job.project_id:
            raise ValidationError('缺陷和执行任务必须属于同一项目')
        serializer.save()


class TestCaseRevisionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TestCaseRevision.objects.select_related('test_case', 'changed_by')
    serializer_class = TestCaseRevisionSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['test_case', 'revision']
    ordering = ['test_case_id', '-revision']

    def get_queryset(self):
        return self.queryset.filter(
            test_case__project__in=accessible_execution_projects(self.request.user)
        ).distinct()


class ExecutionSourceBindingViewSet(viewsets.ModelViewSet):
    queryset = ExecutionSourceBinding.objects.select_related('project', 'created_by')
    serializer_class = ExecutionSourceBindingSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['project', 'source_type', 'enabled', 'source_id']
    ordering = ['project_id', 'source_type', 'source_id']
    http_method_names = ['get', 'post', 'put', 'patch', 'head', 'options']

    def get_queryset(self):
        queryset = self.queryset.filter(
            project__in=accessible_execution_projects(self.request.user)
        ).distinct()
        if getattr(self.request.auth, 'project_id', None):
            queryset = queryset.filter(project_id=self.request.auth.project_id)
        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        project = serializer.validated_data.get('project', self.get_object().project)
        if not accessible_execution_projects(self.request.user).filter(pk=project.pk).exists():
            raise PermissionDenied('没有权限修改该项目的执行 source binding')
        serializer.save()

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        binding = self.get_object()
        binding.enabled = False
        binding.save(update_fields=['enabled', 'updated_at'])
        return Response(self.get_serializer(binding).data)


class ExecutionProfileViewSet(viewsets.ModelViewSet):
    queryset = ExecutionProfile.objects.select_related('project', 'version', 'created_by')
    serializer_class = ExecutionProfileSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['project', 'provider', 'source_type', 'enabled', 'webhook_enabled']
    search_fields = ['name', 'description']
    ordering = ['project_id', 'name']

    def get_queryset(self):
        return self.queryset.filter(
            project__in=accessible_execution_projects(self.request.user)
        ).distinct()

    def perform_create(self, serializer):
        project = serializer.validated_data['project']
        if not accessible_execution_projects(self.request.user).filter(pk=project.pk).exists():
            raise PermissionDenied('没有权限创建该项目的执行配置')
        version = serializer.validated_data.get('version')
        if version is not None and not version.projects.filter(pk=project.pk).exists():
            raise ValidationError({'version': '版本不属于所选项目'})
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        project = serializer.validated_data.get('project', self.get_object().project)
        if not accessible_execution_projects(self.request.user).filter(pk=project.pk).exists():
            raise PermissionDenied('没有权限修改该项目的执行配置')
        serializer.save()


class ExecutionRetryPolicyViewSet(viewsets.ModelViewSet):
    queryset = ExecutionRetryPolicy.objects.select_related('project', 'profile', 'created_by')
    serializer_class = ExecutionRetryPolicySerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['project', 'profile', 'enabled', 'case_key']
    ordering = ['project_id', 'profile_id', 'case_key', 'id']

    def get_queryset(self):
        return self.queryset.filter(project__in=accessible_execution_projects(self.request.user)).distinct()

    def _validate_scope(self, serializer):
        if serializer.instance is None:
            project = serializer.validated_data.get('project')
            profile = serializer.validated_data.get('profile')
        else:
            project = serializer.validated_data.get('project', serializer.instance.project)
            profile = serializer.validated_data.get('profile', serializer.instance.profile)
        if project is None or not accessible_execution_projects(self.request.user).filter(pk=project.pk).exists():
            raise PermissionDenied('没有权限管理该项目的重试策略')
        if profile is not None and profile.project_id != project.pk:
            raise ValidationError({'profile': '执行配置不属于所选项目'})

    def perform_create(self, serializer):
        self._validate_scope(serializer)
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        self._validate_scope(serializer)
        serializer.save()


class ExecutionQualityGateEvaluationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ExecutionQualityGateEvaluation.objects.select_related('job', 'profile', 'job__project', 'evaluated_by')
    serializer_class = ExecutionQualityGateEvaluationSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['job', 'profile', 'stage', 'passed']
    ordering = ['-created_at']

    def get_queryset(self):
        return self.queryset.filter(job__project__in=accessible_execution_projects(self.request.user)).distinct()


class ExecutionAISuggestionViewSet(viewsets.ModelViewSet):
    queryset = ExecutionAISuggestion.objects.select_related('project', 'job', 'requested_by', 'reviewed_by')
    serializer_class = ExecutionAISuggestionSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['project', 'job', 'kind', 'status']
    ordering = ['-created_at']
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        return self.queryset.filter(project__in=accessible_execution_projects(self.request.user)).distinct()

    def create(self, request, *args, **kwargs):
        project = Project.objects.filter(
            pk=request.data.get('project'),
            pk__in=accessible_execution_projects(request.user).values('pk'),
        ).first()
        if project is None:
            return Response({'detail': '项目不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
        job = None
        if request.data.get('job'):
            from .models import ExecutionJob
            job = ExecutionJob.objects.filter(
                pk=request.data.get('job'),
                project=project,
            ).first()
            if job is None:
                return Response({'detail': '执行任务不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
        try:
            suggestion = record_ai_suggestion(
                project=project,
                job=job,
                kind=str(request.data.get('kind') or ''),
                prompt_version=str(request.data.get('prompt_version') or '1'),
                model_provider=str(request.data.get('model_provider') or ''),
                model_name=str(request.data.get('model_name') or ''),
                target_type=str(request.data.get('target_type') or ''),
                target_key=str(request.data.get('target_key') or ''),
                source_fingerprint=str(request.data.get('source_fingerprint') or ''),
                input_payload=request.data.get('input') or request.data.get('input_redacted') or {},
                output_payload=request.data.get('output') or request.data.get('output_redacted') or {},
                input_tokens=request.data.get('input_tokens', 0),
                output_tokens=request.data.get('output_tokens', 0),
                cost_micros=request.data.get('cost_micros', 0),
                requested_by=request.user,
            )
        except AISuggestionDisabled as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except (ValueError, TypeError) as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(suggestion).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='approve')
    def approve(self, request, pk=None):
        return self._review(request, pk, 'approved')

    @action(detail=True, methods=['post'], url_path='reject')
    def reject(self, request, pk=None):
        return self._review(request, pk, 'rejected')

    def _review(self, request, pk, review_status):
        suggestion = self.get_object()
        try:
            suggestion = review_ai_suggestion(
                suggestion,
                request.user,
                review_status,
                str(request.data.get('note') or '')[:4000],
            )
        except GovernanceConflict as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(suggestion).data)


class ExecutionAISuggestionAuditViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ExecutionAISuggestionAudit.objects.select_related('suggestion', 'actor', 'suggestion__project')
    serializer_class = ExecutionAISuggestionAuditSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['suggestion', 'action', 'actor']
    ordering = ['-created_at']

    def get_queryset(self):
        return self.queryset.filter(
            suggestion__project__in=accessible_execution_projects(self.request.user)
        ).distinct()


class ExecutionFailureReviewViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ExecutionFailureReview.objects.select_related('job', 'job__project', 'reviewed_by')
    serializer_class = ExecutionFailureReviewSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['job', 'category', 'case_key']
    ordering = ['-updated_at']

    def get_queryset(self):
        return self.queryset.filter(job__project__in=accessible_execution_projects(self.request.user)).distinct()


class WebhookEventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = WebhookEvent.objects.select_related('profile', 'profile__project', 'job')
    serializer_class = WebhookEventSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['profile', 'provider', 'status', 'job']
    ordering = ['-received_at']

    def get_queryset(self):
        return self.queryset.filter(
            profile__project__in=accessible_execution_projects(self.request.user)
        ).distinct()


class CIWebhookView(APIView):
    """Receive signed CI events; no outbound callback is performed by default."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        parameters=CI_WEBHOOK_PARAMETERS,
        description=(
            "Unauthenticated webhook receiver. Supply X-TestHub-Profile and an event ID header "
            "(X-TestHub-Event, X-GitHub-Delivery, X-Gitlab-Event-UUID, or X-Request-ID). GitHub accepts "
            "X-Hub-Signature-256 or X-TestHub-Signature. Other providers require X-TestHub-Timestamp and "
            "X-TestHub-Signature; the timestamp must be within five minutes."
        ),
        request={
            "application/json": OpenApiTypes.OBJECT,
            "application/x-www-form-urlencoded": OpenApiTypes.OBJECT,
            "multipart/form-data": OpenApiTypes.OBJECT,
        },
        responses={
            200: CIWebhookResponseSerializer,
            202: CIWebhookResponseSerializer,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
            415: ErrorResponseSerializer,
        },
    )
    def post(self, request, provider, *args, **kwargs):
        profile_key = request.headers.get('X-TestHub-Profile', '').strip()
        if not profile_key:
            return Response({'detail': 'X-TestHub-Profile is required'}, status=status.HTTP_400_BAD_REQUEST)
        profile = ExecutionProfile.objects.filter(
            pk=profile_key,
            provider=provider,
            enabled=True,
            webhook_enabled=True,
        ).first()
        if profile is None:
            return Response({'detail': 'Webhook profile not found or disabled'}, status=status.HTTP_404_NOT_FOUND)
        try:
            event_id, payload_hash = verify_webhook_request(request, profile, provider)
            payload = request.data if isinstance(request.data, dict) else {}
            receipt = process_webhook(profile, provider, event_id, payload, payload_hash)
        except WebhookVerificationError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_401_UNAUTHORIZED)
        except (ValidationError, ValueError) as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'event_id': receipt.event_id,
            'created': receipt.created,
            'job_id': receipt.job_id,
            'status': receipt.status,
        }, status=status.HTTP_202_ACCEPTED if receipt.created else status.HTTP_200_OK)


class ExecutionDatasetViewSet(viewsets.ModelViewSet):
    queryset = ExecutionDataset.objects.select_related('project', 'created_by')
    serializer_class = ExecutionDatasetSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['project', 'enabled']
    search_fields = ['name', 'description']
    ordering_fields = ['id', 'project', 'name', 'created_at', 'updated_at']
    ordering = ['project_id', 'name', 'id']

    def get_queryset(self):
        return self.queryset.filter(project__in=accessible_execution_projects(self.request.user)).distinct()

    def perform_create(self, serializer):
        project = serializer.validated_data['project']
        if not accessible_execution_projects(self.request.user).filter(pk=project.pk).exists():
            raise PermissionDenied('没有权限创建该项目的数据集')
        serializer.save(created_by=self.request.user)


class ExecutionDataMatrixViewSet(viewsets.ModelViewSet):
    queryset = ExecutionDataMatrix.objects.select_related('dataset', 'dataset__project')
    serializer_class = ExecutionDataMatrixSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['dataset', 'enabled']
    ordering = ['dataset_id', 'name']

    def get_queryset(self):
        return self.queryset.filter(
            dataset__project__in=accessible_execution_projects(self.request.user)
        ).distinct()


class ExecutionFixtureViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ExecutionFixture.objects.select_related('project', 'dataset', 'created_by')
    serializer_class = ExecutionFixtureSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['project', 'dataset', 'run_id', 'status']
    ordering = ['-created_at']

    def get_queryset(self):
        return self.queryset.filter(project__in=accessible_execution_projects(self.request.user)).distinct()

    @action(detail=True, methods=['post'])
    def cleanup(self, request, pk=None):
        fixture = self.get_object()
        from .datasets import cleanup_fixture
        result = cleanup_fixture(fixture)
        return Response(result)


class ExecutionNotificationSubscriptionViewSet(viewsets.ModelViewSet):
    queryset = ExecutionNotificationSubscription.objects.select_related('project', 'channel', 'created_by')
    serializer_class = ExecutionNotificationSubscriptionSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['project', 'event_type', 'enabled', 'channel']
    http_method_names = ['get', 'post', 'put', 'patch', 'head', 'options']
    ordering = ['project_id', 'event_type', 'id']

    def get_queryset(self):
        return self.queryset.filter(project__in=accessible_execution_projects(self.request.user)).distinct()

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class ExecutionNotificationEventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ExecutionNotificationEvent.objects.select_related('project', 'subscription', 'subscription__channel')
    serializer_class = ExecutionNotificationEventSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['project', 'event_type', 'status', 'subscription']

    def get_queryset(self):
        return self.queryset.filter(project__in=accessible_execution_projects(self.request.user)).distinct()


class ExecutionPluginManifestViewSet(viewsets.ModelViewSet):
    queryset = ExecutionPluginManifest.objects.all()
    serializer_class = ExecutionPluginManifestSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['project', 'kind', 'enabled']
    search_fields = ['name', 'version']
    ordering_fields = ['id', 'name', 'version', 'kind', 'created_at', 'updated_at']
    ordering = ['id']

    def get_queryset(self):
        return self.queryset.filter(
            Q(project__isnull=True) | Q(project__in=accessible_execution_projects(self.request.user))
        ).distinct()

    def perform_create(self, serializer):
        project = serializer.validated_data.get('project')
        if project is not None and not accessible_execution_projects(self.request.user).filter(pk=project.pk).exists():
            raise PermissionDenied('没有权限创建该项目插件声明')
        serializer.save()
