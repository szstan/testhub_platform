# -*- coding: utf-8 -*-
from typing import Optional
import math
import re

from rest_framework import serializers
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from django.core.signing import TimestampSigner
from .runtime_config import validate_base_url
from .access import (
    accessible_projects,
    scope_test_cases,
    scope_executions,
    scheduled_task_relation_errors,
)
from .models import (
    AppProject,
    AppTestConfig,
    AppDevice,
    AppElement,
    AppComponent,
    AppCustomComponent,
    AppComponentPackage,
    AppPackage,
    AppTestCase,
    AppTestSuite,
    AppTestSuiteCase,
    AppTestExecution,
    AppScheduledTask,
    AppNotificationLog,
)
from .scene_component_runtime import SceneComponentError, validate_project_component_references


_SECRET_ALIAS_RE = re.compile(r'secret://[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_VARIABLE_REFERENCE_RE = re.compile(
    r'(?:\{\{\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*\s*\}\}'
    r'|\$\{\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*\s*\})\Z'
)
_NESTED_SCENE_STEP_FIELDS = ('steps', 'then_steps', 'else_steps', 'try_steps', 'catch_steps', 'finally_steps')
_MAX_SCENE_STEP_DEPTH = 16


def _is_empty_sensitive_value(value):
    return (
        value is None
        or (isinstance(value, str) and not value.strip())
        or (isinstance(value, (list, dict)) and not value)
    )


def _validate_sensitive_variables(variables):
    if not isinstance(variables, list):
        return
    for variable in variables:
        if not isinstance(variable, dict) or variable.get('sensitive') is not True:
            continue
        value = variable.get('value')
        if _is_empty_sensitive_value(value):
            continue
        if not isinstance(value, str) or not _SECRET_ALIAS_RE.fullmatch(value):
            raise serializers.ValidationError('敏感变量只能留空或使用 secret:// 别名')


def _is_exact_scene_variable_reference(value):
    if not isinstance(value, str) or not _VARIABLE_REFERENCE_RE.fullmatch(value):
        return False
    expression = value[2:-2] if value.startswith('{{') else value[2:-1]
    root = expression.strip().split('.', 1)[0].split('[', 1)[0].strip()
    return root != 'params'


def _validate_sensitive_flow(ui_flow):
    if isinstance(ui_flow, dict):
        if 'steps' not in ui_flow:
            return
        ui_flow = ui_flow['steps']
        if not isinstance(ui_flow, list):
            raise serializers.ValidationError('ui_flow.steps 必须是步骤列表')
    elif not isinstance(ui_flow, list):
        return

    def visit(steps, depth):
        if depth > _MAX_SCENE_STEP_DEPTH:
            raise serializers.ValidationError('敏感输入配置嵌套过深')
        for step in steps:
            if not isinstance(step, dict):
                continue
            config = step.get('config')
            containers = [step]
            if isinstance(config, dict):
                containers.append(config)
            action = step.get('type')
            if (
                isinstance(action, str)
                and action.lower() in {'input', 'text'}
                and any(container.get('sensitive') is True for container in containers)
            ):
                for container in containers:
                    for key in ('value', 'text'):
                        value = container.get(key)
                        if _is_empty_sensitive_value(value):
                            continue
                        if not _is_exact_scene_variable_reference(value):
                            raise serializers.ValidationError('敏感输入只能留空或使用变量引用')

            for container in containers:
                for field in _NESTED_SCENE_STEP_FIELDS:
                    nested = container.get(field)
                    if isinstance(nested, list):
                        visit(nested, depth + 1)

    visit(ui_flow, 0)


# ========== 项目管理序列化器 ==========

class AppProjectSerializer(serializers.ModelSerializer):
    """APP项目序列化器 - 列表/详情"""
    owner_name = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    test_case_count = serializers.SerializerMethodField()
    test_suite_count = serializers.SerializerMethodField()

    class Meta:
        model = AppProject
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    def get_owner_name(self, obj) -> Optional[str]:
        return obj.owner.username if obj.owner else None

    def get_member_count(self, obj):
        return obj.members.count()

    def get_test_case_count(self, obj):
        return obj.test_cases.count()

    def get_test_suite_count(self, obj):
        return obj.test_suites.count()


class AppProjectCreateSerializer(serializers.ModelSerializer):
    """APP项目创建序列化器"""
    class Meta:
        model = AppProject
        fields = ('name', 'description', 'status', 'start_date', 'end_date', 'members')
        extra_kwargs = {
            'members': {'required': False},
        }


class AppProjectUpdateSerializer(serializers.ModelSerializer):
    """APP项目更新序列化器"""
    class Meta:
        model = AppProject
        fields = ('name', 'description', 'status', 'start_date', 'end_date', 'members')


# ========== 配置序列化器 ==========

class AppTestConfigSerializer(serializers.ModelSerializer):
    """APP测试配置序列化器"""
    
    class Meta:
        model = AppTestConfig
        fields = ['id', 'adb_path', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class AppDeviceListSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        devices = data.all() if hasattr(data, 'all') else data
        if hasattr(devices, 'annotate'):
            pending_executions = AppTestExecution.objects.filter(
                device_id=OuterRef('pk'),
                device_action_pending=True,
            )
            devices = devices.annotate(
                _has_pending_execution=Exists(pending_executions)
            )
            return super().to_representation(devices)

        devices = list(devices)
        if not devices:
            return []
        pending_device_ids = set(AppTestExecution.objects.filter(
            device_id__in=[device.pk for device in devices],
            device_action_pending=True,
        ).values_list('device_id', flat=True))
        self.child._pending_action_device_ids = pending_device_ids
        try:
            return super().to_representation(devices)
        finally:
            del self.child._pending_action_device_ids


class AppDeviceSerializer(serializers.ModelSerializer):
    """APP设备序列化器"""
    locked_by_name = serializers.SerializerMethodField()
    lease_requires_token = serializers.SerializerMethodField()
    has_pending_action = serializers.SerializerMethodField()
    
    class Meta:
        model = AppDevice
        fields = [
            'id', 'device_id', 'name', 'status', 'android_version', 'connection_type',
            'ip_address', 'port', 'locked_by', 'locked_by_name', 'lease_requires_token', 'locked_at',
            'max_allocation_time', 'lease_acquired_at', 'lease_expires_at',
            'last_heartbeat_at', 'capabilities', 'health_status', 'maintenance_mode',
            'has_pending_action', 'last_seen_at', 'device_specs', 'description', 'location',
            'created_at', 'updated_at',
        ]
        read_only_fields = (
            'id', 'locked_by', 'locked_by_name', 'locked_at', 'lease_acquired_at',
            'lease_expires_at', 'last_heartbeat_at', 'last_seen_at', 'created_at', 'updated_at',
        )
        list_serializer_class = AppDeviceListSerializer
    
    def get_locked_by_name(self, obj) -> Optional[str]:
        return obj.locked_by.username if obj.locked_by else None

    def get_lease_requires_token(self, obj) -> bool:
        return bool(obj.lease_token_hash)

    def get_has_pending_action(self, obj) -> bool:
        pending_execution = getattr(obj, '_has_pending_execution', None)
        if pending_execution is not None:
            return bool(obj.device_action_pending or pending_execution)

        pending_device_ids = getattr(self, '_pending_action_device_ids', None)
        if pending_device_ids is not None:
            return bool(obj.device_action_pending or obj.pk in pending_device_ids)

        if obj.device_action_pending:
            return True
        return AppTestExecution.objects.filter(
            device_id=obj.pk,
            device_action_pending=True,
        ).exists()


class AppElementSerializer(serializers.ModelSerializer):
    """APP元素序列化器"""
    created_by_name = serializers.SerializerMethodField()
    element_type_display = serializers.CharField(source='get_element_type_display', read_only=True)
    preview_url = serializers.SerializerMethodField()
    
    class Meta:
        model = AppElement
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'usage_count', 'last_used_at')
    
    def validate_project(self, value):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is not None and not accessible_projects(user).filter(pk=value.pk).exists():
            raise serializers.ValidationError('无权使用该 APP 项目')
        return value

    def get_created_by_name(self, obj) -> Optional[str]:
        return obj.created_by.username if obj.created_by else None
    
    def get_preview_url(self, obj) -> Optional[str]:
        """获取图片预览 URL"""
        if obj.element_type == 'image' and obj.config:
            image_path = obj.config.get('image_path')
            if image_path:
                from .utils.image_helpers import get_element_image_url
                return get_element_image_url(image_path)
        return None
    
    def validate_config(self, value):
        """验证配置项"""
        element_type = self.initial_data.get('element_type')
        
        if element_type == 'image':
            if not value.get('image_path'):
                raise serializers.ValidationError('图片元素必须包含 image_path 字段')
            
            # 如果有 file_hash，可以进行重复检测（在视图层处理更合适）
            file_hash = value.get('file_hash')
            if file_hash:
                # 检查是否有其他元素使用相同哈希（排除自身）
                instance_id = self.instance.id if self.instance else None
                existing = AppElement.objects.filter(
                    config__file_hash=file_hash
                ).exclude(id=instance_id).first()
                
                if existing:
                    raise serializers.ValidationError(
                        f'相同的图片已被元素 "{existing.name}" (ID: {existing.id}) 使用。'
                        f'建议复制该元素或上传不同的图片。'
                    )
        
        elif element_type == 'pos':
            # 坐标类型需要 x, y
            if 'x' not in value or 'y' not in value:
                raise serializers.ValidationError('坐标元素必须包含 x 和 y 字段')
        
        elif element_type == 'region':
            # 区域类型需要 x1, y1, x2, y2
            required_fields = ['x1', 'y1', 'x2', 'y2']
            missing_fields = [f for f in required_fields if f not in value]
            if missing_fields:
                raise serializers.ValidationError(
                    f'区域元素缺少必需字段: {", ".join(missing_fields)}'
                )
        
        return value
    
    def to_representation(self, instance):
        data = super().to_representation(instance)
        return data


class AppPackageSerializer(serializers.ModelSerializer):
    """APP应用包名序列化器"""
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AppPackage
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    def get_created_by_name(self, obj) -> Optional[str]:
        return obj.created_by.username if obj.created_by else None


class AppTestCaseSerializer(serializers.ModelSerializer):
    """APP测试用例序列化器"""
    created_by_name = serializers.SerializerMethodField()
    app_package_name = serializers.CharField(source='app_package.name', read_only=True)

    class Meta:
        model = AppTestCase
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    def validate_variables(self, value):
        _validate_sensitive_variables(value)
        return value

    def validate_ui_flow(self, value):
        _validate_sensitive_flow(value)
        return value

    def validate(self, attrs):
        flow_was_supplied = 'ui_flow' in attrs
        project_was_supplied = 'project' in attrs
        if not flow_was_supplied and not project_was_supplied:
            return attrs

        flow = attrs.get('ui_flow', getattr(self.instance, 'ui_flow', None))
        if isinstance(flow, dict):
            flow_steps = flow.get('steps', [])
        elif isinstance(flow, list):
            flow_steps = flow
        else:
            flow_steps = []
        project = attrs.get('project', getattr(self.instance, 'project', None))
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        try:
            validate_project_component_references(flow_steps, project, user)
        except SceneComponentError:
            raise serializers.ValidationError({'ui_flow': '项目组件配置无效或无权访问'}) from None
        return attrs

    def validate_project(self, value):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is not None and not accessible_projects(user).filter(pk=value.pk).exists():
            raise serializers.ValidationError('无权使用该 APP 项目')
        return value

    def validate_runtime_config(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('runtime_config 必须是 JSON 对象')
        supported = {'base_url', 'retry_times', 'retry_interval', 'ocr_languages', 'ocr_use_gpu', 'stop_on_error'}
        if set(value) - supported:
            raise serializers.ValidationError('runtime_config 包含不支持的配置项')

        if 'base_url' in value:
            try:
                validate_base_url(value['base_url'])
            except ValueError as exc:
                raise serializers.ValidationError(str(exc)) from None

        retry_times = value.get('retry_times')
        if retry_times is not None and (
            isinstance(retry_times, bool)
            or not isinstance(retry_times, int)
            or not 0 <= retry_times <= 10
        ):
            raise serializers.ValidationError('retry_times 必须是 0 到 10 之间的整数')

        retry_interval = value.get('retry_interval')
        if retry_interval is not None and (
            isinstance(retry_interval, bool)
            or not isinstance(retry_interval, (int, float))
            or retry_interval < 0
            or retry_interval > 10
            or not math.isfinite(retry_interval)
        ):
            raise serializers.ValidationError('retry_interval 必须是 0 到 10 之间的有限数值')

        languages = value.get('ocr_languages')
        if languages is not None and (
            not isinstance(languages, list)
            or not languages
            or len(languages) > 2
            or any(not isinstance(language, str) or language not in ('en', 'ch_sim') for language in languages)
            or len(set(languages)) != len(languages)
        ):
            raise serializers.ValidationError('ocr_languages 仅支持不重复的 en / ch_sim 列表')

        use_gpu = value.get('ocr_use_gpu')
        if use_gpu is not None and not isinstance(use_gpu, bool):
            raise serializers.ValidationError('ocr_use_gpu 必须是布尔值')

        stop_on_error = value.get('stop_on_error')
        if stop_on_error is not None and not isinstance(stop_on_error, bool):
            raise serializers.ValidationError('stop_on_error 必须是布尔值')

        return value

    def get_created_by_name(self, obj) -> Optional[str]:
        return obj.created_by.username if obj.created_by else None


class AppTestExecutionSerializer(serializers.ModelSerializer):
    """APP测试执行记录序列化器"""
    case_name = serializers.CharField(read_only=True)
    device_name = serializers.CharField(read_only=True)
    user_name = serializers.CharField(read_only=True)
    report_access_token = serializers.SerializerMethodField()


    pass_rate = serializers.FloatField(read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    result_display = serializers.CharField(source='get_result_display', read_only=True, default=None)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Preserve the legacy string contract without exposing filesystem paths.
        data['report_path'] = ''
        return data

    def get_report_access_token(self, obj) -> str:
        """Mint a short-lived share token only for an authorized request."""
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not self.context.get('include_report_access_token', True):
            return ''
        if not obj.report_path or not request or not getattr(user, 'is_authenticated', False):
            return ''
        if not scope_executions(
            AppTestExecution.objects.filter(pk=obj.pk), user
        ).exists():
            return ''
        return TimestampSigner().sign(str(obj.pk))

    class Meta:
        model = AppTestExecution
        fields = '__all__'
        read_only_fields = (
            'created_at', 'updated_at', 'started_at', 'finished_at', 'duration',
            'stop_requested', 'release_device_lease_on_stop',
        )


class AppComponentSerializer(serializers.ModelSerializer):
    """UI组件定义序列化器"""
    
    class Meta:
        model = AppComponent
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')


class AppCustomComponentSerializer(serializers.ModelSerializer):
    """自定义组件定义序列化器"""
    
    class Meta:
        model = AppCustomComponent
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')


class AppComponentPackageSerializer(serializers.ModelSerializer):
    """组件包序列化器"""
    created_by_name = serializers.SerializerMethodField()
    
    class Meta:
        model = AppComponentPackage
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')
    
    def get_created_by_name(self, obj) -> Optional[str]:
        return obj.created_by.username if obj.created_by else None


# ========== 测试套件序列化器 ==========

class AppTestSuiteCaseSerializer(serializers.ModelSerializer):
    """套件-用例关联序列化器"""
    test_case = serializers.SerializerMethodField()
    test_case_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = AppTestSuiteCase
        fields = ('id', 'test_case', 'test_case_id', 'order')

    def get_test_case(self, obj):
        tc = obj.test_case
        return {
            'id': tc.id,
            'name': tc.name,
            'description': tc.description,
            'app_package_name': tc.app_package.name if tc.app_package else '',
            'updated_at': tc.updated_at,
        }


class AppTestSuiteSerializer(serializers.ModelSerializer):
    """测试套件列表序列化器"""
    created_by_name = serializers.SerializerMethodField()
    test_case_count = serializers.SerializerMethodField()
    suite_cases = AppTestSuiteCaseSerializer(many=True, read_only=True)
    execution_status_display = serializers.CharField(
        source='get_execution_status_display', read_only=True
    )
    execution_result_display = serializers.CharField(
        source='get_execution_result_display', read_only=True, default=None
    )

    class Meta:
        model = AppTestSuite
        fields = (
            'id', 'name', 'description', 'project',
            'execution_status', 'execution_status_display',
            'execution_result', 'execution_result_display',
            'passed_count', 'failed_count', 'last_run_at',
            'test_case_count', 'suite_cases',
            'created_by', 'created_by_name',
            'created_at', 'updated_at',
        )
        read_only_fields = (
            'created_at', 'updated_at',
            'execution_status', 'execution_result',
            'passed_count', 'failed_count', 'last_run_at',
        )

    def get_created_by_name(self, obj) -> Optional[str]:
        return obj.created_by.username if obj.created_by else None

    def get_test_case_count(self, obj):
        return obj.suite_cases.count()


class AppTestSuiteCreateSerializer(serializers.ModelSerializer):
    """测试套件创建序列化器"""
    test_case_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        write_only=True,
        default=[]
    )

    class Meta:
        model = AppTestSuite
        fields = ('id', 'name', 'description', 'project', 'test_case_ids')
        read_only_fields = ('id',)

    def validate_project(self, value):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is not None and not accessible_projects(user).filter(pk=value.pk).exists():
            raise serializers.ValidationError('无权使用该 APP 项目')
        return value

    def create(self, validated_data):
        test_case_ids = validated_data.pop('test_case_ids', [])
        project = validated_data.get('project')
        user = getattr(self.context.get('request'), 'user', None)
        allowed_ids = set(scope_test_cases(
            AppTestCase.objects.filter(id__in=test_case_ids), user
        ).filter(project=project).values_list('id', flat=True))
        if set(test_case_ids) != allowed_ids:
            raise serializers.ValidationError('测试用例列表包含无权使用的用例')
        with transaction.atomic():
            suite = AppTestSuite.objects.create(**validated_data)
            AppTestSuiteCase.objects.bulk_create([
                AppTestSuiteCase(
                    test_suite=suite,
                    test_case_id=tc_id,
                    order=idx,
                )
                for idx, tc_id in enumerate(test_case_ids)
            ])
        return suite


class AppTestSuiteUpdateSerializer(serializers.ModelSerializer):
    """测试套件更新序列化器"""

    def validate_project(self, value):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is not None and not accessible_projects(user).filter(pk=value.pk).exists():
            raise serializers.ValidationError('无权使用该 APP 项目')
        return value

    def validate(self, attrs):
        project = attrs.get('project', getattr(self.instance, 'project', None))
        if project is not None and self.instance.suite_cases.exclude(
            test_case__project_id=project.id
        ).exists():
            raise serializers.ValidationError('套件中的所有测试用例必须属于同一项目')
        return attrs

    class Meta:
        model = AppTestSuite
        fields = ('name', 'description', 'project')


# ========== 定时任务序列化器 ==========

class AppScheduledTaskSerializer(serializers.ModelSerializer):
    """APP定时任务序列化器"""
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    device_name = serializers.SerializerMethodField()
    app_package_name = serializers.CharField(source='app_package.name', read_only=True, default='')
    test_suite_name = serializers.CharField(source='test_suite.name', read_only=True, default='')
    test_case_name = serializers.CharField(source='test_case.name', read_only=True, default='')
    task_type_display = serializers.CharField(source='get_task_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    trigger_type_display = serializers.CharField(source='get_trigger_type_display', read_only=True)
    notification_type_display = serializers.SerializerMethodField()

    def validate_project(self, value):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is not None and not accessible_projects(user).filter(pk=value.pk).exists():
            raise serializers.ValidationError('无权使用该 APP 项目')
        return value

    class Meta:
        model = AppScheduledTask
        fields = [
            'id', 'name', 'description', 'project',
            'task_type', 'task_type_display',
            'trigger_type', 'trigger_type_display',
            'cron_expression', 'interval_seconds', 'execute_at',
            'device', 'device_name',
            'app_package', 'app_package_name',
            'test_suite', 'test_suite_name',
            'test_case', 'test_case_name',
            'notify_on_success', 'notify_on_failure',
            'notification_type', 'notification_type_display', 'notify_emails',
            'status', 'status_display',
            'last_run_time', 'next_run_time',
            'total_runs', 'successful_runs', 'failed_runs',
            'last_result', 'error_message',
            'created_by', 'created_by_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'created_by', 'last_run_time', 'next_run_time',
            'total_runs', 'successful_runs', 'failed_runs',
            'last_result', 'error_message',
            'created_at', 'updated_at',
        ]

    def get_device_name(self, obj) -> str:
        if obj.device:
            return obj.device.name or obj.device.device_id
        return ''

    def get_notification_type_display(self, obj) -> str:
        return obj.get_notification_type_display() if obj.notification_type else '-'

    def validate(self, attrs):
        trigger_type = attrs.get('trigger_type', getattr(self.instance, 'trigger_type', None))
        if trigger_type == 'CRON' and not attrs.get('cron_expression', getattr(self.instance, 'cron_expression', '')):
            raise serializers.ValidationError('Cron表达式不能为空')
        if trigger_type == 'INTERVAL':
            interval_seconds = attrs.get('interval_seconds', getattr(self.instance, 'interval_seconds', None))
            if not interval_seconds:
                raise serializers.ValidationError('间隔秒数不能为空')
            if interval_seconds < 60:
                raise serializers.ValidationError('间隔秒数不能小于60秒')
        if trigger_type == 'ONCE':
            execute_at = attrs.get('execute_at', getattr(self.instance, 'execute_at', None))
            if not execute_at:
                raise serializers.ValidationError('执行时间不能为空')
            if execute_at <= timezone.now():
                raise serializers.ValidationError('执行时间必须大于当前时间')

        project = attrs.get('project', getattr(self.instance, 'project', None))
        device = attrs.get('device', getattr(self.instance, 'device', None))
        app_package = attrs.get('app_package', getattr(self.instance, 'app_package', None))
        test_suite = attrs.get('test_suite', getattr(self.instance, 'test_suite', None))
        test_case = attrs.get('test_case', getattr(self.instance, 'test_case', None))
        task_type = attrs.get('task_type', getattr(self.instance, 'task_type', None))
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        errors = scheduled_task_relation_errors(
            user=user,
            project=project,
            device=device,
            app_package=app_package,
            test_suite=test_suite,
            test_case=test_case,
            task_type=task_type,
        )
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        instance = super().create(validated_data)
        instance.next_run_time = instance.calculate_next_run()
        instance.save(update_fields=['next_run_time'])
        return instance

    def update(self, instance, validated_data):
        instance = super().update(instance, validated_data)
        instance.next_run_time = instance.calculate_next_run()
        instance.save(update_fields=['next_run_time'])
        return instance


class AppNotificationLogSerializer(serializers.ModelSerializer):
    """APP通知日志序列化器"""
    recipient_names = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    notification_type_display = serializers.CharField(source='get_notification_type_display', read_only=True)
    retry_status = serializers.SerializerMethodField()
    task_type_display = serializers.SerializerMethodField()
    actual_notification_type_display = serializers.SerializerMethodField()

    class Meta:
        model = AppNotificationLog
        fields = [
            'id', 'task', 'task_name',
            'notification_type', 'notification_type_display',
            'actual_notification_type_display', 'task_type_display',
            'sender_name', 'sender_email',
            'recipient_names', 'webhook_bot_info', 'notification_content',
            'status', 'status_display', 'error_message', 'response_info',
            'created_at', 'sent_at', 'retry_count', 'retry_status',
        ]
        read_only_fields = ['created_at', 'sent_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['webhook_bot_info'] = {'configured': bool(instance.webhook_bot_info)} if instance.webhook_bot_info else {}
        data['response_info'] = {'available': bool(instance.response_info)} if instance.response_info else {}
        return data

    def get_recipient_names(self, obj) -> str:
        return obj.get_recipient_names()

    def get_retry_status(self, obj) -> str:
        return obj.get_retry_status()

    def get_task_type_display(self, obj) -> str:
        if obj.task_type:
            choices = dict(AppScheduledTask.TASK_TYPE_CHOICES)
            return choices.get(obj.task_type, obj.task_type)
        return '未记录'

    def get_actual_notification_type_display(self, obj) -> str:
        if obj.webhook_bot_info:
            bot_type = obj.webhook_bot_info.get('type', '') or obj.webhook_bot_info.get('bot_type', '')
            type_map = {'wechat': '企微机器人', 'feishu': '飞书机器人', 'dingtalk': '钉钉机器人'}
            return type_map.get(bot_type, 'Webhook机器人')
        if obj.recipient_info and isinstance(obj.recipient_info, list) and len(obj.recipient_info) > 0:
            return '邮箱通知'
        return '-'
