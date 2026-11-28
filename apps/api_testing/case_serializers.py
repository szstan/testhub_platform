from django.db import transaction
from rest_framework import serializers

from apps.testcases.models import TestCase

from .models import (
    ApiCaseExecution,
    ApiCollection,
    ApiProject,
    ApiRequest,
    ApiTestCase,
    ApiTestCaseStep,
    Environment,
    TestSuiteCase,
)
from .serializers import UserSerializer
from .utils import can_access_api_project, can_use_environment

SUPPORTED_ASSERTIONS = {'status_code', 'response_time', 'contains', 'json_path', 'header', 'equals'}


def can_access_testcase_project(project, user):
    if project is None or not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return True
    return project.owner_id == user.pk or project.members.filter(pk=user.pk).exists()


def _lock_and_validate_step_requests(request_ids, project):
    request_ids = sorted(set(request_ids))
    if not request_ids:
        return {}

    locations = dict(ApiRequest.objects.filter(pk__in=request_ids).values_list('pk', 'collection_id'))
    if len(locations) != len(request_ids):
        raise serializers.ValidationError({'steps': '步骤请求已删除，请刷新后重试'})

    collection_ids = sorted({collection_id for collection_id in locations.values() if collection_id})
    collections = {
        collection.pk: collection
        for collection in ApiCollection.objects.select_for_update().filter(
            pk__in=collection_ids,
        ).order_by('pk')
    }
    requests = {
        request.pk: request
        for request in ApiRequest.objects.select_for_update().filter(
            pk__in=request_ids,
        ).order_by('pk')
    }
    if len(requests) != len(request_ids):
        raise serializers.ValidationError({'steps': '步骤请求已删除，请刷新后重试'})

    for request_id, api_request in requests.items():
        if api_request.collection_id != locations[request_id]:
            raise serializers.ValidationError({'steps': '步骤请求所属集合已变更，请刷新后重试'})
        collection = collections.get(api_request.collection_id)
        if (
            api_request.request_type != 'HTTP'
            or collection is None
            or collection.project_id != project.pk
        ):
            raise serializers.ValidationError({
                'steps': '步骤请求必须是同一API项目中的HTTP请求',
            })
    return requests


class ApiTestCaseStepSerializer(serializers.ModelSerializer):
    order = serializers.IntegerField(min_value=0, required=False)
    request_name = serializers.CharField(source='request.name', read_only=True)
    request_method = serializers.CharField(source='request.method', read_only=True)

    class Meta:
        model = ApiTestCaseStep
        fields = [
            'id', 'name', 'order', 'enabled', 'request', 'request_name', 'request_method',
            'request_overrides', 'assertions', 'extractors',
        ]
        read_only_fields = ['id', 'request_name', 'request_method']

    def validate_request_overrides(self, value):
        if not isinstance(value, dict) or set(value) - {'params', 'headers', 'body'}:
            raise serializers.ValidationError('仅支持 params、headers、body 覆盖')
        for key in ('params', 'headers'):
            if key in value and not isinstance(value[key], dict):
                raise serializers.ValidationError({key: '必须是JSON对象'})
        body = value.get('body')
        allowed_body_types = {'json', 'raw', 'form-data', 'x-www-form-urlencoded', 'none'}
        if isinstance(body, dict) and 'type' in body and body.get('type') not in allowed_body_types:
            raise serializers.ValidationError({'body': '请求体类型无效'})
        return value

    def validate_assertions(self, value):
        if value is None:
            return None
        if not isinstance(value, list):
            raise serializers.ValidationError('断言必须是数组或 null')
        for index, item in enumerate(value):
            if not isinstance(item, dict) or item.get('type') not in SUPPORTED_ASSERTIONS:
                raise serializers.ValidationError({index: '断言类型无效'})
            assertion_type = item['type']
            if assertion_type == 'json_path' and not item.get('json_path'):
                raise serializers.ValidationError({index: 'json_path不能为空'})
            if assertion_type == 'header' and not item.get('header_name'):
                raise serializers.ValidationError({index: 'header_name不能为空'})
            if assertion_type == 'status_code' and 'expected' not in item and 'value' not in item:
                raise serializers.ValidationError({index: 'status_code断言需要expected或value'})
            if assertion_type != 'status_code' and 'expected' not in item and not (
                assertion_type == 'header' and 'expected_value' in item
            ):
                raise serializers.ValidationError({index: '断言需要expected'})
            if assertion_type == 'json_path':
                if item.get('comparison', 'json') not in {'json', 'legacy'}:
                    raise serializers.ValidationError({index: 'comparison只支持json或legacy'})
                item.setdefault('comparison', 'json')
        return value

    def validate_extractors(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('提取器必须是数组')
        seen = set()
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise serializers.ValidationError({index: '提取器必须是对象'})
            name = item.get('name')
            source = item.get('source')
            expression = item.get('expression')
            if not isinstance(name, str) or not name.strip() or name in seen:
                raise serializers.ValidationError({index: '提取变量名不能为空且不能重复'})
            if source not in {'json_path', 'header'}:
                raise serializers.ValidationError({index: 'source只支持json_path或header'})
            if not isinstance(expression, str) or not expression.strip():
                raise serializers.ValidationError({index: 'expression不能为空'})
            if 'required' in item and not isinstance(item['required'], bool):
                raise serializers.ValidationError({index: 'required必须是布尔值'})
            seen.add(name)
        return value


class ApiTestCaseSerializer(serializers.ModelSerializer):
    steps = ApiTestCaseStepSerializer(many=True, required=False)
    execution_fingerprint = serializers.SerializerMethodField()
    project = serializers.PrimaryKeyRelatedField(queryset=ApiProject.objects.all())
    environment = serializers.PrimaryKeyRelatedField(
        queryset=Environment.objects.all(), required=False, allow_null=True,
    )
    linked_testcase = serializers.PrimaryKeyRelatedField(
        queryset=TestCase.objects.select_related('project').all(), required=False, allow_null=True,
    )
    created_by = UserSerializer(read_only=True)

    class Meta:
        model = ApiTestCase
        fields = [
            'id', 'project', 'name', 'description', 'preconditions', 'expected_result',
            'priority', 'status', 'tags', 'variables', 'environment', 'stop_on_failure',
            'linked_testcase', 'steps', 'execution_fingerprint', 'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'execution_fingerprint', 'created_by', 'created_at', 'updated_at']

    def get_execution_fingerprint(self, obj):
        from .case_execution_snapshot import api_case_execution_fingerprint

        steps = getattr(obj, '_prefetched_objects_cache', {}).get('steps')
        if steps is None:
            steps = obj.steps.select_related('request__collection').order_by('order', 'pk')
        return api_case_execution_fingerprint(
            obj,
            environment=obj.environment,
            steps=steps,
        )

    def validate_tags(self, value):
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise serializers.ValidationError('tags必须是字符串数组')
        return value

    def validate_variables(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('variables必须是JSON对象')
        return value

    def validate_steps(self, value):
        if len(value) > 50:
            raise serializers.ValidationError('单个用例最多包含50个步骤')
        orders = [step.get('order', index) for index, step in enumerate(value)]
        if len(set(orders)) != len(orders):
            raise serializers.ValidationError('步骤order不能重复')
        return value

    def validate(self, attrs):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        project = attrs.get('project', getattr(self.instance, 'project', None))
        environment = attrs.get('environment', getattr(self.instance, 'environment', None))
        linked_testcase = attrs.get('linked_testcase', getattr(self.instance, 'linked_testcase', None))
        if not can_access_api_project(project, user):
            raise serializers.ValidationError({'project': '无权使用目标项目'})
        if environment and not can_use_environment(environment, user, project=project):
            raise serializers.ValidationError({'environment': '无权使用该执行环境'})
        if linked_testcase and not can_access_testcase_project(linked_testcase.project, user):
            raise serializers.ValidationError({'linked_testcase': '无权访问关联的通用测试用例'})

        if self.instance and self.instance.executions.filter(status__in=['PENDING', 'RUNNING']).exists():
            raise serializers.ValidationError({'detail': '用例有正在排队或执行的任务，暂不能编辑'})
        if self.instance and self.instance.suite_memberships.filter(
            test_suite__executions__status='RUNNING',
        ).exists():
            raise serializers.ValidationError({'detail': '用例正在某个测试套件中运行，暂不能编辑'})
        steps = attrs.get('steps')
        if steps is not None:
            for index, step in enumerate(steps):
                api_request = step['request']
                collection = api_request.collection
                if (
                    api_request.request_type != 'HTTP'
                    or collection is None
                    or collection.project_id != project.pk
                ):
                    raise serializers.ValidationError({
                        'steps': {index: {'request': '步骤请求必须是同一API项目中的HTTP请求'}}
                    })
        if self.instance and project.pk != self.instance.project_id:
            if self.instance.suite_memberships.exists():
                raise serializers.ValidationError({'project': '已加入测试套件的用例不能跨项目迁移'})
            if self.instance.executions.filter(status__in=['PENDING', 'RUNNING']).exists():
                raise serializers.ValidationError({'project': '运行中的用例不能迁移项目'})
            if steps is None and self.instance.steps.exclude(request__collection__project_id=project.pk).exists():
                raise serializers.ValidationError({'project': '所有步骤请求必须属于目标API项目'})
        return attrs

    @staticmethod
    def _write_steps(instance, steps):
        if steps is None:
            return
        instance.steps.all().delete()
        for index, step in enumerate(steps):
            ApiTestCaseStep.objects.create(
                test_case=instance,
                order=step.get('order', index),
                **{key: value for key, value in step.items() if key != 'order'},
            )

    @transaction.atomic
    def create(self, validated_data):
        steps = validated_data.pop('steps', [])
        locked_requests = _lock_and_validate_step_requests(
            [step['request'].pk for step in steps], validated_data['project'],
        )
        for step in steps:
            step['request'] = locked_requests[step['request'].pk]
        request = self.context.get('request')
        validated_data.setdefault('created_by', getattr(request, 'user', None))
        instance = ApiTestCase.objects.create(**validated_data)
        self._write_steps(instance, steps)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        steps = validated_data.pop('steps', None)
        instance = ApiTestCase.objects.select_for_update().get(pk=instance.pk)
        if instance.executions.filter(status__in=['PENDING', 'RUNNING']).exists():
            raise serializers.ValidationError('用例有正在排队或执行的任务，暂不能编辑')
        if instance.suite_memberships.filter(test_suite__executions__status='RUNNING').exists():
            raise serializers.ValidationError('用例正在某个测试套件中运行，暂不能编辑')
        new_project = validated_data.get('project', instance.project)
        request_ids = None
        if steps is not None:
            request_ids = [step['request'].pk for step in steps]
        elif new_project.pk != instance.project_id:
            request_ids = list(instance.steps.values_list('request_id', flat=True))
        if request_ids is not None:
            locked_requests = _lock_and_validate_step_requests(request_ids, new_project)
            if steps is not None:
                for step in steps:
                    step['request'] = locked_requests[step['request'].pk]
        if new_project.pk != instance.project_id:
            if instance.suite_memberships.exists():
                raise serializers.ValidationError({'project': '已加入测试套件的用例不能跨项目迁移'})
            if steps is None and instance.steps.exclude(
                request__collection__project_id=new_project.pk,
            ).exists():
                raise serializers.ValidationError({'project': '所有步骤请求必须属于目标API项目'})
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()
        self._write_steps(instance, steps)
        return instance


class TestSuiteCaseSerializer(serializers.ModelSerializer):
    test_case = ApiTestCaseSerializer(read_only=True)

    class Meta:
        model = TestSuiteCase
        fields = ['id', 'test_case', 'order', 'enabled']


class ApiCaseExecutionSerializer(serializers.ModelSerializer):
    environment_name = serializers.CharField(source='environment.name', read_only=True, default=None)
    executed_by = UserSerializer(read_only=True)

    class Meta:
        model = ApiCaseExecution
        fields = [
            'id', 'project', 'test_case', 'test_case_name', 'environment', 'environment_name',
            'executed_by', 'status', 'start_time', 'end_time', 'duration', 'total_steps',
            'passed_steps', 'failed_steps', 'skipped_steps', 'results', 'error_message', 'created_at',
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        from apps.quality_intelligence.services.redaction import redact_payload
        data['results'] = redact_payload(data.get('results'))
        data['error_message'] = redact_payload(data.get('error_message', ''))
        return data
