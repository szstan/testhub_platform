"""性能测试模块序列化器。

两个重点：
1. load_config / sla_config 是自由 JSON，必须在入口做 schema 校验 —— 否则
   非法配置会一路带到子进程才炸，用户看到的是一条 FAILED 记录而不是表单报错。
2. 场景变量里可能有密码/token，读接口要掩码，写接口要能「不传即保留原值」。
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (PerfBaseline, PerfDataFile, PerfExecution, PerfMetricSample,
                     PerfProject, PerfRequestStat, PerfScenario, PerfScenarioStep,
                     PerfScheduledTask)
from .services.variables import mask_secrets, merge_secret_values

User = get_user_model()

LOAD_MODELS = ('CONCURRENCY', 'RAMPING', 'RPS', 'SPIKE')
VARIABLE_TYPES = ('CONSTANT', 'RANDOM_INT', 'RANDOM_STRING', 'ENUM', 'UUID', 'TIMESTAMP', 'CSV')
EXTRACTOR_TYPES = ('JSON_PATH', 'REGEX', 'HEADER')
ASSERTION_TYPES = ('STATUS_CODE', 'CONTAINS', 'NOT_CONTAINS', 'JSON_PATH', 'RESPONSE_TIME', 'REGEX')


class SimpleUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email']


# ====================================================================== #
# 配置校验
# ====================================================================== #
def validate_load_config(value):
    """压力策略 schema 校验，同时兜住平台容量红线。"""
    if not isinstance(value, dict):
        raise serializers.ValidationError('压力策略必须是对象')

    model = (value.get('model') or 'CONCURRENCY').upper()
    if model not in LOAD_MODELS:
        raise serializers.ValidationError(f'不支持的压力模型：{model}')
    value['model'] = model

    def _positive_int(key, label, required=False, maximum=None):
        raw = value.get(key)
        if raw in (None, ''):
            if required:
                raise serializers.ValidationError(f'{label}不能为空')
            return 0
        try:
            num = int(raw)
        except (TypeError, ValueError):
            raise serializers.ValidationError(f'{label}必须是整数')
        if num < 0:
            raise serializers.ValidationError(f'{label}不能为负数')
        if required and num <= 0:
            raise serializers.ValidationError(f'{label}必须大于 0')
        if maximum and num > maximum:
            raise serializers.ValidationError(f'{label} {num} 超过平台上限 {maximum}')
        return num

    if model == 'CONCURRENCY':
        conc = _positive_int('concurrency', '并发用户数', required=True,
                             maximum=settings.PERF_MAX_CONCURRENCY)
        duration = _positive_int('duration', '压测时长', required=True,
                                 maximum=settings.PERF_MAX_DURATION)
        ramp = _positive_int('ramp_up', '加压时长')
        if ramp > duration:
            raise serializers.ValidationError('加压时长不能超过总压测时长')
        _ = conc

    elif model == 'RAMPING':
        stages = value.get('stages') or []
        if not isinstance(stages, list) or not stages:
            raise serializers.ValidationError('阶梯加压模式必须配置至少一个阶段')
        if len(stages) > 20:
            raise serializers.ValidationError('阶段数量不能超过 20 个')
        total = 0
        for idx, stage in enumerate(stages, 1):
            if not isinstance(stage, dict):
                raise serializers.ValidationError(f'第 {idx} 个阶段格式非法')
            try:
                target = int(stage.get('target') or 0)
                dur = int(stage.get('duration') or 0)
            except (TypeError, ValueError):
                raise serializers.ValidationError(f'第 {idx} 个阶段的目标并发/持续时间必须是整数')
            if target < 0 or dur <= 0:
                raise serializers.ValidationError(
                    f'第 {idx} 个阶段：目标并发不能为负、持续时间必须大于 0')
            if target > settings.PERF_MAX_CONCURRENCY:
                raise serializers.ValidationError(
                    f'第 {idx} 个阶段目标并发 {target} 超过平台上限 {settings.PERF_MAX_CONCURRENCY}')
            total += dur
        if total > settings.PERF_MAX_DURATION:
            raise serializers.ValidationError(
                f'各阶段总时长 {total}s 超过平台上限 {settings.PERF_MAX_DURATION}s')

    elif model == 'RPS':
        _positive_int('target_rps', '目标 RPS', required=True, maximum=settings.PERF_MAX_TARGET_RPS)
        _positive_int('duration', '压测时长', required=True, maximum=settings.PERF_MAX_DURATION)
        _positive_int('max_concurrency', '最大并发上限', maximum=settings.PERF_MAX_CONCURRENCY)

    elif model == 'SPIKE':
        base = _positive_int('baseline_concurrency', '基线并发',
                             maximum=settings.PERF_MAX_CONCURRENCY)
        spike = _positive_int('spike_concurrency', '尖峰并发', required=True,
                              maximum=settings.PERF_MAX_CONCURRENCY)
        spike_dur = _positive_int('spike_duration', '单次尖峰时长', required=True)
        times = _positive_int('spike_times', '尖峰次数', required=True)
        if spike <= base:
            raise serializers.ValidationError('尖峰并发必须大于基线并发，否则不构成尖峰')
        if spike_dur * 2 * times > settings.PERF_MAX_DURATION:
            raise serializers.ValidationError(
                f'尖峰总时长 {spike_dur * 2 * times}s 超过平台上限 {settings.PERF_MAX_DURATION}s')

    _positive_int('max_requests', '最大请求数上限')
    return value


def validate_sla_config(value):
    if not isinstance(value, dict):
        raise serializers.ValidationError('SLA 配置必须是对象')
    if not value.get('enabled'):
        return value

    from .services.sla import SLA_METRICS
    thresholds = value.get('thresholds') or {}
    if not isinstance(thresholds, dict):
        raise serializers.ValidationError('SLA 阈值必须是对象')

    cleaned = {}
    for key, raw in thresholds.items():
        if key not in SLA_METRICS:
            raise serializers.ValidationError(f'不支持的 SLA 指标：{key}')
        if raw in (None, ''):
            continue
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise serializers.ValidationError(f'SLA 指标 {key} 的阈值必须是数字')
        if num < 0:
            raise serializers.ValidationError(f'SLA 指标 {key} 的阈值不能为负数')
        if key == 'error_rate' and num > 100:
            raise serializers.ValidationError('错误率阈值不能超过 100%')
        cleaned[key] = num

    if not cleaned:
        raise serializers.ValidationError('启用 SLA 时至少要设置一个有效阈值')
    value['thresholds'] = cleaned

    if value.get('abort_on_breach'):
        window = value.get('breach_window') or 10
        try:
            window = int(window)
        except (TypeError, ValueError):
            raise serializers.ValidationError('熔断判定窗口必须是整数秒')
        if window < 1:
            raise serializers.ValidationError('熔断判定窗口至少 1 秒')
        value['breach_window'] = window
    return value


def validate_variables(value):
    if not isinstance(value, list):
        raise serializers.ValidationError('变量列表必须是数组')
    names = set()
    for idx, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise serializers.ValidationError(f'第 {idx} 个变量格式非法')
        name = (item.get('name') or '').strip()
        if not name:
            raise serializers.ValidationError(f'第 {idx} 个变量缺少名称')
        if name in names:
            raise serializers.ValidationError(f'变量名重复：{name}')
        names.add(name)
        vtype = (item.get('type') or 'CONSTANT').upper()
        if vtype not in VARIABLE_TYPES:
            raise serializers.ValidationError(f'变量 {name} 的类型 {vtype} 不支持')
        item['type'] = vtype
        if vtype == 'RANDOM_INT':
            try:
                if int(item.get('min', 0)) > int(item.get('max', 0)):
                    raise serializers.ValidationError(f'变量 {name} 的最小值大于最大值')
            except (TypeError, ValueError):
                raise serializers.ValidationError(f'变量 {name} 的取值范围必须是整数')
        if vtype == 'ENUM':
            # 运行时求值统一读 values；options 是早期字段名，这里做一次归一化，
            # 否则「校验通过但运行时永远取到空串」这种问题根本查不出来。
            values = item.get('values') or item.get('options') or []
            if not isinstance(values, list) or not values:
                raise serializers.ValidationError(f'枚举变量 {name} 必须配置候选值')
            item['values'] = values
            item.pop('options', None)
        if vtype == 'CSV' and not item.get('data_file_id'):
            raise serializers.ValidationError(f'CSV 变量 {name} 必须选择数据文件')
    return value


# ====================================================================== #
# 项目
# ====================================================================== #
class PerfProjectSerializer(serializers.ModelSerializer):
    owner = SimpleUserSerializer(read_only=True)
    members = SimpleUserSerializer(many=True, read_only=True)
    member_ids = serializers.ListField(child=serializers.IntegerField(),
                                       write_only=True, required=False)
    scenario_count = serializers.SerializerMethodField()
    execution_count = serializers.SerializerMethodField()

    class Meta:
        model = PerfProject
        fields = ['id', 'name', 'description', 'status', 'default_env', 'owner',
                  'members', 'member_ids', 'scenario_count', 'execution_count',
                  'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def get_scenario_count(self, obj):
        if hasattr(obj, 'scenario_count_anno'):
            return obj.scenario_count_anno or 0
        return obj.scenarios.count()

    def get_execution_count(self, obj):
        if hasattr(obj, 'execution_count_anno'):
            return obj.execution_count_anno or 0
        return obj.executions.count()

    def create(self, validated_data):
        member_ids = validated_data.pop('member_ids', [])
        validated_data['owner'] = self.context['request'].user
        project = super().create(validated_data)
        if member_ids:
            project.members.set(User.objects.filter(id__in=member_ids))
        return project

    def update(self, instance, validated_data):
        member_ids = validated_data.pop('member_ids', None)
        project = super().update(instance, validated_data)
        if member_ids is not None:
            project.members.set(User.objects.filter(id__in=member_ids))
        return project


# ====================================================================== #
# 步骤 / 场景
# ====================================================================== #
class PerfScenarioStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = PerfScenarioStep
        fields = ['id', 'scenario', 'order', 'name', 'enabled', 'source_request',
                  'method', 'url', 'headers', 'params', 'body_type', 'body', 'files',
                  'extractors', 'assertions', 'think_time', 'weight', 'is_setup']

    def validate_name(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('步骤名称不能为空（它同时是指标聚合标识）')
        return value

    def validate_url(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('请求 URL 不能为空')
        return value

    def validate_files(self, value):
        """multipart 文件字段归一化：[{field, file_id, filename, content_type}]。

        归属校验（file_id 必须属于当前场景项目的 UPLOAD 文件）由 save-steps
        视图结合场景上下文完成，此处只做结构合法性检查。
        """
        if value in (None, ''):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError('文件字段必须是数组')
        cleaned = []
        for idx, item in enumerate(value, 1):
            if not isinstance(item, dict):
                raise serializers.ValidationError(f'第 {idx} 个文件字段格式非法')
            field = str(item.get('field') or '').strip()
            if not field:
                raise serializers.ValidationError(f'第 {idx} 个文件字段缺少字段名(field)')
            file_id = item.get('file_id')
            if file_id in ('', None):
                file_id = None
            else:
                try:
                    file_id = int(file_id)
                except (TypeError, ValueError):
                    raise serializers.ValidationError(f'第 {idx} 个文件字段的 file_id 必须是整数')
            cleaned.append({
                'field': field,
                'file_id': file_id,
                'filename': str(item.get('filename') or '')[:200],
                'content_type': str(item.get('content_type') or '')[:100],
            })
        return cleaned

    def validate_extractors(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('提取规则必须是数组')
        for idx, item in enumerate(value, 1):
            if not isinstance(item, dict):
                raise serializers.ValidationError(f'第 {idx} 条提取规则格式非法')
            if not (item.get('name') or '').strip():
                raise serializers.ValidationError(f'第 {idx} 条提取规则缺少变量名')
            etype = (item.get('type') or 'JSON_PATH').upper()
            if etype not in EXTRACTOR_TYPES:
                raise serializers.ValidationError(f'第 {idx} 条提取规则类型 {etype} 不支持')
            item['type'] = etype
            if not (item.get('expr') or '').strip():
                raise serializers.ValidationError(f'第 {idx} 条提取规则缺少表达式')
        return value

    def validate_assertions(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('断言规则必须是数组')
        for idx, item in enumerate(value, 1):
            if not isinstance(item, dict):
                raise serializers.ValidationError(f'第 {idx} 条断言格式非法')
            atype = (item.get('type') or '').upper()
            if atype not in ASSERTION_TYPES:
                raise serializers.ValidationError(f'第 {idx} 条断言类型 {atype} 不支持')
            item['type'] = atype
            if item.get('expected') in (None, ''):
                raise serializers.ValidationError(f'第 {idx} 条断言缺少期望值')
            if atype == 'JSON_PATH' and not (item.get('json_path') or '').strip():
                raise serializers.ValidationError(f'第 {idx} 条 JSON_PATH 断言缺少路径表达式')
        return value


class PerfScenarioListSerializer(serializers.ModelSerializer):
    """列表页轻量序列化：不返回大 JSON 字段，避免列表接口体积失控。"""

    project_name = serializers.CharField(source='project.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    step_count = serializers.IntegerField(source='steps.count', read_only=True)
    load_model = serializers.SerializerMethodField()
    last_execution = serializers.SerializerMethodField()

    class Meta:
        model = PerfScenario
        fields = ['id', 'project', 'project_name', 'name', 'description', 'engine',
                  'enabled', 'step_count', 'load_model', 'last_execution',
                  'created_by_name', 'created_at', 'updated_at']

    def get_load_model(self, obj):
        return (obj.load_config or {}).get('model', 'CONCURRENCY')

    def get_last_execution(self, obj):
        # 优先使用 ViewSet 列表查询里的关联子查询注解（避免 N+1）；
        # 未注解时（列表外的其它场景）回退为单条查询。
        exec_id = getattr(obj, '_latest_exec_id', None)
        if exec_id is not None:
            summary = getattr(obj, '_latest_summary', None) or {}
            return {
                'id': exec_id,
                'execution_no': getattr(obj, '_latest_exec_no', ''),
                'status': getattr(obj, '_latest_exec_status', ''),
                'sla_result': getattr(obj, '_latest_exec_sla', ''),
                'tps': summary.get('tps'),
                'p95_rt': summary.get('p95_rt'),
                'error_rate': summary.get('error_rate'),
                'created_at': getattr(obj, '_latest_exec_created', None),
            }
        execution = obj.executions.order_by('-created_at').first()
        if not execution:
            return None
        return {
            'id': execution.id,
            'execution_no': execution.execution_no,
            'status': execution.status,
            'sla_result': execution.sla_result,
            'tps': (execution.summary or {}).get('tps'),
            'p95_rt': (execution.summary or {}).get('p95_rt'),
            'error_rate': (execution.summary or {}).get('error_rate'),
            'created_at': execution.created_at,
        }


class PerfScenarioSerializer(serializers.ModelSerializer):
    steps = PerfScenarioStepSerializer(many=True, read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    created_by = SimpleUserSerializer(read_only=True)
    has_active_execution = serializers.SerializerMethodField()

    class Meta:
        model = PerfScenario
        fields = ['id', 'project', 'project_name', 'name', 'description', 'engine',
                  'load_config', 'sla_config', 'perf_targets', 'variables', 'env_config',
                  'runtime_config', 'enabled', 'steps', 'created_by',
                  'has_active_execution', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def get_has_active_execution(self, obj):
        return obj.has_active_execution()

    def validate_load_config(self, value):
        return validate_load_config(value or {})

    def validate_sla_config(self, value):
        return validate_sla_config(value or {})

    def validate_variables(self, value):
        return validate_variables(value or [])

    def validate_env_config(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('环境配置必须是对象')
        base_url = (value.get('base_url') or '').strip()
        if base_url and not base_url.startswith(('http://', 'https://')):
            raise serializers.ValidationError('环境基址必须以 http:// 或 https:// 开头')
        value['base_url'] = base_url.rstrip('/')
        headers = value.get('headers')
        if headers is not None and not isinstance(headers, dict):
            raise serializers.ValidationError('公共请求头必须是对象')
        return value

    def validate_runtime_config(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('运行时配置必须是对象')
        for key, label, lo, hi in (('timeout', '请求超时', 1, 300),
                                   ('sample_interval', '采样间隔', 1, 60)):
            if value.get(key) in (None, ''):
                continue
            try:
                num = int(value[key])
            except (TypeError, ValueError):
                raise serializers.ValidationError(f'{label}必须是整数')
            if not lo <= num <= hi:
                raise serializers.ValidationError(f'{label}应在 {lo}~{hi} 之间')
            value[key] = num

        # script_ref 落库前先削平：只保留 mode 与 data_file_id。
        # jmx_path 之类的路径字段一律丢弃，真实路径由 views.resolve_script_ref
        # 从 PerfDataFile 反查并校验落在 MEDIA_ROOT 内，避免目录穿越。
        script_ref = value.get('script_ref')
        if script_ref not in (None, '', {}):
            if not isinstance(script_ref, dict):
                raise serializers.ValidationError('script_ref 必须是对象')
            mode = str(script_ref.get('mode') or 'scenario').strip().lower()
            if mode not in ('scenario', 'script'):
                raise serializers.ValidationError(f'不支持的执行模式：{mode}')
            if mode == 'script':
                if not script_ref.get('data_file_id'):
                    raise serializers.ValidationError('脚本模式必须选择一个 .jmx 文件')
                value['script_ref'] = {'mode': 'script',
                                       'data_file_id': script_ref['data_file_id']}
            else:
                value['script_ref'] = {'mode': 'scenario'}
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['variables'] = mask_secrets(data.get('variables') or [])
        return data

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        # 变量里的密文字段：前端回传掩码时保留原值，避免"编辑一次密码就丢"
        if 'variables' in validated_data:
            validated_data['variables'] = merge_secret_values(
                validated_data['variables'], instance.variables or [])
        return super().update(instance, validated_data)


# ====================================================================== #
# 执行
# ====================================================================== #
class PerfRequestStatSerializer(serializers.ModelSerializer):
    class Meta:
        model = PerfRequestStat
        exclude = ['execution']


class PerfMetricSampleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PerfMetricSample
        exclude = ['id', 'execution']


class PerfExecutionListSerializer(serializers.ModelSerializer):
    scenario_name = serializers.CharField(source='scenario.name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    executed_by_name = serializers.SerializerMethodField()
    scheduled_task_name = serializers.CharField(source='scheduled_task.name', read_only=True,
                                                default=None)
    tps = serializers.SerializerMethodField()
    p95_rt = serializers.SerializerMethodField()
    error_rate = serializers.SerializerMethodField()
    total_requests = serializers.SerializerMethodField()
    progress = serializers.FloatField(read_only=True)

    class Meta:
        model = PerfExecution
        fields = ['id', 'execution_no', 'scenario', 'scenario_name', 'project',
                  'project_name', 'trigger_type', 'status', 'sla_result',
                  'executed_by_name', 'scheduled_task', 'scheduled_task_name',
                  'tps', 'p95_rt', 'error_rate', 'total_requests',
                  'progress', 'start_time', 'end_time', 'duration', 'created_at']

    def get_executed_by_name(self, obj):
        return obj.executed_by.username if obj.executed_by else '系统'

    def get_tps(self, obj):
        return (obj.summary or {}).get('tps')

    def get_p95_rt(self, obj):
        return (obj.summary or {}).get('p95_rt')

    def get_error_rate(self, obj):
        return (obj.summary or {}).get('error_rate')

    def get_total_requests(self, obj):
        return (obj.summary or {}).get('total_requests')


class PerfExecutionDetailSerializer(serializers.ModelSerializer):
    scenario_name = serializers.CharField(source='scenario.name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    executed_by = SimpleUserSerializer(read_only=True)
    request_stats = PerfRequestStatSerializer(many=True, read_only=True)
    progress = serializers.FloatField(read_only=True)
    has_raw_detail = serializers.SerializerMethodField()

    class Meta:
        model = PerfExecution
        fields = ['id', 'execution_no', 'scenario', 'scenario_name', 'project',
                  'project_name', 'trigger_type', 'status', 'sla_result',
                  'verdict', 'verdict_details',
                  'load_snapshot', 'steps_snapshot', 'summary', 'sla_detail',
                  'error_message', 'artifact_dir', 'report_url', 'worker_host',
                  'executed_by', 'request_stats', 'progress', 'has_raw_detail',
                  'start_time', 'end_time', 'duration', 'created_at']

    def get_has_raw_detail(self, obj):
        import os
        from django.conf import settings as dj_settings
        if not obj.artifact_dir:
            return False
        return os.path.isfile(os.path.join(
            dj_settings.MEDIA_ROOT, obj.artifact_dir, 'raw.csv.gz'))


# ====================================================================== #
# 基线 / 数据文件 / 定时任务
# ====================================================================== #
class PerfBaselineSerializer(serializers.ModelSerializer):
    scenario_name = serializers.CharField(source='scenario.name', read_only=True)
    execution_no = serializers.CharField(source='execution.execution_no', read_only=True)
    set_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PerfBaseline
        fields = ['id', 'scenario', 'scenario_name', 'execution', 'execution_no',
                  'metrics', 'tolerance', 'note', 'set_by_name', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def get_set_by_name(self, obj):
        return obj.set_by.username if obj.set_by else ''

    def validate_tolerance(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('容忍度必须是对象')
        for key in ('rt_degrade_pct', 'tps_degrade_pct'):
            if value.get(key) in (None, ''):
                continue
            try:
                num = float(value[key])
            except (TypeError, ValueError):
                raise serializers.ValidationError(f'{key} 必须是数字')
            if not 0 <= num <= 1000:
                raise serializers.ValidationError(f'{key} 应在 0~1000 之间')
            value[key] = num
        return value


class PerfDataFileSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.SerializerMethodField()
    file_size = serializers.SerializerMethodField()

    class Meta:
        model = PerfDataFile
        fields = ['id', 'project', 'name', 'file_type', 'file', 'columns', 'row_count',
                  'meta', 'uploaded_by_name', 'file_size', 'created_at']
        # columns/row_count/meta 一律由 ViewSet 解析后写入，不接受前端伪造
        read_only_fields = ['columns', 'row_count', 'meta', 'created_at']

    def get_uploaded_by_name(self, obj):
        return obj.uploaded_by.username if obj.uploaded_by else ''

    def get_file_size(self, obj):
        try:
            return obj.file.size
        except Exception:  # noqa: BLE001 - 文件可能已被清理
            return 0


class PerfScheduledTaskSerializer(serializers.ModelSerializer):
    scenario_name = serializers.CharField(source='scenario.name', read_only=True)
    project = serializers.IntegerField(source='scenario.project_id', read_only=True)
    project_name = serializers.CharField(source='scenario.project.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = PerfScheduledTask
        fields = ['id', 'scenario', 'scenario_name', 'project', 'project_name', 'name',
                  'description', 'trigger_type', 'cron_expression', 'interval_minutes',
                  'scheduled_time', 'status', 'next_run_at', 'last_run_at', 'run_count',
                  'success_count', 'fail_count', 'notify_channels', 'notify_on',
                  'last_error', 'created_by_name', 'created_at', 'updated_at']
        read_only_fields = ['next_run_at', 'last_run_at', 'run_count', 'success_count',
                            'fail_count', 'last_error', 'created_at', 'updated_at']

    def validate(self, attrs):
        trigger = attrs.get('trigger_type') or getattr(self.instance, 'trigger_type', 'CRON')
        if trigger == 'CRON':
            expr = attrs.get('cron_expression') or getattr(self.instance, 'cron_expression', '')
            if not expr:
                raise serializers.ValidationError({'cron_expression': 'Cron 表达式不能为空'})
            try:
                from croniter import croniter
                if not croniter.is_valid(expr):
                    raise ValueError
            except ImportError:
                pass
            except Exception:
                raise serializers.ValidationError({'cron_expression': 'Cron 表达式格式非法'})
        elif trigger == 'INTERVAL':
            minutes = attrs.get('interval_minutes') or getattr(self.instance, 'interval_minutes', None)
            if not minutes or int(minutes) < 1:
                raise serializers.ValidationError({'interval_minutes': '间隔分钟数必须大于 0'})
        elif trigger == 'ONCE':
            when = attrs.get('scheduled_time') or getattr(self.instance, 'scheduled_time', None)
            if not when:
                raise serializers.ValidationError({'scheduled_time': '单次执行时间不能为空'})
        return attrs

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        task = super().create(validated_data)
        task.calculate_next_run()
        task.save(update_fields=['next_run_at'])
        return task

    def update(self, instance, validated_data):
        task = super().update(instance, validated_data)
        task.calculate_next_run()
        task.save(update_fields=['next_run_at'])
        return task
