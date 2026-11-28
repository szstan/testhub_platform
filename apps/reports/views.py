from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from django.db.models import Count, Sum, F, Avg, Q
from django.db.models.functions import TruncDate, Length
from django.utils import timezone
from datetime import timedelta, datetime
from .models import TestReport, ReportTemplate
from .serializers import TestReportSerializer, accessible_report_projects
from apps.executions.models import TestPlan, TestRun, TestRunCase
from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.requirement_analysis.models import RequirementAnalysis, GeneratedTestCase, BusinessRequirement


def get_accessible_projects(user):
    return accessible_report_projects(user)

@extend_schema_view(
    list=extend_schema(operation_id='test_reports_list', responses=OpenApiTypes.OBJECT),
    retrieve=extend_schema(operation_id='test_reports_retrieve', responses=OpenApiTypes.OBJECT),
    create=extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT),
    update=extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT),
    partial_update=extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT),
    destroy=extend_schema(responses=OpenApiTypes.OBJECT),
)
class TestReportViewSet(viewsets.ModelViewSet):
    """测试报告视图集"""
    queryset = TestReport.objects.all()
    serializer_class = TestReportSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        serializer.save(generated_by=self.request.user)

    def get_queryset(self):
        return self.queryset.filter(
            project__in=get_accessible_projects(self.request.user)
        ).distinct()
    
    @action(detail=False, methods=['get'])
    def dashboard(self, request):
        """获取概览数据"""
        project_id = request.query_params.get('project')
        
        # 基础查询集
        plans_qs = TestPlan.objects.filter(
            is_active=True,
            projects__in=get_accessible_projects(request.user),
        ).distinct()
        cases_qs = TestCase.objects.filter(
            project__in=get_accessible_projects(request.user),
        )
        
        if project_id:
            plans_qs = plans_qs.filter(projects__id=project_id)
            cases_qs = cases_qs.filter(project_id=project_id)
            
        # 统计数据
        total_plans = plans_qs.count()
        total_cases = cases_qs.count()
        
        # One grouped query supplies progress for every visible run, rather
        # than five or six count queries for each run in every plan.
        runs_qs = TestRun.objects.filter(
            test_plan__in=plans_qs,
            project__in=get_accessible_projects(request.user),
        )
        if project_id:
            runs_qs = runs_qs.filter(project_id=project_id)
        annotated_runs = runs_qs.annotate(
            case_total=Count('run_cases'),
            case_tested=Count('run_cases', filter=Q(run_cases__status__in=['passed', 'failed', 'blocked', 'retest'])),
            case_passed=Count('run_cases', filter=Q(run_cases__status='passed')),
        )
        plan_progresses = {}
        for run in annotated_runs.values('test_plan_id', 'case_total', 'case_tested').iterator():
            progress = round(100 * run['case_tested'] / run['case_total'], 1) if run['case_total'] else 0
            aggregate = plan_progresses.setdefault(run['test_plan_id'], [0, 0])
            aggregate[0] += progress
            aggregate[1] += 1
        avg_plan_progress = round(
            sum(total / count for total, count in plan_progresses.values()) / len(plan_progresses), 1,
        ) if plan_progresses else 0

        recent_runs = annotated_runs.order_by('-created_at', '-pk').values('case_tested', 'case_passed')[:10]
        total_executed = total_passed = 0
        for run in recent_runs:
            total_executed += run['case_tested']
            total_passed += run['case_passed']
        pass_rate = round(100 * total_passed / total_executed, 1) if total_executed else 0

        defects_count = sum(
            len(defects) for defects in TestRunCase.objects.filter(test_run__in=runs_qs)
            .exclude(defects=[]).values_list('defects', flat=True).iterator()
            if isinstance(defects, list)
        )
        
        return Response({
            'active_plans': total_plans,
            'plan_progress': avg_plan_progress,
            'total_cases': total_cases,
            'total_defects': defects_count,
            'pass_rate': pass_rate
        })

    @action(detail=False, methods=['get'])
    def status_distribution(self, request):
        """获取执行状态分布"""
        project_id = request.query_params.get('project')
        version_id = request.query_params.get('version')
        
        runs_qs = TestRun.objects.filter(
            project__in=get_accessible_projects(request.user),
        ).distinct()
        if project_id:
            runs_qs = runs_qs.filter(project_id=project_id)
        if version_id:
            runs_qs = runs_qs.filter(version_id=version_id)
            
        distribution = TestRunCase.objects.filter(test_run__in=runs_qs).values('status').annotate(
            count=Count('id')
        )
        
        result = {item['status']: item['count'] for item in distribution}
        for status, _ in TestRunCase.STATUS_CHOICES:
            if status not in result:
                result[status] = 0
                
        return Response(result)

    @action(detail=False, methods=['get'])
    def defect_distribution(self, request):
        """获取缺陷分布 (按优先级)"""
        project_id = request.query_params.get('project')
        qs = TestRunCase.objects.filter(
            status='failed',
            test_run__project__in=get_accessible_projects(request.user),
        )

        
        if project_id:
            qs = qs.filter(test_run__project_id=project_id)
            
        distribution = qs.values('priority').annotate(count=Count('id'))
        
        # 映射优先级显示
        priority_map = dict(TestRunCase.PRIORITY_CHOICES)
        result = []
        for item in distribution:
            result.append({
                'name': priority_map.get(item['priority'], item['priority']),
                'value': item['count']
            })
            
        return Response(result)

    @action(detail=False, methods=['get'])
    def failed_cases_top(self, request):
        """获取失败用例TOP榜"""
        project_id = request.query_params.get('project')
        
        qs = TestRunCase.objects.filter(
            status='failed',
            test_run__project__in=get_accessible_projects(request.user),
        )

        if project_id:
            qs = qs.filter(test_run__project_id=project_id)
            
        # 按 testcase 分组统计失败次数
        top_failed = qs.values(
            'testcase__id', 'testcase__title'
        ).annotate(
            fail_count=Count('id')
        ).order_by('-fail_count')[:10]
        
        return Response(top_failed)

    @action(detail=False, methods=['get'])
    def execution_trend(self, request):
        """获取每日执行趋势"""
        project_id = request.query_params.get('project')
        try:
            days = int(request.query_params.get('days', 7))
        except (TypeError, ValueError, OverflowError):
            return Response({'days': '必须是 1 到 366 之间的整数'}, status=400)
        if not 1 <= days <= 366:
            return Response({'days': '必须是 1 到 366 之间的整数'}, status=400)
        
        # 获取当前时区的今天开始时间
        current_tz = timezone.get_current_timezone()
        local_now = timezone.localtime(timezone.now())
        today = local_now.date()
        
        # 计算起始日期
        start_date = today - timedelta(days=days - 1)
        
        # 构造起始时间的 datetime 对象 (00:00:00)
        start_datetime = datetime.combine(start_date, datetime.min.time())
        start_datetime = timezone.make_aware(start_datetime, current_tz)
        
        qs = TestRunCase.objects.filter(
            test_run__project__in=get_accessible_projects(request.user),
            executed_at__gte=start_datetime,
            status__in=['passed', 'failed', 'blocked', 'retest']
        )

        
        if project_id:
            qs = qs.filter(test_run__project_id=project_id)
            
        end_datetime = timezone.make_aware(
            datetime.combine(today + timedelta(days=1), datetime.min.time()), current_tz,
        )
        qs = qs.filter(executed_at__lt=end_datetime)
        daily_counts = list(qs.order_by().annotate(
            day=TruncDate('executed_at', tzinfo=current_tz),
        ).values('day').annotate(count=Count('pk')))
        date_map = {row['day'].isoformat(): row['count'] for row in daily_counts if row['day'] is not None}
        if any(row['day'] is None for row in daily_counts):
            # MySQL installations without timezone tables cannot use CONVERT_TZ.
            # Count bounded UTC windows in one SQL aggregate instead of loading
            # every timestamp into Python or issuing a query per day.
            windows = {}
            for offset in range(days):
                day = start_date + timedelta(days=offset)
                begin = timezone.make_aware(datetime.combine(day, datetime.min.time()), current_tz)
                end = timezone.make_aware(datetime.combine(day + timedelta(days=1), datetime.min.time()), current_tz)
                windows[f'd{offset}'] = Count('pk', filter=Q(executed_at__gte=begin, executed_at__lt=end))
            counts = qs.aggregate(**windows)
            date_map = {
                (start_date + timedelta(days=offset)).isoformat(): counts[f'd{offset}']
                for offset in range(days)
            }
        
        # 补全日期
        result = []
        for i in range(days):
            date = start_date + timedelta(days=i)
            date_str = date.strftime('%Y-%m-%d')
            result.append({
                'date': date_str,
                'count': date_map.get(date_str, 0)
            })
            
        return Response(result)

    @action(detail=False, methods=['get'])
    def ai_efficiency(self, request):
        """获取AI效能分析"""
        project_id = request.query_params.get('project')
        
        cases_qs = TestCase.objects.filter(
            project__in=get_accessible_projects(request.user),
        )
        generated_qs = GeneratedTestCase.objects.filter(
            requirement__analysis__document__project__in=get_accessible_projects(request.user),
        )
        requirements_qs = BusinessRequirement.objects.filter(
            analysis__document__project__in=get_accessible_projects(request.user),
        )
        
        if project_id:
            cases_qs = cases_qs.filter(project_id=project_id)
            generated_qs = generated_qs.filter(requirement__analysis__document__project_id=project_id)
            requirements_qs = requirements_qs.filter(analysis__document__project_id=project_id)
            
        # 1. AI生成 vs 人工创建
        ai_count = generated_qs.count()
        adopted_ai_count = generated_qs.filter(status='adopted').count()
        total_cases = cases_qs.count()
        manual_count = max(0, total_cases - adopted_ai_count)
        
        # 2. 生成采纳率
        adoption_rate = round((adopted_ai_count / ai_count * 100), 1) if ai_count > 0 else 0
        
        # 3. 需求覆盖率
        total_reqs = requirements_qs.count()
        covered_reqs = generated_qs.filter(status='adopted').values('requirement').distinct().count()
        coverage_rate = round((covered_reqs / total_reqs * 100), 1) if total_reqs > 0 else 0
        
        # 4. 节省时间估算
        saved_hours = round(ai_count * 15 / 60, 1)
        
        return Response({
            'ai_vs_manual': {
                'ai': ai_count,
                'manual': manual_count
            },
            'adoption_rate': adoption_rate,
            'requirement_coverage': coverage_rate,
            'saved_hours': saved_hours
        })

    @action(detail=False, methods=['get'])
    def team_workload(self, request):
        """获取团队工作量"""
        project_id = request.query_params.get('project')
        
        qs = TestRunCase.objects.filter(
            test_run__project__in=get_accessible_projects(request.user),
            status__in=['passed', 'failed', 'blocked', 'retest'],
            executed_by__isnull=False
        )

        
        if project_id:
            qs = qs.filter(test_run__project_id=project_id)
            
        # 统计执行数量
        execution_stats = qs.values(
            'executed_by__username'
        ).annotate(
            count=Count('id')
        ).order_by('-count')[:10]
        
        # 统计发现缺陷数量
        defect_stats = {}
        defect_qs = qs.filter(status__in=['failed', 'blocked'])
        defect_data = defect_qs.values('executed_by__username').annotate(count=Count('id'))
        for item in defect_data:
            defect_stats[item['executed_by__username']] = item['count']
            
        result = []
        for item in execution_stats:
            username = item['executed_by__username']
            result.append({
                'username': username,
                'execution_count': item['count'],
                'defect_count': defect_stats.get(username, 0)
            })
            
        return Response(result)