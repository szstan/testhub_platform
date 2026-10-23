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
from django.db import transaction
from django.db.models import Q
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
    ExecutionPluginManifest,
    ExecutionProfile,
    ExecutionQualityGateEvaluation,
    ExecutionRetryPolicy,
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
    ExecutionPluginManifestSerializer,
    ExecutionProfileSerializer,
    ExecutionQualityGateEvaluationSerializer,
    ExecutionRetryPolicySerializer,
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
from .adapters import AdapterConflict, get_executor_adapter
from .result_events import ResultEventError, ResultEventIngestor
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


class ExecutionPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def get_accessible_projects(user):
    return Project.objects.filter(
        Q(owner=user) | Q(members=user)
    ).distinct()

class ResultImportView(APIView):
    """Import JUnit or Allure results into one idempotent CI execution job."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        project_id = request.data.get('project_id')
        result_format = request.data.get('format')
        external_run_id = request.data.get('external_run_id')
        build_reference = request.data.get('build_reference', '')
        uploaded = request.FILES.get('file')
        if not project_id or not result_format or not external_run_id or uploaded is None:
            return Response(
                {'detail': 'project_id、format、external_run_id 和 file 均为必填项'},
                status=status.HTTP_400_BAD_REQUEST,
            )
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

    def get(self, request):
        days = max(1, min(int(request.query_params.get('days', 30)), 365))
        since = timezone.now() - timedelta(days=days)
        projects = accessible_execution_projects(request.user)
        project_id = request.query_params.get('project')
        if project_id:
            projects = projects.filter(pk=project_id)
        jobs = list(ExecutionJob.objects.filter(project__in=projects, created_at__gte=since))
        ci_jobs = [job for job in jobs if job.source_type == 'ci']
        summaries = [job.result or {} for job in ci_jobs if isinstance(job.result, dict)]
        evaluations = list(ExecutionQualityGateEvaluation.objects.filter(job__in=jobs, stage='release'))
        from collections import Counter
        failure_reviews = ExecutionFailureReview.objects.filter(job__in=jobs)
        try:
            from apps.quality_intelligence.models import QualityTestProfile
            flaky_count = QualityTestProfile.objects.filter(
                project_id__in=[str(project.pk) for project in projects],
                reliability_status='FLAKY',
            ).count()
        except Exception:
            flaky_count = 0
        return Response({
            'window_days': days,
            'total_jobs': len(jobs),
            'ci_jobs': len(ci_jobs),
            'ci_association_rate': round(sum(1 for item in summaries if not int(item.get('unmatched', 0) or 0)) / len(summaries) * 100, 2) if summaries else 0,
            'unmatched_results': sum(int(item.get('unmatched', 0) or 0) for item in summaries),
            'gate_evaluations': len(evaluations),
            'gate_pass_rate': round(sum(1 for item in evaluations if item.passed) / len(evaluations) * 100, 2) if evaluations else 0,
            'failure_categories': dict(Counter(failure_reviews.values_list('category', flat=True))),
            'flaky_profiles': flaky_count,
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
        # 在更新TestPlan时，处理版本信息
        version_id = self.request.data.get('version')
        version = None
        if version_id:
            from apps.versions.models import Version
            try:
                version = Version.objects.get(id=version_id)
            except Version.DoesNotExist:
                pass
        
        project_ids = self.request.data.get('projects', [])
        testcase_ids = self.request.data.get('testcases', [])
        self._validate_project_ids(project_ids)

        with transaction.atomic():
            test_plan = serializer.save(version=version)
            self._sync_test_runs(test_plan, project_ids, testcase_ids)

            assignee_ids = self.request.data.get('assignees', [])
            if assignee_ids:
                test_plan.assignees.set(assignee_ids)
            else:
                test_plan.assignees.clear()


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
        return self.queryset.filter(
            project__in=accessible_execution_projects(self.request.user)
        ).distinct()

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
        adapter = get_executor_adapter(job.source_type)
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
        adapter = get_executor_adapter(job.source_type)
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
        return Response(analyze_job_failures(self.get_object()))

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
        baseline_id = request.data.get('baseline_job_id')
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
        baseline_id = request.data.get('baseline_job_id')
        baseline = None
        if baseline_id:
            baseline = self.get_queryset().filter(pk=baseline_id).first()
            if baseline is None:
                return Response({'detail': 'baseline_job_id 不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
        try:
            max_failed = int(request.data.get('max_failed', 0))
            max_unmatched = int(request.data.get('max_unmatched', 0))
            result = evaluate_release_gate(
                job,
                baseline=baseline,
                max_failed=max_failed,
                max_unmatched=max_unmatched,
                evaluated_by=request.user,
            )
        except (TypeError, ValueError) as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
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
        project_id = request.query_params.get('project')
        if not project_id:
            raise ValidationError({'project': 'project 参数不能为空'})
        if not accessible_execution_projects(request.user).filter(pk=project_id).exists():
            raise PermissionDenied('没有权限访问该项目')
        requirements = BusinessRequirement.objects.filter(
            analysis__document__project_id=project_id,
        )
        linked = requirements.filter(manual_testcase_links__isnull=False).distinct()
        return Response({
            'project': int(project_id),
            'total_requirements': requirements.count(),
            'covered_requirements': linked.count(),
            'coverage_rate': round(linked.count() / requirements.count() * 100, 1) if requirements.exists() else 0,
            'uncovered_requirement_ids': list(
                requirements.filter(manual_testcase_links__isnull=True).values_list('id', flat=True)
            ),
        })


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
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['project', 'enabled']
    search_fields = ['name', 'description']

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


class ExecutionPluginManifestViewSet(viewsets.ModelViewSet):
    queryset = ExecutionPluginManifest.objects.all()
    serializer_class = ExecutionPluginManifestSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ExecutionPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['project', 'kind', 'enabled']
    search_fields = ['name', 'version']

    def get_queryset(self):
        return self.queryset.filter(
            Q(project__isnull=True) | Q(project__in=accessible_execution_projects(self.request.user))
        ).distinct()

    def perform_create(self, serializer):
        project = serializer.validated_data.get('project')
        if project is not None and not accessible_execution_projects(self.request.user).filter(pk=project.pk).exists():
            raise PermissionDenied('没有权限创建该项目插件声明')
        serializer.save()
