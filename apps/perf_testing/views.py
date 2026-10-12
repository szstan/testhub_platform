"""性能测试模块视图。

对齐 apps/api_testing/views.py 的既有范式：
DefaultRouter + ModelViewSet + FlexiblePageNumberPagination + IsAuthenticated。

这里所有"会真的打流量"的入口（execute/debug/run-now）都必须先过 preflight，
它是平台唯一的护栏：并发上限、时长上限、禁压主机、并发执行数都在那里卡。
"""
import csv
import io
import json
import logging
import os
import shutil
from datetime import timedelta

from django.conf import settings
from django.db import models as db_models
from django.db import transaction
from django.http import FileResponse, HttpResponse
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from .auth import HasPerfShareTokenOrAuthenticated, ShareTokenAuthentication

from .engines import engine_status
from .models import (PerfBaseline, PerfComparisonReport, PerfDataFile, PerfExecution,
                     PerfMetricSample, PerfProject, PerfRequestStat, PerfScenario,
                     PerfScenarioStep, PerfScheduledTask)
from .operation_logger import log_operation
from .serializers import (PerfBaselineSerializer, PerfDataFileSerializer,
                          PerfExecutionDetailSerializer, PerfExecutionListSerializer,
                          PerfMetricSampleSerializer, PerfProjectSerializer,
                          PerfRequestStatSerializer, PerfScenarioListSerializer,
                          PerfScenarioSerializer, PerfScenarioStepSerializer,
                          PerfScheduledTaskSerializer)
from .services import cleanup as cleanup_service
from .services import executor, reporter
from .services.url_rewrite import (collapse_redundant_slashes, normalize_base_url_token,
                                   rewrite_base_url_token)

logger = logging.getLogger(__name__)


def resolve_script_ref(scenario, raw):
    """把前端传来的 script_ref 收敛成服务端可信的引用。

    返回 (script_ref, error)。error 非空时调用方必须拒绝执行。

    安全约束（重要）：
    前端只允许提交 {'mode': 'script', 'data_file_id': N}，jmx_path 一律由服务端
    从 PerfDataFile 反查。直接信任前端传来的绝对路径等于开放任意文件读取——
    JMeter 会把该路径当测试计划加载，配合 JSR223/BeanShell 元件甚至可升级为
    任意命令执行。    解析后还会二次校验路径落在 MEDIA_ROOT 内，防止软链绕过。

    raw 为空时回落到场景自身持久化的配置 scenario.runtime_config['script_ref']，
    这样定时压测、复跑等不经过编辑器的触发路径也能复用同一份脚本选择。
    """
    if not raw:
        raw = (getattr(scenario, 'runtime_config', None) or {}).get('script_ref') or {}
    if not raw:
        return {}, None
    if not isinstance(raw, dict):
        return None, 'script_ref 必须是对象'

    mode = str(raw.get('mode') or '').strip().lower()
    if mode in ('', 'scenario'):
        return {}, None
    if mode != 'script':
        return None, f'不支持的执行模式：{mode}'
    if scenario.engine != 'JMETER':
        return None, '仅 JMeter 引擎支持上传脚本模式，请先把场景引擎切换为 JMeter'

    data_file_id = raw.get('data_file_id')
    if not data_file_id:
        return None, '脚本模式必须选择一个已上传的 .jmx 文件'
    try:
        data_file = PerfDataFile.objects.get(pk=data_file_id)
    except (PerfDataFile.DoesNotExist, ValueError, TypeError):
        return None, f'脚本文件不存在（id={data_file_id}）'
    if data_file.project_id != scenario.project_id:
        return None, '脚本文件不属于当前场景所在项目'
    if data_file.file_type != 'JMX':
        return None, f'文件「{data_file.name}」不是 JMeter 脚本'

    try:
        jmx_path = os.path.abspath(data_file.file.path)
        media_root = os.path.abspath(settings.MEDIA_ROOT)
        if os.path.commonpath([media_root, jmx_path]) != media_root:
            return None, '脚本文件路径非法'
    except (ValueError, NotImplementedError, AttributeError) as exc:
        return None, f'无法定位脚本文件：{exc}'
    if not os.path.exists(jmx_path):
        return None, '脚本文件已丢失，请重新上传'

    return {
        'mode': 'script',
        'data_file_id': data_file.id,
        'data_file_name': data_file.name,
        'jmx_path': jmx_path,
    }, None


class FlexiblePageNumberPagination(PageNumberPagination):
    """与 api_testing 保持一致：page_size=0 表示不分页返回全部。"""

    page_size_query_param = 'page_size'
    max_page_size = 10000

    def paginate_queryset(self, queryset, request, view=None):
        page_size = request.query_params.get(self.page_size_query_param)
        if page_size is not None:
            try:
                if int(page_size) == 0:
                    return None
            except (ValueError, TypeError):
                pass
        return super().paginate_queryset(queryset, request, view)


# ====================================================================== #
# 项目
# ====================================================================== #
class PerfProjectViewSet(viewsets.ModelViewSet):
    """压测项目"""

    queryset = PerfProject.objects.all().select_related('owner').prefetch_related('members')
    serializer_class = PerfProjectSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = FlexiblePageNumberPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'owner']
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'updated_at', 'name']

    def get_queryset(self):
        # 注解场景/执行计数，避免序列化器逐项目 count() 的 N+1。
        # 两个 Count 必须 distinct，否则 JOIN 会产生笛卡尔积放大。
        from django.db.models import Count
        return super().get_queryset().annotate(
            scenario_count_anno=Count('scenarios', distinct=True),
            execution_count_anno=Count('executions', distinct=True),
        )

    def perform_create(self, serializer):
        project = serializer.save()
        log_operation('CREATE', 'PROJECT', project.id, project.name, self.request.user)

    def perform_update(self, serializer):
        project = serializer.save()
        log_operation('UPDATE', 'PROJECT', project.id, project.name, self.request.user)

    def perform_destroy(self, instance):
        # 有正在跑的压测时不允许删项目，否则子进程会写一张已经不存在的表记录
        running = PerfExecution.objects.filter(
            project=instance, status__in=PerfExecution.ACTIVE_STATUSES).count()
        if running:
            raise ValidationError(f'项目下还有 {running} 个压测正在执行，请先停止后再删除')
        log_operation('DELETE', 'PROJECT', instance.id, instance.name, self.request.user)
        instance.delete()

    @action(detail=True, methods=['get'])
    def statistics(self, request, pk=None):
        """项目概览：场景数、执行数、最近成功率、平均 TPS。"""
        project = self.get_object()
        executions = PerfExecution.objects.filter(project=project)
        recent = executions.order_by('-created_at')[:20]
        finished = [e for e in recent if e.status in PerfExecution.FINAL_STATUSES]
        sla_passed = len([e for e in finished if e.sla_result == 'PASSED'])
        tps_values = [(e.summary or {}).get('tps') or 0 for e in finished]

        return Response({
            'scenario_count': project.scenarios.count(),
            'execution_count': executions.count(),
            'running_count': executions.filter(
                status__in=PerfExecution.ACTIVE_STATUSES).count(),
            'sla_pass_rate': round(sla_passed / len(finished) * 100, 1) if finished else 0,
            'avg_tps': round(sum(tps_values) / len(tps_values), 2) if tps_values else 0,
            'data_file_count': project.data_files.count(),
        })


# ====================================================================== #
# 场景
# ====================================================================== #
class PerfScenarioViewSet(viewsets.ModelViewSet):
    """压测场景"""

    queryset = PerfScenario.objects.all().select_related('project', 'created_by')
    permission_classes = [IsAuthenticated]
    pagination_class = FlexiblePageNumberPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['project', 'engine', 'enabled']
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'updated_at', 'name']

    def get_serializer_class(self):
        if self.action == 'list':
            return PerfScenarioListSerializer
        return PerfScenarioSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == 'list':
            # 列表需要"最近一次执行"摘要。用关联子查询注解而非逐场景查询，
            # 把 PerfScenarioListSerializer.get_last_execution 的 N+1 收敛为单条 SQL。
            from django.db.models import OuterRef, Subquery
            latest = PerfExecution.objects.filter(
                scenario=OuterRef('pk')).order_by('-created_at')
            return qs.prefetch_related('steps').annotate(
                _latest_exec_id=Subquery(latest.values('id')[:1]),
                _latest_exec_no=Subquery(latest.values('execution_no')[:1]),
                _latest_exec_status=Subquery(latest.values('status')[:1]),
                _latest_exec_sla=Subquery(latest.values('sla_result')[:1]),
                _latest_exec_created=Subquery(latest.values('created_at')[:1]),
                _latest_summary=Subquery(latest.values('summary')[:1]),
            )
        return qs.prefetch_related('steps')

    def perform_create(self, serializer):
        scenario = serializer.save()
        log_operation('CREATE', 'SCENARIO', scenario.id, scenario.name, self.request.user)

    def perform_update(self, serializer):
        scenario = serializer.save()
        log_operation('UPDATE', 'SCENARIO', scenario.id, scenario.name, self.request.user)

    def perform_destroy(self, instance):
        if instance.has_active_execution():
            raise ValidationError('场景有正在执行的压测，请先停止后再删除')
        log_operation('DELETE', 'SCENARIO', instance.id, instance.name, self.request.user)
        instance.delete()

    # ------------------------------------------------------------------ #
    # 步骤批量保存：前端是一个可拖拽列表，整体提交比逐条 CRUD 简单可靠
    # ------------------------------------------------------------------ #
    @action(detail=True, methods=['post'], url_path='save-steps')
    def save_steps(self, request, pk=None):
        scenario = self.get_object()
        steps = request.data.get('steps')
        if not isinstance(steps, list):
            return Response({'error': 'steps 必须是数组'}, status=status.HTTP_400_BAD_REQUEST)

        cleaned = []
        referenced_file_ids = set()
        for idx, raw in enumerate(steps):
            payload = dict(raw or {})
            payload.pop('id', None)
            # 步骤必须归属到当前场景：无论前端是否携带 scenario 字段都强制注入，
            # 避免“该字段是必填项”类 400（历史上曾因服务端热重载滞后而短暂出现）。
            payload['scenario'] = scenario.id
            payload['order'] = payload.get('order', idx)
            # 防御性归一化：前端偶发把可选 JSON 字段传成 null/字符串，
            # 这里兜底成模型默认值，避免整批保存直接 400。
            if not isinstance(payload.get('extractors'), list):
                payload['extractors'] = []
            if not isinstance(payload.get('assertions'), list):
                payload['assertions'] = []
            if not isinstance(payload.get('headers'), dict):
                payload['headers'] = {}
            if not isinstance(payload.get('params'), dict):
                payload['params'] = {}
            if not isinstance(payload.get('think_time'), dict):
                payload['think_time'] = {}
            if not isinstance(payload.get('files'), list):
                payload['files'] = []
            for item in payload['files']:
                if isinstance(item, dict) and item.get('file_id'):
                    try:
                        referenced_file_ids.add(int(item['file_id']))
                    except (TypeError, ValueError):
                        pass
            serializer = PerfScenarioStepSerializer(data=payload)
            if not serializer.is_valid():
                return Response(
                    {'error': f'第 {idx + 1} 个步骤校验失败', 'detail': serializer.errors},
                    status=status.HTTP_400_BAD_REQUEST)
            cleaned.append(serializer.validated_data)

        # 文件字段归属校验：只允许引用本项目内 file_type=UPLOAD 的文件资产，
        # 防止跨项目引用或伪造 file_id（与 resolve_script_ref 同一安全思路）。
        if referenced_file_ids:
            valid_ids = set(PerfDataFile.objects.filter(
                id__in=referenced_file_ids,
                project=scenario.project_id,
                file_type='UPLOAD').values_list('id', flat=True))
            invalid = sorted(referenced_file_ids - valid_ids)
            if invalid:
                return Response(
                    {'error': f'步骤引用的上传文件不存在或不属于当前项目：{invalid}'},
                    status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            scenario.steps.all().delete()
            PerfScenarioStep.objects.bulk_create(
                [PerfScenarioStep(**item) for item in cleaned])

        log_operation('UPDATE', 'SCENARIO', scenario.id, scenario.name, request.user,
                      description=f'更新步骤（共 {len(cleaned)} 个）')
        return Response(PerfScenarioSerializer(
            scenario, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def duplicate(self, request, pk=None):
        """复制场景（含步骤）。压测场景配置很重，复制比重建实用得多。"""
        source = self.get_object()
        source_steps = list(source.steps.all())
        new_name = (request.data.get('name') or f'{source.name} - 副本')[:200]

        with transaction.atomic():
            clone = PerfScenario.objects.create(
                project=source.project,
                name=new_name,
                description=source.description,
                engine=source.engine,
                load_config=source.load_config,
                sla_config=source.sla_config,
                variables=source.variables,
                env_config=source.env_config,
                runtime_config=source.runtime_config,
                enabled=source.enabled,
                created_by=request.user,
            )
            PerfScenarioStep.objects.bulk_create([
                PerfScenarioStep(
                    scenario=clone, order=s.order, name=s.name, enabled=s.enabled,
                    source_request=s.source_request, method=s.method, url=s.url,
                    headers=s.headers, params=s.params, body_type=s.body_type,
                    body=s.body, files=s.files, extractors=s.extractors,
                    assertions=s.assertions,
                    think_time=s.think_time, weight=s.weight, is_setup=s.is_setup)
                for s in source_steps
            ])

        log_operation('CREATE', 'SCENARIO', clone.id, clone.name, request.user,
                      description=f'复制自场景「{source.name}」')
        return Response(PerfScenarioSerializer(clone, context={'request': request}).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='import-from-api')
    def import_from_api(self, request, pk=None):
        """从接口测试模块导入用例为压测步骤。

        压测场景 90% 的来源就是"已经调通的接口用例"，让用户重录一遍毫无意义。
        """
        scenario = self.get_object()
        request_ids = request.data.get('request_ids') or []
        if not request_ids:
            return Response({'error': '请选择要导入的接口用例'},
                            status=status.HTTP_400_BAD_REQUEST)

        # 基础地址（baseUrl）处理方式：
        # - 'keep'（默认）：URL 维持 {{baseUrl}}/path 变量引用，base_url 落到场景 env_config.base_url，
        #   运行时由引擎解析（Postman/Insomnia 的环境变量风格）。
        # - 'replace'：把每条 URL 开头的 {{baseUrl}} 变量直接替换为具体地址（GitHub 式“钉死基址”）。
        # 两者在参数缺失时均向后兼容旧行为（URL 原样、不改 env_config）。
        base_url_mode = (request.data.get('base_url_mode') or 'keep').strip().lower()
        if base_url_mode not in ('keep', 'replace'):
            base_url_mode = 'keep'
        base_url = (request.data.get('base_url') or '').strip()

        from apps.api_testing.models import ApiRequest

        api_requests = list(ApiRequest.objects.filter(
            id__in=request_ids, request_type='HTTP'))
        if not api_requests:
            return Response({'error': '未找到可导入的 HTTP 接口用例（WebSocket 用例不支持压测）'},
                            status=status.HTTP_400_BAD_REQUEST)

        order_base = (scenario.steps.aggregate(m=db_models.Max('order'))['m'] or -1) + 1
        body_type_map = {
            'json': 'JSON', 'raw': 'RAW', 'xml': 'XML',
            'form-data': 'FORM', 'x-www-form-urlencoded': 'FORM', 'none': 'NONE',
        }
        as_setup = bool(request.data.get('as_setup'))

        created = []
        warnings = []
        for offset, api in enumerate(api_requests):
            raw_body = api.body if isinstance(api.body, dict) else {}
            body_kind = (raw_body.get('type') or 'none').lower()
            # api_testing 的 body 实际结构是 {'type': ..., 'data': ...}（历史缺陷：
            # 此处曾读 'content' 导致 POST/PUT 的 JSON 请求体全部丢失）。
            content = raw_body.get('data')
            if content is None:
                content = raw_body.get('content')
            step_files = []
            if isinstance(content, (dict, list)):
                if body_kind in ('form-data', 'x-www-form-urlencoded') and isinstance(content, list):
                    # form-data 的 data 是 [{key, value, type, ...}] 列表：
                    # 文本字段转成 {key: value} 文本（引擎 FORM 分支按 json.loads 解成 dict），
                    # type=file 的文件字段落到 step.files 占位（接口调试的文件不持久化，
                    # 无法随导入携带，需用户在压测步骤里补传）。
                    form_pairs = {}
                    for item in content:
                        if not isinstance(item, dict) or not item.get('key'):
                            continue
                        if item.get('type') == 'file':
                            step_files.append({
                                'field': str(item.get('key')), 'file_id': None,
                                'filename': str(item.get('value') or '')[:200],
                                'content_type': '',
                            })
                        else:
                            form_pairs[str(item.get('key'))] = item.get('value', '')
                    body_text = json.dumps(form_pairs, ensure_ascii=False, indent=2)
                else:
                    body_text = json.dumps(content, ensure_ascii=False, indent=2)
            else:
                body_text = str(content or '')

            # keep 模式：把字面 base_url/ 前缀归一化为 {{base_url}}/，
            # 运行时由 VariableContext 解析场景 env_config.base_url。
            step_url = api.url
            if base_url_mode == 'replace':
                step_url = rewrite_base_url_token(step_url, base_url)
            else:
                step_url = normalize_base_url_token(step_url)

            created.append(PerfScenarioStep(
                scenario=scenario,
                order=order_base + offset,
                name=api.name[:200],
                source_request=api,
                method=api.method,
                # 落库前折叠冗余双斜杠（{{base_url}}/jar//login -> {{base_url}}/jar/login），
                # 避免执行时 base_url+path 拼出错误地址；scheme 的 :// 保留。
                url=collapse_redundant_slashes(step_url),
                headers=api.headers if isinstance(api.headers, dict) else {},
                params=api.params if isinstance(api.params, dict) else {},
                body_type=body_type_map.get(body_kind, 'NONE'),
                body=body_text,
                files=step_files,
                is_setup=as_setup,
                # 断言不自动带过来：接口用例的断言常含业务强校验，
                # 高并发下逐条跑会让压力机自己成为瓶颈，交给用户显式选择。
                assertions=[],
                extractors=[],
            ))
            if step_files:
                field_names = '、'.join(f['field'] for f in step_files)
                warnings.append(
                    f'接口「{api.name}」含 {len(step_files)} 个文件字段（{field_names}），'
                    f'文件内容无法随导入携带，请在压测步骤中手动上传文件')

        PerfScenarioStep.objects.bulk_create(created)

        # keep 模式：把用户填的 base_url 落到场景 env_config.base_url，使 {{baseUrl}} 能解析。
        if base_url_mode == 'keep' and base_url:
            env = scenario.env_config or {}
            if not isinstance(env, dict):
                env = {}
            env['base_url'] = base_url
            scenario.env_config = env
            scenario.save(update_fields=['env_config'])

        log_operation('UPDATE', 'SCENARIO', scenario.id, scenario.name, request.user,
                      description=f'从接口用例导入 {len(created)} 个步骤')
        return Response({
            'imported': len(created),
            'skipped': len(request_ids) - len(created),
            'warnings': warnings,
            'scenario': PerfScenarioSerializer(scenario, context={'request': request}).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def preflight(self, request, pk=None):
        """执行前检查：不落任何数据，仅返回校验结论与容量预估。"""
        scenario = self.get_object()
        script_ref, err = resolve_script_ref(scenario, request.data.get('script_ref'))
        if err:
            # 脚本引用非法也是一种"检查不通过"，走同一个弹窗展示比抛 400 更连贯
            return Response({
                'passed': False, 'errors': [err], 'warnings': [],
                'estimated': {'peak_concurrency': 0, 'planned_duration': 0,
                              'estimated_requests': 0, 'target_hosts': [], 'step_count': 0},
            })
        result = executor.preflight(scenario, load_config=request.data.get('load_config'),
                                    script_ref=script_ref)
        return Response(result)

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        """正式压测：preflight → 建执行记录 → 拉起独立子进程。"""
        scenario = self.get_object()

        if scenario.has_active_execution():
            return Response({'error': '该场景已有正在执行的压测，请等待结束或先停止'},
                            status=status.HTTP_409_CONFLICT)

        script_ref, err = resolve_script_ref(scenario, request.data.get('script_ref'))
        if err:
            return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

        execution, check = executor.start_execution(
            scenario, user=request.user, trigger_type='MANUAL',
            script_ref=script_ref)
        if execution is None:
            return Response({'error': '执行前检查未通过', 'preflight': check},
                            status=status.HTTP_400_BAD_REQUEST)

        log_operation('EXECUTE', 'SCENARIO', scenario.id, scenario.name, request.user,
                      description=f'发起压测 {execution.execution_no}')
        return Response({
            'execution': PerfExecutionDetailSerializer(execution).data,
            'preflight': check,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def debug(self, request, pk=None):
        """调试模式：1 并发 1 轮，直接返回每步的请求/响应详情。

        压测最大的坑是"配错了但看不出来"，跑完才发现 100% 失败。
        调试模式让用户在正式加压前先确认脚本本身是通的。
        """
        scenario = self.get_object()
        steps = list(scenario.steps.filter(enabled=True).order_by('order'))
        if not steps:
            return Response({'error': '场景没有启用的步骤'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            result = executor.debug_run(scenario)
        except Exception as exc:  # noqa: BLE001 - 调试异常直接回给前端展示
            logger.warning('压测调试失败: %s', exc, exc_info=True)
            return Response({'error': f'调试执行失败：{exc}'},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=['get'], url_path='execution-history')
    def execution_history(self, request, pk=None):
        """场景的历史执行趋势（供趋势图使用）。"""
        scenario = self.get_object()
        limit = min(int(request.query_params.get('limit') or 20), 100)
        executions = PerfExecution.objects.filter(
            scenario=scenario, status='COMPLETED').order_by('-created_at')[:limit]
        points = [{
            'id': e.id,
            'execution_no': e.execution_no,
            'created_at': e.created_at,
            'tps': (e.summary or {}).get('tps') or 0,
            'avg_rt': (e.summary or {}).get('avg_rt') or 0,
            'p95_rt': (e.summary or {}).get('p95_rt') or 0,
            'error_rate': (e.summary or {}).get('error_rate') or 0,
            'sla_result': e.sla_result,
        } for e in executions]
        points.reverse()
        return Response(points)


# ====================================================================== #
# 步骤（单条 CRUD，配合前端行内编辑）
# ====================================================================== #
class PerfScenarioStepViewSet(viewsets.ModelViewSet):
    queryset = PerfScenarioStep.objects.all().select_related('scenario')
    serializer_class = PerfScenarioStepSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = FlexiblePageNumberPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['scenario', 'enabled', 'is_setup']
    ordering_fields = ['order', 'id']


# ====================================================================== #
# 执行
# ====================================================================== #
def _ensure_report_file(execution):
    """确保报告 HTML 存在，返回 (绝对路径, 错误描述)；失败时 abs_path 为 None。

    分享直链场景下报告可能因「执行收尾时生成失败」或「清理任务删除产物」
    而缺失；执行已终态时可基于库内采样/统计数据无损重建，
    避免分享链接打开即 404（历史执行记录也因此自愈可用）。
    """
    def _resolve():
        if not execution.report_url:
            return None
        path = os.path.join(settings.MEDIA_ROOT, execution.report_url)
        return path if os.path.isfile(path) else None

    abs_path = _resolve()
    if abs_path is None and not execution.is_active:
        try:
            execution.report_url = reporter.generate_report(execution)
            execution.save(update_fields=['report_url'])
            abs_path = _resolve()
        except Exception as exc:  # noqa: BLE001
            logger.warning('自愈重建压测报告失败 #%s: %s', execution.id, exc)
    if abs_path:
        return abs_path, ''
    if execution.is_active:
        return None, '压测尚未结束，报告未生成'
    if not execution.report_url:
        return None, '报告尚未生成，请先生成报告后再分享'
    return None, '报告文件已被清理且重建失败，请重新生成报告'


class PerfExecutionViewSet(mixins.DestroyModelMixin, viewsets.ReadOnlyModelViewSet):
    """压测执行：只读 + 删除。执行记录不允许改，它是审计凭据。"""

    queryset = PerfExecution.objects.all().select_related(
        'scenario', 'project', 'executed_by')
    permission_classes = [IsAuthenticated]
    pagination_class = FlexiblePageNumberPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['scenario', 'project', 'status', 'sla_result', 'trigger_type']
    search_fields = ['execution_no', 'scenario__name']
    ordering_fields = ['created_at', 'start_time', 'duration']

    def get_serializer_class(self):
        if self.action == 'list':
            return PerfExecutionListSerializer
        return PerfExecutionDetailSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == 'retrieve':
            return qs.prefetch_related('request_stats')
        return qs

    def perform_destroy(self, instance):
        if instance.is_active:
            raise ValidationError('执行进行中，请先停止后再删除')
        # 产物目录随记录一起删，避免 media 目录里堆孤儿文件
        artifact = executor.abs_artifact_dir(instance)
        log_operation('DELETE', 'EXECUTION', instance.id, instance.execution_no,
                      self.request.user)
        instance.delete()
        if artifact and os.path.isdir(artifact):
            shutil.rmtree(artifact, ignore_errors=True)

    @action(detail=True, methods=['post'])
    def stop(self, request, pk=None):
        execution = self.get_object()
        graceful = request.data.get('graceful', True)
        ok, message = executor.stop_execution(execution, graceful=bool(graceful))
        if ok:
            log_operation('EXECUTE', 'EXECUTION', execution.id, execution.execution_no,
                          request.user, description='手动停止压测')
        return Response({'success': ok, 'message': message},
                        status=status.HTTP_200_OK if ok else status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def realtime(self, request, pk=None):
        """WebSocket 不可用时的轮询降级接口。

        只回增量：前端传 since（上次拿到的最大 ts_offset），避免长压测每次拉全量。
        """
        execution = self.get_object()
        since = request.query_params.get('since')
        samples = PerfMetricSample.objects.filter(execution=execution)
        if since not in (None, ''):
            try:
                samples = samples.filter(ts_offset__gt=int(since))
            except (TypeError, ValueError):
                pass
        samples = samples.order_by('ts_offset')[:2000]

        return Response({
            'status': execution.status,
            'progress': execution.progress,
            'sla_result': execution.sla_result,
            'summary': execution.summary or {},
            'error_message': execution.error_message,
            'heartbeat_at': execution.heartbeat_at,
            'samples': PerfMetricSampleSerializer(samples, many=True).data,
        })

    @action(detail=True, methods=['get'])
    def samples(self, request, pk=None):
        """全量时序采样点（图表用，自动降采样避免前端卡死）。"""
        execution = self.get_object()
        max_points = min(int(request.query_params.get('max_points') or 1000), 5000)
        qs = PerfMetricSample.objects.filter(execution=execution).order_by('ts_offset')
        total = qs.count()
        if total <= max_points:
            data = PerfMetricSampleSerializer(qs, many=True).data
        else:
            from .services.metrics import downsample
            data = downsample(PerfMetricSampleSerializer(qs, many=True).data, max_points)
        return Response({'total': total, 'returned': len(data), 'samples': data})

    @action(detail=True, methods=['get'], url_path='request-stats')
    def request_stats(self, request, pk=None):
        execution = self.get_object()
        stats = PerfRequestStat.objects.filter(execution=execution)
        return Response(PerfRequestStatSerializer(stats, many=True).data)

    @action(detail=True, methods=['post'], url_path='generate-report')
    def generate_report(self, request, pk=None):
        execution = self.get_object()
        if execution.is_active:
            return Response({'error': '压测尚未结束，无法生成报告'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            rel_path = reporter.generate_report(execution)
        except Exception as exc:  # noqa: BLE001
            logger.error('生成压测报告失败: %s', exc, exc_info=True)
            return Response({'error': f'生成报告失败：{exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        execution.report_url = rel_path
        execution.save(update_fields=['report_url'])
        return Response({'report_url': rel_path})

    @action(detail=True, methods=['get'],
            authentication_classes=[ShareTokenAuthentication, *api_settings.DEFAULT_AUTHENTICATION_CLASSES],
            permission_classes=[HasPerfShareTokenOrAuthenticated])
    def report(self, request, pk=None):
        """直接返回 HTML 报告内容（前端 iframe / 新窗口打开）。

        支持 ?token= 分享直链；无 token 时仍需正常登录。
        报告文件缺失且执行已结束时先尝试自愈重建，失败时返回
        可读的 JSON 错误（Http404 会被 DRF 吞成笼统的 Not Found）。
        """
        execution = self.get_object()
        abs_path, error = _ensure_report_file(execution)
        if not abs_path:
            return Response({'error': error}, status=status.HTTP_404_NOT_FOUND)
        with open(abs_path, 'r', encoding='utf-8') as fh:
            return HttpResponse(fh.read(), content_type='text/html; charset=utf-8')

    @action(detail=True, methods=['get'], url_path='ai-analysis')
    def ai_analysis(self, request, pk=None):
        """压测 AI 失败分析（SSE 流式）。

        性能策略：
        1. 先查 Redis 缓存，命中则一次性返回（< 100ms）
        2. 未命中则 SSE 流式推送 LLM 输出（逐块 yield）
        3. 流式完成后写入缓存（TTL 30min）

        前端用 EventSource 消费，Tab 懒加载（点击才请求）。
        """
        import json
        from django.core.cache import cache
        from django.http import StreamingHttpResponse
        from apps.perf_testing.models import PerfRequestStat
        from apps.perf_testing.services.ai_analysis import analyze_stream

        execution = self.get_object()
        cache_key = f'perf:ai_analysis:{execution.id}'

        # 1. 缓存命中 → 即时返回
        cached = cache.get(cache_key)
        if cached:
            data = json.loads(cached)
            return Response(data)

        # 2. 未命中 → SSE 流式
        stats = list(PerfRequestStat.objects.filter(execution_id=execution.id).annotate(
            # 模型字段已重命名为 avg_rt/p95_rt，此处别名回旧键，保持下游消费键不变
            avg_response_time=db_models.F('avg_rt'), p95=db_models.F('p95_rt')
        ).values(
            'step_name', 'avg_response_time', 'p95', 'error_rate', 'failed', 'total'
        ))
        summary = execution.summary or {}
        verdict = execution.verdict or ''
        verdict_details = execution.verdict_details or []

        if not stats:
            return Response({'error': '无执行指标数据，请等待执行完成'}, status=status.HTTP_400_BAD_REQUEST)

        def sse_stream():
            full = []
            try:
                for chunk in analyze_stream(stats, summary, verdict, verdict_details):
                    full.append(chunk)
                    yield f'data: {json.dumps({"chunk": chunk}, ensure_ascii=False)}\n\n'
                # 写缓存
                result = json.dumps({'analysis': ''.join(full)}, ensure_ascii=False)
                cache.set(cache_key, result, timeout=1800)
                yield f'data: {json.dumps({"done": True}, ensure_ascii=False)}\n\n'
            except Exception as e:
                yield f'data: {json.dumps({"error": str(e)}, ensure_ascii=False)}\n\n'

        response = StreamingHttpResponse(sse_stream(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'  # Nginx 不缓冲
        return response

    @action(detail=True, methods=['get'], url_path='download-raw',
            authentication_classes=[ShareTokenAuthentication, *api_settings.DEFAULT_AUTHENTICATION_CLASSES],
            permission_classes=[HasPerfShareTokenOrAuthenticated])
    def download_raw(self, request, pk=None):
        """下载原始请求明细（gzip CSV）。

        支持 ?token= 分享直链；无 token 时仍需正常登录。
        """
        execution = self.get_object()
        abs_path = os.path.join(executor.abs_artifact_dir(execution), 'raw.csv.gz')
        if not os.path.isfile(abs_path):
            return Response({'error': '原始明细不存在或已被清理'},
                            status=status.HTTP_404_NOT_FOUND)
        response = FileResponse(open(abs_path, 'rb'), content_type='application/gzip')
        response['Content-Disposition'] = (
            f'attachment; filename="{execution.execution_no}_raw.csv.gz"')
        return response

    @action(detail=True, methods=['post'], url_path='share-link')
    def share_link(self, request, pk=None):
        """生成/重置报告分享直链。expires_in_days=None 表示永不过期。"""
        execution = self.get_object()
        if execution.is_active:
            return Response({'error': '压测尚未结束，请等待执行完成后再分享报告'},
                            status=status.HTTP_400_BAD_REQUEST)
        # 签发直链前先确保报告文件就绪，避免发出的链接打开即 404
        _, error = _ensure_report_file(execution)
        if error:
            return Response({'error': f'无法分享：{error}'},
                            status=status.HTTP_400_BAD_REQUEST)
        raw = request.data.get('expires_in_days', None)
        expires_in_days = None
        if raw not in (None, '', 'null'):
            try:
                expires_in_days = int(raw)
            except (TypeError, ValueError):
                return Response({'error': 'expires_in_days 必须是整数(天)'},
                                status=status.HTTP_400_BAD_REQUEST)
        token = execution.generate_share_token(expires_in_days)
        base = request.build_absolute_uri(
            f'/api/perf-testing/executions/{execution.id}/report/')
        raw_base = request.build_absolute_uri(
            f'/api/perf-testing/executions/{execution.id}/download-raw/')
        return Response({
            'token': token,
            'share_url': f'{base}?token={token}',
            'raw_url': f'{raw_base}?token={token}',
            'expires_at': execution.share_expires_at,
        })

    @action(detail=True, methods=['post'], url_path='revoke-share-link')
    def revoke_share_link(self, request, pk=None):
        """撤销报告分享直链。"""
        execution = self.get_object()
        execution.revoke_share_token()
        return Response({'success': True})

    @action(detail=True, methods=['get'], url_path='run-log')
    def run_log(self, request, pk=None):
        """子进程运行日志（排障用，尾部若干行）。"""
        execution = self.get_object()
        abs_path = os.path.join(executor.abs_artifact_dir(execution), 'run.log')
        if not os.path.isfile(abs_path):
            return Response({'content': '', 'exists': False})
        tail = min(int(request.query_params.get('lines') or 500), 5000)
        # 兼容历史日志编码：新子进程强制 UTF-8 写入，但旧日志可能是
        # Windows 下的 GBK/cp936，逐编码尝试避免乱码。
        raw = open(abs_path, 'rb').read()
        text = None
        for enc in ('utf-8-sig', 'utf-8', 'gb18030'):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if text is None:
            text = raw.decode('utf-8', errors='replace')
        lines = text.splitlines(keepends=True)[-tail:]
        return Response({'content': ''.join(lines), 'exists': True})

    @action(detail=False, methods=['get'])
    def compare(self, request):
        """多执行对比：?ids=1,2,3。返回汇总差异 + 各自时序曲线。"""
        raw_ids = (request.query_params.get('ids') or '').strip()
        ids = [int(i) for i in raw_ids.split(',') if i.strip().isdigit()]
        if len(ids) < 2:
            return Response({'error': '至少选择 2 次执行进行对比'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(ids) > 5:
            return Response({'error': '一次最多对比 5 次执行'},
                            status=status.HTTP_400_BAD_REQUEST)

        executions = list(PerfExecution.objects.filter(id__in=ids).select_related('scenario'))
        if len(executions) < 2:
            return Response({'error': '所选执行记录不存在'}, status=status.HTTP_404_NOT_FOUND)
        # 保持用户传入的顺序，第一条作为对比基准
        executions.sort(key=lambda e: ids.index(e.id))

        from .services.compare_report import build_snapshot
        return Response(build_snapshot(executions))

    @action(detail=False, methods=['get'])
    def dashboard(self, request):
        """压测总览：近 30 天概况 + 当前运行中列表。"""
        since = timezone.now() - timedelta(days=30)
        qs = PerfExecution.objects.filter(created_at__gte=since)
        project_id = request.query_params.get('project')
        if project_id:
            qs = qs.filter(project_id=project_id)

        finished = qs.filter(status__in=PerfExecution.FINAL_STATUSES)
        evaluated = finished.exclude(sla_result='NOT_EVALUATED')
        passed = evaluated.filter(sla_result='PASSED').count()

        running = PerfExecution.objects.filter(
            status__in=PerfExecution.ACTIVE_STATUSES).select_related('scenario')
        if project_id:
            running = running.filter(project_id=project_id)

        recent = qs.order_by('-created_at')[:10]
        return Response({
            'total_executions': qs.count(),
            'completed': finished.filter(status='COMPLETED').count(),
            'failed': finished.filter(status__in=['FAILED', 'TIMEOUT']).count(),
            'running_count': running.count(),
            'sla_pass_rate': round(passed / evaluated.count() * 100, 1)
            if evaluated.count() else 0,
            'running': PerfExecutionListSerializer(running, many=True).data,
            'recent': PerfExecutionListSerializer(recent, many=True).data,
        })

    @action(detail=False, methods=['post'], url_path='reap-stale')
    def reap_stale(self, request):
        """手动触发僵尸执行回收（运维入口）。"""
        count = cleanup_service.reap_stale_executions()
        return Response({'reaped': count})


# ====================================================================== #
# 基线
# ====================================================================== #
class PerfBaselineViewSet(viewsets.ModelViewSet):
    queryset = PerfBaseline.objects.all().select_related('scenario', 'execution', 'set_by')
    serializer_class = PerfBaselineSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = FlexiblePageNumberPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['scenario']
    ordering_fields = ['created_at', 'updated_at']

    def perform_create(self, serializer):
        serializer.save(set_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(set_by=self.request.user)

    @action(detail=False, methods=['post'], url_path='set-from-execution')
    def set_from_execution(self, request):
        """把某次执行的结果设为该场景的性能基线。"""
        execution_id = request.data.get('execution_id')
        if not execution_id:
            return Response({'error': '缺少 execution_id'},
                            status=status.HTTP_400_BAD_REQUEST)
        execution = PerfExecution.objects.filter(id=execution_id).first()
        if not execution:
            return Response({'error': '执行记录不存在'}, status=status.HTTP_404_NOT_FOUND)
        if execution.status != 'COMPLETED':
            return Response({'error': '只有正常完成的执行才能作为基线'},
                            status=status.HTTP_400_BAD_REQUEST)

        tolerance = request.data.get('tolerance') or PerfBaseline.DEFAULT_TOLERANCE
        baseline, _created = PerfBaseline.objects.update_or_create(
            scenario=execution.scenario,
            defaults={
                'execution': execution,
                'metrics': execution.summary or {},
                'tolerance': tolerance,
                'note': request.data.get('note', ''),
                'set_by': request.user,
            })
        log_operation('UPDATE', 'SCENARIO', execution.scenario_id,
                      execution.scenario.name, request.user,
                      description=f'设置性能基线（来源 {execution.execution_no}）')
        return Response(PerfBaselineSerializer(baseline).data)

    @action(detail=False, methods=['get'])
    def compare(self, request):
        """执行 vs 基线：判断是否劣化。"""
        execution_id = request.query_params.get('execution_id')
        execution = PerfExecution.objects.filter(id=execution_id).select_related(
            'scenario').first()
        if not execution:
            return Response({'error': '执行记录不存在'}, status=status.HTTP_404_NOT_FOUND)
        baseline = PerfBaseline.objects.filter(scenario=execution.scenario).first()
        if not baseline:
            return Response({'has_baseline': False, 'items': []})

        tolerance = {**PerfBaseline.DEFAULT_TOLERANCE, **(baseline.tolerance or {})}
        base = baseline.metrics or {}
        cur = execution.summary or {}

        items = []
        # 响应时间类：越大越差
        for key, label in (('avg_rt', '平均响应时间'), ('p95_rt', 'P95 响应时间'),
                           ('p99_rt', 'P99 响应时间')):
            b, c = base.get(key), cur.get(key)
            if not isinstance(b, (int, float)) or not b or not isinstance(c, (int, float)):
                continue
            change = round((c - b) / b * 100, 2)
            items.append({
                'metric': key, 'label': label, 'baseline': b, 'current': c,
                'change_pct': change, 'direction': 'lower_better',
                'degraded': change > tolerance['rt_degrade_pct'],
                'tolerance_pct': tolerance['rt_degrade_pct'],
            })
        # 吞吐类：越小越差
        for key, label in (('tps', 'TPS'), ('peak_tps', '峰值 TPS')):
            b, c = base.get(key), cur.get(key)
            if not isinstance(b, (int, float)) or not b or not isinstance(c, (int, float)):
                continue
            change = round((c - b) / b * 100, 2)
            items.append({
                'metric': key, 'label': label, 'baseline': b, 'current': c,
                'change_pct': change, 'direction': 'higher_better',
                'degraded': change < -tolerance['tps_degrade_pct'],
                'tolerance_pct': tolerance['tps_degrade_pct'],
            })

        return Response({
            'has_baseline': True,
            'baseline_execution_no': baseline.execution.execution_no
            if baseline.execution else '',
            'baseline_created_at': baseline.created_at,
            'degraded': any(i['degraded'] for i in items),
            'items': items,
        })


# ====================================================================== #
# 数据文件
# ====================================================================== #
class PerfDataFileViewSet(viewsets.ModelViewSet):
    queryset = PerfDataFile.objects.all().select_related('project', 'uploaded_by')
    serializer_class = PerfDataFileSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = FlexiblePageNumberPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['project', 'file_type']
    search_fields = ['name']
    ordering_fields = ['created_at', 'name']

    MAX_SIZE = 20 * 1024 * 1024  # 20MB：再大就该走数据库参数化了

    def create(self, request, *args, **kwargs):
        """上传文件。file_type=CSV(默认) 走参数化数据校验，JMX 走脚本校验，
        UPLOAD 为请求体上传文件（multipart/form-data 用），不限扩展名。"""
        file_type = (request.data.get('file_type') or 'CSV').upper()
        if file_type not in dict(PerfDataFile.FILE_TYPE_CHOICES):
            return Response({'error': f'不支持的文件类型：{file_type}'},
                            status=status.HTTP_400_BAD_REQUEST)
        if file_type == 'JMX':
            return self._create_jmx(request)
        if file_type == 'UPLOAD':
            return self._create_upload(request)
        return self._create_csv(request)

    # ------------------------------------------------------------------ #
    def _create_jmx(self, request):
        """JMeter 脚本上传：先静态解析确认是可执行的测试计划，再落盘。

        不做「先存后校验」——一个坏脚本留在磁盘上，下次压测才在 prepare 阶段炸，
        用户看到的是执行失败而不是上传失败，排查成本高一个数量级。
        """
        from .services import jmx_inspect

        upload = request.FILES.get('file')
        if not upload:
            return Response({'error': '请选择要上传的 .jmx 文件'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not upload.name.lower().endswith('.jmx'):
            return Response({'error': '脚本模式仅支持 JMeter .jmx 文件'},
                            status=status.HTTP_400_BAD_REQUEST)
        if upload.size > jmx_inspect.MAX_JMX_SIZE:
            return Response(
                {'error': f'.jmx 不能超过 {jmx_inspect.MAX_JMX_SIZE // 1024 // 1024}MB'},
                status=status.HTTP_400_BAD_REQUEST)

        try:
            raw = upload.read()
        finally:
            upload.seek(0)

        meta = jmx_inspect.inspect_jmx_bytes(raw)
        if not meta.get('valid'):
            return Response({'error': meta.get('error') or '.jmx 解析失败'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not meta.get('sampler_count'):
            return Response({'error': '.jmx 中没有任何采样器（Sampler），执行后不会产生任何请求'},
                            status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(data={
            'project': request.data.get('project'),
            'name': request.data.get('name') or upload.name,
            'file_type': 'JMX',
            'file': upload,
        })
        serializer.is_valid(raise_exception=True)
        instance = serializer.save(uploaded_by=request.user,
                                   columns=[], row_count=0,
                                   meta=jmx_inspect.summarize(meta))
        log_operation('CREATE', 'DATAFILE', instance.id, instance.name, request.user,
                      description=f'上传 JMeter 脚本，线程组 '
                                  f'{len(meta.get("thread_groups") or [])} 个 / '
                                  f'采样器 {meta.get("sampler_count")} 个')
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)

    # ------------------------------------------------------------------ #
    def _create_upload(self, request):
        """请求体上传文件：供压测步骤的 multipart/form-data 文件字段引用。

        不限扩展名（业务接口可能要求任意类型），只做大小限制；
        原始文件名与 Content-Type 记入 meta，引擎发送时用它还原 multipart 头。
        """
        upload = request.FILES.get('file')
        if not upload:
            return Response({'error': '请选择要上传的文件'},
                            status=status.HTTP_400_BAD_REQUEST)
        if upload.size > self.MAX_SIZE:
            return Response({'error': f'文件不能超过 {self.MAX_SIZE // 1024 // 1024}MB'},
                            status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(data={
            'project': request.data.get('project'),
            'name': request.data.get('name') or upload.name,
            'file_type': 'UPLOAD',
            'file': upload,
        })
        serializer.is_valid(raise_exception=True)
        instance = serializer.save(
            uploaded_by=request.user, columns=[], row_count=0,
            meta={'size': upload.size,
                  'content_type': upload.content_type or 'application/octet-stream'})
        log_operation('CREATE', 'DATAFILE', instance.id, instance.name, request.user,
                      description=f'上传请求文件（{upload.size // 1024}KB）')
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)

    # ------------------------------------------------------------------ #
    def _create_csv(self, request):
        upload = request.FILES.get('file')
        if not upload:
            return Response({'error': '请选择要上传的 CSV 文件'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not upload.name.lower().endswith('.csv'):
            return Response({'error': '仅支持 CSV 文件'},
                            status=status.HTTP_400_BAD_REQUEST)
        if upload.size > self.MAX_SIZE:
            return Response({'error': f'文件不能超过 {self.MAX_SIZE // 1024 // 1024}MB'},
                            status=status.HTTP_400_BAD_REQUEST)

        # 先解析再落库：格式不对就别浪费磁盘
        try:
            raw = upload.read()
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = raw.decode('gbk', errors='replace')
            rows = list(csv.reader(io.StringIO(text)))
        except Exception as exc:  # noqa: BLE001
            return Response({'error': f'CSV 解析失败：{exc}'},
                            status=status.HTTP_400_BAD_REQUEST)
        finally:
            upload.seek(0)

        if not rows:
            return Response({'error': 'CSV 文件为空'}, status=status.HTTP_400_BAD_REQUEST)
        columns = [c.strip() for c in rows[0]]
        if not any(columns):
            return Response({'error': 'CSV 首行必须是列名'},
                            status=status.HTTP_400_BAD_REQUEST)
        data_rows = [r for r in rows[1:] if any((c or '').strip() for c in r)]
        if not data_rows:
            return Response({'error': 'CSV 除表头外没有数据行'},
                            status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(data={
            'project': request.data.get('project'),
            'name': request.data.get('name') or upload.name,
            'file_type': 'CSV',
            'file': upload,
        })
        serializer.is_valid(raise_exception=True)
        instance = serializer.save(uploaded_by=request.user, columns=columns,
                                   row_count=len(data_rows), meta={})
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def preview(self, request, pk=None):
        """预览：CSV 返回前 N 行，JMX 返回脚本摘要 + XML 头部片段，UPLOAD 只回元信息。"""
        data_file = self.get_object()
        if data_file.file_type == 'JMX':
            return self._preview_jmx(data_file, request)
        if data_file.file_type == 'UPLOAD':
            return Response({'file_type': 'UPLOAD', 'meta': data_file.meta or {}})
        limit = min(int(request.query_params.get('limit') or 10), 100)
        try:
            with data_file.file.open('rb') as fh:
                raw = fh.read()
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = raw.decode('gbk', errors='replace')
            rows = list(csv.reader(io.StringIO(text)))
        except Exception as exc:  # noqa: BLE001
            return Response({'error': f'读取文件失败：{exc}'},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'file_type': 'CSV',
            'columns': rows[0] if rows else [],
            'rows': rows[1:limit + 1],
            'total_rows': max(len(rows) - 1, 0),
        })

    def _preview_jmx(self, data_file, request):
        """脚本预览：重新解析一次而不是直接吐 meta，避免文件被外部改动后信息失真。"""
        from .services import jmx_inspect

        head_lines = min(int(request.query_params.get('limit') or 40), 200)
        try:
            with data_file.file.open('rb') as fh:
                raw = fh.read()
        except Exception as exc:  # noqa: BLE001
            return Response({'error': f'读取脚本失败：{exc}'},
                            status=status.HTTP_400_BAD_REQUEST)
        meta = jmx_inspect.inspect_jmx_bytes(raw)
        text = raw.decode('utf-8', errors='replace')
        return Response({
            'file_type': 'JMX',
            'valid': bool(meta.get('valid')),
            'error': meta.get('error') or '',
            'meta': jmx_inspect.summarize(meta) if meta.get('valid') else {},
            'head': '\n'.join(text.splitlines()[:head_lines]),
        })

    def perform_destroy(self, instance):
        # 被场景变量引用中的文件不允许删，否则下次压测直接崩在准备阶段
        used_by = []
        for scenario in PerfScenario.objects.filter(project=instance.project):
            for var in scenario.variables or []:
                if str(var.get('data_file_id')) == str(instance.id):
                    used_by.append(scenario.name)
                    break
        if used_by:
            raise ValidationError(
                f'文件被以下场景引用，无法删除：{"、".join(used_by[:5])}')
        # 正在执行中的压测若引用了该脚本，删掉会让 JMeter 中途读不到文件
        if instance.file_type == 'JMX':
            running = PerfExecution.objects.filter(
                project=instance.project,
                status__in=PerfExecution.ACTIVE_STATUSES,
                script_ref__data_file_id=instance.id).exists()
            if running:
                raise ValidationError('该脚本正被执行中的压测使用，无法删除')
        try:
            instance.file.delete(save=False)
        except Exception:  # noqa: BLE001 - 文件可能已不在
            pass
        instance.delete()


# ====================================================================== #
# 定时任务
# ====================================================================== #
class PerfScheduledTaskViewSet(viewsets.ModelViewSet):
    queryset = PerfScheduledTask.objects.all().select_related(
        'scenario', 'scenario__project', 'created_by')
    serializer_class = PerfScheduledTaskSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = FlexiblePageNumberPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['scenario', 'status', 'trigger_type']
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'next_run_at']

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project')
        if project_id:
            qs = qs.filter(scenario__project_id=project_id)
        return qs

    def perform_create(self, serializer):
        task = serializer.save()
        log_operation('CREATE', 'TASK', task.id, task.name, self.request.user)

    def perform_update(self, serializer):
        task = serializer.save()
        log_operation('UPDATE', 'TASK', task.id, task.name, self.request.user)

    def perform_destroy(self, instance):
        log_operation('DELETE', 'TASK', instance.id, instance.name, self.request.user)
        instance.delete()

    @action(detail=True, methods=['post'])
    def toggle(self, request, pk=None):
        task = self.get_object()
        task.status = 'PAUSED' if task.status == 'ACTIVE' else 'ACTIVE'
        if task.status == 'ACTIVE':
            task.calculate_next_run()
        task.save(update_fields=['status', 'next_run_at'])
        log_operation('UPDATE', 'TASK', task.id, task.name, request.user,
                      description=f'切换状态为 {task.get_status_display()}')
        return Response(self.get_serializer(task).data)

    @action(detail=True, methods=['post'], url_path='run-now')
    def run_now(self, request, pk=None):
        """立即执行一次定时任务（不改变原有调度节奏）。"""
        task = self.get_object()
        scenario = task.scenario
        if scenario.has_active_execution():
            return Response({'error': '该场景已有正在执行的压测'},
                            status=status.HTTP_409_CONFLICT)

        execution, check = executor.start_execution(
            scenario, user=request.user, trigger_type='SCHEDULED', scheduled_task=task)
        if execution is None:
            return Response({'error': '执行前检查未通过', 'preflight': check},
                            status=status.HTTP_400_BAD_REQUEST)

        task.last_run_at = timezone.now()
        task.run_count += 1
        task.save(update_fields=['last_run_at', 'run_count'])
        log_operation('EXECUTE', 'TASK', task.id, task.name, request.user,
                      description=f'手动触发定时任务，执行 {execution.execution_no}')
        return Response({'execution': PerfExecutionDetailSerializer(execution).data,
                         'preflight': check}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def executions(self, request, pk=None):
        task = self.get_object()
        # 优先按外键精确归属；外键为空的历史记录才按「同场景 + 定时触发」兜底
        qs = PerfExecution.objects.filter(
            db_models.Q(scheduled_task=task)
            | db_models.Q(scheduled_task__isnull=True, scenario=task.scenario,
                          trigger_type='SCHEDULED')
        ).order_by('-created_at')[:50]
        return Response(PerfExecutionListSerializer(qs, many=True).data)


class EngineStatusView(APIView):
    """引擎与实时通道能力上报。

    前端据此：置灰未安装的引擎、决定走 WebSocket 还是轮询降级。
    channels 未安装或 CHANNEL_LAYERS 未配时 websocket=False，
    前端会直接用轮询，不做无谓的连接重试。
    """

    permission_classes = [IsAuthenticated]

    @staticmethod
    def _websocket_available():
        """WebSocket 实时通道是否"真正可用"。

        仅判断 channel layer 已配置是不够的：CHANNEL_LAYERS 用了 Redis 后端，
        若 Redis 进程没起（配置存在但不可达），executor 的 group_send 会全部
        静默失败（_PushWorker 熔断），前端连上 WS 却收不到任何实时样本，且因
        WS 显示“已连接”而不会降级到轮询——实时监控就一片空白。

        因此这里除通道层存在外，再用极短超时的 TCP 连接实测 Redis 可达性，
        不可达即视为 WebSocket 不可用，让前端走轮询（轮询读 DB 采样，同样能实时）。
        """
        try:
            from channels.layers import get_channel_layer
            if get_channel_layer() is None:
                return False
        except Exception:  # noqa: BLE001 - 未装 channels 属于预期情况
            return False

        from urllib.parse import urlparse
        url = (settings.REDIS_URL or '').strip()
        try:
            parsed = urlparse(url)
            host = parsed.hostname or '127.0.0.1'
            port = parsed.port or 6379
        except Exception:  # noqa: BLE001
            return False
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.4)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                return False
            finally:
                sock.close()
        except Exception:  # noqa: BLE001
            return False

    def get(self, request):
        websocket_ok = self._websocket_available()

        return Response({
            'engines': engine_status(),
            'websocket': websocket_ok,
            'limits': {
                'max_concurrency': settings.PERF_MAX_CONCURRENCY,
                'max_target_rps': settings.PERF_MAX_TARGET_RPS,
                'max_duration': settings.PERF_MAX_DURATION,
                'max_concurrent_executions': settings.PERF_MAX_CONCURRENT_EXECUTIONS,
            },
        })


class PerfComparisonReportViewSet(viewsets.ViewSet):
    """多轮执行对照报告：持久化快照 + 可选 AI 对照分析。"""
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _brief(report):
        return {
            'id': report.id,
            'title': report.title,
            'project_id': report.project_id,
            'execution_ids': report.execution_ids,
            'reference_execution_id': report.reference_execution_id,
            'has_ai_analysis': bool(report.ai_analysis),
            'created_by': report.created_by.username if report.created_by else '',
            'created_at': report.created_at,
        }

    def list(self, request):
        qs = PerfComparisonReport.objects.select_related('created_by')
        project_id = request.query_params.get('project')
        if project_id:
            qs = qs.filter(project_id=project_id)
        total = qs.count()
        rows = qs.order_by('-created_at')[:200]
        return Response({'items': [self._brief(r) for r in rows], 'total': total})

    def retrieve(self, request, pk=None):
        report = PerfComparisonReport.objects.filter(id=pk).select_related('created_by').first()
        if not report:
            return Response({'error': '对照报告不存在'}, status=status.HTTP_404_NOT_FOUND)
        data = self._brief(report)
        data['snapshot'] = report.snapshot
        data['ai_analysis'] = report.ai_analysis
        return Response(data)

    def destroy(self, request, pk=None):
        deleted, _ = PerfComparisonReport.objects.filter(id=pk).delete()
        if not deleted:
            return Response({'error': '对照报告不存在'}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def create(self, request):
        raw_ids = request.data.get('execution_ids') or []
        ids = list(dict.fromkeys(int(i) for i in raw_ids
                                 if str(i).lstrip('-').isdigit()))
        if len(ids) < 2:
            return Response({'error': '至少选择 2 次执行进行对比'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(ids) > 5:
            return Response({'error': '一次最多对比 5 次执行'},
                            status=status.HTTP_400_BAD_REQUEST)

        project_id = request.data.get('project_id')
        executions = list(PerfExecution.objects.filter(id__in=ids).select_related('scenario'))
        if len(executions) < len(ids):
            return Response({'error': '所选执行记录不存在'}, status=status.HTTP_404_NOT_FOUND)
        if project_id and any(str(e.project_id) != str(project_id) for e in executions):
            return Response({'error': '存在不属于该项目的执行记录'},
                            status=status.HTTP_400_BAD_REQUEST)
        executions.sort(key=lambda e: ids.index(e.id))

        reference_id = request.data.get('reference_execution_id')
        if reference_id is not None:
            try:
                reference_id = int(reference_id)
            except (TypeError, ValueError):
                reference_id = None
            if reference_id not in ids:
                return Response({'error': '基准执行必须在所选列表中'},
                                status=status.HTTP_400_BAD_REQUEST)

        from .services.compare_report import build_snapshot
        snapshot = build_snapshot(executions, reference_execution_id=reference_id)

        title = (request.data.get('title') or '').strip()
        if not title:
            from django.utils import timezone as tz
            title = f"对比报告 #{','.join(str(i) for i in ids[:5])} · " \
                    f"{tz.now().strftime('%Y-%m-%d %H:%M')}"

        report = PerfComparisonReport.objects.create(
            project_id=executions[0].project_id,
            title=title[:200],
            execution_ids=ids,
            reference_execution_id=snapshot.get('reference_execution_id'),
            snapshot=snapshot,
            created_by=request.user,
        )

        if request.data.get('ai_analyze'):
            from .tasks import comparison_ai_analysis_task
            comparison_ai_analysis_task.delay(report.id)

        return Response(self._brief(report), status=status.HTTP_201_CREATED)
