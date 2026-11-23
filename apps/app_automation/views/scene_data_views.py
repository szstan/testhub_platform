"""Project-scoped immutable scene datasets and reusable component versions."""
from __future__ import annotations

import json
import re

from django.db import IntegrityError, transaction
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..access import accessible_projects, scope_project
from ..component_templates import ComponentTemplateError, validate_steps
from ..dataset_utils import DEFAULT_LIMITS, DatasetError, parse_dataset
from ..models import AppComponent, AppCustomComponent, AppDataSet, AppProjectComponent
from ..scene_component_runtime import (
    SceneComponentError,
    validate_component_definition_sensitive_data,
)
from .test_case_views import AppPagination

_COMPONENT_TYPE = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,49}\Z')
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z')
_SECRET_REFERENCE = re.compile(r'secret://[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_VARIABLE_REFERENCE = re.compile(
    r'(?:\{\{\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*\s*\}\}'
    r'|\$\{\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*\s*\})\Z'
)
_SENSITIVE_COLUMN = re.compile(r'password|passwd|secret|token|credential|api[_-]?key|密码|密钥|令牌', re.I)
_NESTED_STEP_LIST_FIELDS = ('steps', 'then_steps', 'else_steps', 'try_steps', 'catch_steps', 'finally_steps')


def _safe_sensitive_value(value):
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return not value.strip() or bool(_VARIABLE_REFERENCE.fullmatch(value))


def _has_unsafe_sensitive_step_value(steps):
    def visit(step_list):
        for step in step_list:
            config = step.get('config')
            action = str(step.get('type', '')).lower()
            containers = [step]
            if isinstance(config, dict):
                containers.append(config)
            if (action in {'input', 'text'} and
                    any(container.get('sensitive') is True for container in containers)):
                for container in containers:
                    for key in ('value', 'text'):
                        if key in container and not _safe_sensitive_value(container[key]):
                            return True

            nested_containers = [step]
            if step.get('kind') != 'custom' and isinstance(config, dict):
                nested_containers.append(config)
            for container in nested_containers:
                for field in _NESTED_STEP_LIST_FIELDS:
                    nested = container.get(field)
                    if isinstance(nested, list) and visit(nested):
                        return True
        return False

    return visit(steps)


def _valid_id(value):
    text = str(value)
    return (type(value) in (str, int) and text.isascii() and text.isdigit()
            and len(text) <= 18 and int(text) > 0)


class _ProjectScopedSerializer(serializers.ModelSerializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')
        self.fields['project'].queryset = accessible_projects(getattr(request, 'user', None))


class AppProjectComponentSerializer(_ProjectScopedSerializer):
    class Meta:
        model = AppProjectComponent
        fields = ('id', 'project', 'name', 'type', 'version', 'description', 'schema',
                  'default_config', 'steps', 'created_by', 'created_at')
        read_only_fields = ('id', 'created_by', 'created_at')
        validators = []

    def validate_type(self, value):
        if not _COMPONENT_TYPE.fullmatch(value):
            raise serializers.ValidationError('组件类型必须以字母或下划线开头，且只包含字母、数字和下划线')
        if AppComponent.objects.filter(type=value).exists() or AppCustomComponent.objects.filter(type=value).exists():
            raise serializers.ValidationError('组件类型与全局组件冲突，请使用其他类型')
        return value

    def validate_version(self, value):
        if not _VERSION.fullmatch(value):
            raise serializers.ValidationError('版本格式无效')
        return value

    def validate(self, attrs):
        schema = attrs.get('schema', {})
        defaults = attrs.get('default_config', {})
        steps = attrs.get('steps', [])
        if not isinstance(schema, dict) or not isinstance(defaults, dict):
            raise serializers.ValidationError('组件参数定义和默认值必须是 JSON 对象')
        properties = schema.get('properties', {})
        if not isinstance(properties, dict):
            raise serializers.ValidationError('组件参数 properties 必须是 JSON 对象')
        required = schema.get('required', [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise serializers.ValidationError('组件必填参数必须是名称列表')
        if any(name not in properties for name in required):
            raise serializers.ValidationError('组件必填参数必须在 properties 中定义')
        try:
            validate_steps(steps)
            validate_component_definition_sensitive_data(schema, defaults, steps)
            if _has_unsafe_sensitive_step_value(steps):
                raise serializers.ValidationError('敏感参数和敏感输入只能留空或使用变量引用')
            payload = json.dumps({'schema': schema, 'default_config': defaults, 'steps': steps},
                                 ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ComponentTemplateError, SceneComponentError, TypeError, ValueError,
                RecursionError, UnicodeEncodeError):
            raise serializers.ValidationError('组件定义或嵌套步骤无效') from None
        if len(payload) > 1_048_576:
            raise serializers.ValidationError('组件定义不能超过 1 MiB')
        if not steps:
            raise serializers.ValidationError('可复用组件至少需要一个步骤')
        project = attrs.get('project')
        if project and AppProjectComponent.objects.filter(
            project=project, type=attrs.get('type'), version=attrs.get('version')
        ).exists():
            raise serializers.ValidationError('该项目中已存在相同组件版本')
        return attrs


class AppDataSetSerializer(serializers.ModelSerializer):
    row_count = serializers.SerializerMethodField()

    class Meta:
        model = AppDataSet
        fields = ('id', 'project', 'name', 'version', 'columns', 'rows', 'sensitive_columns',
                  'row_count', 'created_by', 'created_at')
        read_only_fields = fields

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        view = self.context.get('view')
        if getattr(view, 'action', None) == 'list':
            self.fields.pop('rows', None)

    def get_row_count(self, obj):
        return len(obj.rows) if isinstance(obj.rows, list) else 0


class ProjectComponentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                              mixins.CreateModelMixin, viewsets.GenericViewSet):
    """A saved version is immutable; create a new version for later changes."""
    serializer_class = AppProjectComponentSerializer
    permission_classes = (IsAuthenticated,)
    pagination_class = AppPagination

    def get_queryset(self):
        queryset = scope_project(AppProjectComponent.objects.select_related('project'), self.request.user)
        project = self.request.query_params.get('project')
        if project is not None:
            queryset = queryset.filter(project_id=project) if _valid_id(project) else queryset.none()
        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def create(self, request, *args, **kwargs):
        try:
            with transaction.atomic():
                return super().create(request, *args, **kwargs)
        except IntegrityError:
            return Response({'detail': '该项目中已存在相同组件版本'}, status=status.HTTP_409_CONFLICT)


class ProjectDataSetViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                            viewsets.GenericViewSet):
    """Upload CSV/XLSX as an immutable version without resolving secret aliases."""
    serializer_class = AppDataSetSerializer
    permission_classes = (IsAuthenticated,)
    pagination_class = AppPagination

    def get_queryset(self):
        queryset = scope_project(AppDataSet.objects.select_related('project'), self.request.user)
        project = self.request.query_params.get('project')
        if project is not None:
            queryset = queryset.filter(project_id=project) if _valid_id(project) else queryset.none()
        return queryset

    def create(self, request, *args, **kwargs):
        project_id = request.data.get('project')
        if not _valid_id(project_id):
            return Response({'detail': '请选择有效的项目'}, status=status.HTTP_400_BAD_REQUEST)
        project = accessible_projects(request.user).filter(pk=project_id).first()
        if project is None:
            return Response({'detail': '项目不存在或无权访问'}, status=status.HTTP_403_FORBIDDEN)
        name = request.data.get('name', '')
        version = request.data.get('version', 1)
        upload = request.FILES.get('file')
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            return Response({'detail': '请输入不超过 100 个字符的数据集名称'}, status=status.HTTP_400_BAD_REQUEST)
        version_text = str(version)
        if (isinstance(version, bool) or not version_text.isascii() or not version_text.isdigit()
                or len(version_text) > 10 or not 1 <= int(version_text) <= 2_147_483_647):
            return Response({'detail': '数据集版本必须是正整数'}, status=status.HTTP_400_BAD_REQUEST)
        if upload is None or upload.size > DEFAULT_LIMITS.max_bytes:
            return Response({'detail': '请选择不超过 5 MiB 的 CSV 或 XLSX 文件'}, status=status.HTTP_400_BAD_REQUEST)
        raw_sensitive = request.data.get('sensitive_columns', [])
        if isinstance(raw_sensitive, str):
            try:
                raw_sensitive = json.loads(raw_sensitive)
            except (TypeError, ValueError):
                return Response({'detail': '敏感列配置无效'}, status=status.HTTP_400_BAD_REQUEST)
        if (not isinstance(raw_sensitive, list) or len(raw_sensitive) > DEFAULT_LIMITS.max_columns or
                any(not isinstance(column, str) for column in raw_sensitive)):
            return Response({'detail': '敏感列配置无效'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            rows = parse_dataset(upload.read(DEFAULT_LIMITS.max_bytes + 1), upload.name,
                                 sheet_name=request.data.get('sheet_name') or None)
        except DatasetError:
            return Response({'detail': '数据文件格式无效或超过允许的大小、行数和列数'}, status=status.HTTP_400_BAD_REQUEST)
        if not rows:
            return Response({'detail': '数据文件至少需要一行数据'}, status=status.HTTP_400_BAD_REQUEST)
        columns = list(rows[0])
        if any(column not in columns for column in raw_sensitive):
            return Response({'detail': '敏感列必须存在于数据表头中'}, status=status.HTTP_400_BAD_REQUEST)
        sensitive = set(raw_sensitive) | {column for column in columns if _SENSITIVE_COLUMN.search(column)}
        for row in rows:
            for column in sensitive:
                value = row.get(column)
                if value not in ('', None) and (not isinstance(value, str) or not _SECRET_REFERENCE.fullmatch(value)):
                    return Response({'detail': '敏感列只能使用服务端配置的 secret://别名'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            with transaction.atomic():
                dataset = AppDataSet.objects.create(
                    project=project, name=name.strip(), version=int(version), columns=columns,
                    rows=rows, sensitive_columns=[column for column in columns if column in sensitive],
                    created_by=request.user,
                )
        except IntegrityError:
            return Response({'detail': '该项目中已存在相同数据集版本'}, status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(dataset).data, status=status.HTTP_201_CREATED)
