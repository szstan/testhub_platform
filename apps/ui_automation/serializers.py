from rest_framework import serializers
from django.utils import timezone
from .models import (
    UiProject, LocatorStrategy, Element, TestScript, TestSuite,
    TestSuiteScript, TestSuiteTestCase, TestExecution, TestEnvironment, Screenshot,
    ElementGroup, PageObject, PageObjectElement, ScriptStep, ScriptElementUsage,
    TestCase, TestCaseStep, TestCaseExecution, OperationRecord,
    UiScheduledTask, UiNotificationLog, UiTaskNotificationSetting,
    AICase, AIExecutionRecord
)
from django.contrib.auth import get_user_model
from django.conf import settings
from .access import project_accessible, is_ui_admin, selenium_compatibility_enabled

from apps.core.network import UnsafeOutboundURL, validate_outbound_url


def _serializer_user(serializer):
    request = serializer.context.get('request')
    return getattr(request, 'user', None)


def _ensure_project_access(serializer, project, field='project'):
    user = _serializer_user(serializer)
    if project is None:
        raise serializers.ValidationError({field: '请选择有效的项目'})
    if user is not None and getattr(user, 'is_authenticated', False) and not project_accessible(user, project):
        raise serializers.ValidationError({field: '无权使用该 UI 项目'})
    return project


def _load_object(model, value, field):
    try:
        return model.objects.get(pk=value)
    except (model.DoesNotExist, TypeError, ValueError):
        raise serializers.ValidationError({field: '关联对象不存在'})


def _same_project_error(left, right, field):
    left_project_id = getattr(left, 'project_id', getattr(left, 'pk', None)) if left is not None else None
    right_project_id = getattr(right, 'project_id', getattr(right, 'pk', None)) if right is not None else None
    if left is not None and right is not None and left_project_id != right_project_id:
        raise serializers.ValidationError({field: '关联对象必须属于同一项目'})


User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'username', 'email')


class UiProjectSerializer(serializers.ModelSerializer):
    owner = UserSerializer(read_only=True)
    members = UserSerializer(many=True, read_only=True)

    class Meta:
        model = UiProject
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')


class UiProjectCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = UiProject
        fields = ('name', 'description', 'status', 'base_url', 'start_date', 'end_date', 'owner', 'members')

    def validate_base_url(self, value):
        try:
            return validate_outbound_url(
                value,
                allow_private=bool(getattr(settings, 'UI_ALLOW_PRIVATE_TARGETS',
                                           getattr(settings, 'PERF_ALLOW_PRIVATE_TARGETS', False))),
            )
        except UnsafeOutboundURL as exc:
            raise serializers.ValidationError(str(exc)) from exc


class UiProjectUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = UiProject
        fields = ('name', 'description', 'status', 'base_url', 'start_date', 'end_date', 'members')

    def validate_base_url(self, value):
        try:
            return validate_outbound_url(
                value,
                allow_private=bool(getattr(settings, 'UI_ALLOW_PRIVATE_TARGETS',
                                           getattr(settings, 'PERF_ALLOW_PRIVATE_TARGETS', False))),
            )
        except UnsafeOutboundURL as exc:
            raise serializers.ValidationError(str(exc)) from exc


class LocatorStrategySerializer(serializers.ModelSerializer):
    class Meta:
        model = LocatorStrategy
        fields = '__all__'


class ElementSerializer(serializers.ModelSerializer):
    project = UiProjectSerializer(read_only=True)
    locator_strategy = LocatorStrategySerializer(read_only=True)
    created_by = UserSerializer(read_only=True)
    project_id = serializers.IntegerField(write_only=True)
    group_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    parent_element_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    locator_strategy_id = serializers.IntegerField()  # 显式定义，支持读写

    class Meta:
        model = Element
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'created_by')

    def validate_project_id(self, value):
        project = _load_object(UiProject, value, 'project_id')
        _ensure_project_access(self, project, 'project_id')
        return value

    def validate_locator_strategy_id(self, value):
        if value is not None:
            _load_object(LocatorStrategy, value, 'locator_strategy_id')
        return value

    def validate_group_id(self, value):
        if value is not None:
            _load_object(ElementGroup, value, 'group_id')
        return value

    def validate(self, attrs):
        project = _load_object(UiProject, attrs['project_id'], 'project_id') if 'project_id' in attrs else getattr(self.instance, 'project', None)
        _ensure_project_access(self, project, 'project_id')
        group_id = attrs.get('group_id')
        if group_id is not None:
            group = _load_object(ElementGroup, group_id, 'group_id')
            _same_project_error(project, group, 'group_id')
        parent_id = attrs.get('parent_element_id')
        if parent_id is not None:
            parent = _load_object(Element, parent_id, 'parent_element_id')
            _same_project_error(project, parent, 'parent_element_id')
        return attrs

    def create(self, validated_data):
        # 处理外键字段
        project_id = validated_data.pop('project_id')
        locator_strategy_id = validated_data.pop('locator_strategy_id', None)
        group_id = validated_data.pop('group_id', None)
        parent_element_id = validated_data.pop('parent_element_id', None)

        validated_data['project'] = UiProject.objects.get(id=project_id)

        if locator_strategy_id:
            validated_data['locator_strategy'] = LocatorStrategy.objects.get(id=locator_strategy_id)

        if group_id:
            validated_data['group'] = ElementGroup.objects.get(id=group_id)

        if parent_element_id:
            validated_data['parent_element'] = Element.objects.get(id=parent_element_id)

        return super().create(validated_data)

    def update(self, instance, validated_data):
        # 处理外键字段
        project_id = validated_data.pop('project_id', None)
        locator_strategy_id = validated_data.pop('locator_strategy_id', None)
        group_id = validated_data.pop('group_id', None)
        parent_element_id = validated_data.pop('parent_element_id', None)

        if project_id:
            validated_data['project'] = UiProject.objects.get(id=project_id)

        if locator_strategy_id:
            validated_data['locator_strategy'] = LocatorStrategy.objects.get(id=locator_strategy_id)

        if group_id is not None:  # 允许设置为None来清除分组
            if group_id:
                validated_data['group'] = ElementGroup.objects.get(id=group_id)
            else:
                validated_data['group'] = None

        if parent_element_id is not None:
            validated_data['parent_element'] = (
                Element.objects.get(id=parent_element_id) if parent_element_id else None
            )

        return super().update(instance, validated_data)


class TestScriptSerializer(serializers.ModelSerializer):
    project = UiProjectSerializer(read_only=True)
    project_id = serializers.IntegerField(write_only=True)

    def validate_project_id(self, value):
        project = _load_object(UiProject, value, 'project_id')
        _ensure_project_access(self, project, 'project_id')
        return value

    class Meta:
        model = TestScript
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')


class TestScriptCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestScript
        fields = ('project', 'name', 'description', 'script_type', 'content', 'language', 'framework')

    def validate_project(self, value):
        return _ensure_project_access(self, value)


class TestScriptUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestScript
        fields = ('name', 'description', 'script_type', 'content')


class TestSuiteScriptSerializer(serializers.ModelSerializer):
    test_script = TestScriptSerializer(read_only=True)
    test_script_id = serializers.IntegerField(write_only=True)
    test_suite_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = TestSuiteScript
        fields = ('id', 'test_script', 'test_script_id', 'test_suite_id', 'order')

    def validate(self, attrs):
        script = _load_object(TestScript, attrs.get('test_script_id'), 'test_script_id')
        suite_id = attrs.get('test_suite_id')
        suite = _load_object(TestSuite, suite_id, 'test_suite_id') if suite_id else None
        if suite is not None:
            _ensure_project_access(self, suite.project)
            _same_project_error(suite, script, 'test_script_id')
        return attrs

    def create(self, validated_data):
        suite_id = validated_data.pop('test_suite_id', None)
        script_id = validated_data.pop('test_script_id')
        if suite_id is not None:
            validated_data['test_suite'] = _load_object(TestSuite, suite_id, 'test_suite_id')
        validated_data['test_script'] = _load_object(TestScript, script_id, 'test_script_id')
        return super().create(validated_data)


class TestSuiteTestCaseSerializer(serializers.ModelSerializer):
    test_case = serializers.SerializerMethodField()
    test_case_id = serializers.IntegerField(write_only=True)
    test_suite_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = TestSuiteTestCase
        fields = ('id', 'test_case', 'test_case_id', 'test_suite_id', 'order')

    def validate(self, attrs):
        test_case = _load_object(TestCase, attrs.get('test_case_id'), 'test_case_id')
        suite_id = attrs.get('test_suite_id')
        suite = _load_object(TestSuite, suite_id, 'test_suite_id') if suite_id else None
        if suite is not None:
            _ensure_project_access(self, suite.project)
            _same_project_error(suite, test_case, 'test_case_id')
        return attrs

    def create(self, validated_data):
        suite_id = validated_data.pop('test_suite_id', None)
        case_id = validated_data.pop('test_case_id')
        if suite_id is not None:
            validated_data['test_suite'] = _load_object(TestSuite, suite_id, 'test_suite_id')
        validated_data['test_case'] = _load_object(TestCase, case_id, 'test_case_id')
        return super().create(validated_data)

    def get_test_case(self, obj):
        """获取测试用例信息"""
        test_case = obj.test_case
        return {
            'id': test_case.id,
            'name': test_case.name,
            'description': test_case.description,
            'status': test_case.status,
            'priority': test_case.priority,
            'created_at': test_case.created_at
        }


class TestSuiteSerializer(serializers.ModelSerializer):
    project = UiProjectSerializer(read_only=True)
    scripts = TestScriptSerializer(many=True, read_only=True)
    test_cases_data = serializers.SerializerMethodField()
    project_id = serializers.IntegerField(write_only=True)
    suite_scripts = TestSuiteScriptSerializer(many=True, read_only=True)
    suite_test_cases = TestSuiteTestCaseSerializer(many=True, read_only=True)
    test_case_count = serializers.SerializerMethodField()

    class Meta:
        model = TestSuite
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'execution_status', 'passed_count', 'failed_count')

    def validate_project_id(self, value):
        project = _load_object(UiProject, value, 'project_id')
        _ensure_project_access(self, project, 'project_id')
        return value

    def get_test_cases_data(self, obj):
        """获取测试用例数据"""
        return TestSuiteTestCaseSerializer(obj.suite_test_cases.all(), many=True).data

    def get_test_case_count(self, obj):
        """获取测试用例数量"""
        return obj.suite_test_cases.count()


class TestSuiteCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestSuite
        fields = ('id', 'project', 'name', 'description')
        read_only_fields = ('id',)

    def validate_project(self, value):
        return _ensure_project_access(self, value)


class TestSuiteUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestSuite
        fields = ('name', 'description')


class TestSuiteWithScriptsSerializer(serializers.ModelSerializer):
    project = UiProjectSerializer(read_only=True)
    suite_scripts = TestSuiteScriptSerializer(many=True, read_only=True)

    class Meta:
        model = TestSuite
        fields = '__all__'


class TestExecutionSerializer(serializers.ModelSerializer):
    project = UiProjectSerializer(read_only=True)
    test_suite = TestSuiteSerializer(read_only=True)
    test_script = TestScriptSerializer(read_only=True)
    executed_by = UserSerializer(read_only=True)
    project_id = serializers.IntegerField(write_only=True)
    test_suite_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    test_script_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    executed_by_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)

    # 添加计算字段
    test_suite_name = serializers.SerializerMethodField()
    executed_by_name = serializers.SerializerMethodField()
    pass_rate = serializers.SerializerMethodField()

    class Meta:
        model = TestExecution
        fields = '__all__'
        read_only_fields = (
            'created_at', 'started_at', 'finished_at', 'duration',
            'total_cases', 'passed_cases', 'failed_cases', 'skipped_cases'
        )

    def validate(self, attrs):
        project_id = attrs.pop('project_id', None)
        if project_id is not None:
            project = _load_object(UiProject, project_id, 'project_id')
        else:
            project = getattr(self.instance, 'project', None)
        _ensure_project_access(self, project)

        suite_id = attrs.pop('test_suite_id', None) if 'test_suite_id' in attrs else None
        suite = _load_object(TestSuite, suite_id, 'test_suite_id') if suite_id is not None else getattr(self.instance, 'test_suite', None)
        script_id = attrs.pop('test_script_id', None) if 'test_script_id' in attrs else None
        script = _load_object(TestScript, script_id, 'test_script_id') if script_id is not None else getattr(self.instance, 'test_script', None)
        if suite is not None:
            _same_project_error(project, suite, 'test_suite_id')
            attrs['test_suite'] = suite
        if script is not None:
            _same_project_error(project, script, 'test_script_id')
            attrs['test_script'] = script

        if 'executed_by_id' in attrs:
            executed_by_id = attrs.pop('executed_by_id')
            executed_by = _load_object(User, executed_by_id, 'executed_by_id') if executed_by_id is not None else None
            user = _serializer_user(self)
            if user is not None and getattr(user, 'is_authenticated', False) and not is_ui_admin(user) and executed_by is not None and executed_by.pk != user.pk:
                raise serializers.ValidationError({'executed_by_id': '只能以当前用户身份创建执行记录'})
            attrs['executed_by'] = executed_by
        elif self.instance is None:
            user = _serializer_user(self)
            if user is not None and getattr(user, 'is_authenticated', False):
                attrs['executed_by'] = user
        attrs['project'] = project
        return attrs

    def get_test_suite_name(self, obj):
        """获取测试套件名称"""
        return obj.test_suite.name if obj.test_suite else '-'
    def get_executed_by_name(self, obj):
        """获取执行人姓名"""
        return obj.executed_by.username if obj.executed_by else '-'

    def get_pass_rate(self, obj):
        """获取通过率"""
        return obj.pass_rate


class TestExecutionCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestExecution
        fields = ('project', 'test_suite', 'test_script', 'environment', 'executed_by')

    def validate(self, attrs):
        project = _ensure_project_access(self, attrs.get('project'))
        suite = attrs.get('test_suite')
        script = attrs.get('test_script')
        if suite is not None:
            _same_project_error(project, suite, 'test_suite')
        if script is not None:
            _same_project_error(project, script, 'test_script')
        user = _serializer_user(self)
        executed_by = attrs.get('executed_by')
        if executed_by is not None and user is not None and getattr(user, 'is_authenticated', False) and not is_ui_admin(user) and executed_by.pk != user.pk:
            raise serializers.ValidationError({'executed_by': '只能以当前用户身份创建执行记录'})
        if executed_by is None and user is not None and getattr(user, 'is_authenticated', False):
            attrs['executed_by'] = user
        return attrs


class ScreenshotSerializer(serializers.ModelSerializer):
    execution = TestExecutionSerializer(read_only=True)
    execution_id = serializers.IntegerField(write_only=True)

    class Meta:
        model = Screenshot
        fields = '__all__'
        read_only_fields = ('created_at', 'captured_at')

    def validate_execution_id(self, value):
        execution = _load_object(TestExecution, value, 'execution_id')
        _ensure_project_access(self, execution.project, 'execution_id')
        return value

    def create(self, validated_data):
        execution_id = validated_data.pop('execution_id')
        validated_data['execution'] = _load_object(TestExecution, execution_id, 'execution_id')
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if 'execution_id' in validated_data:
            execution_id = validated_data.pop('execution_id')
            validated_data['execution'] = _load_object(TestExecution, execution_id, 'execution_id')
        return super().update(instance, validated_data)


# 新增的serializers
class ElementGroupSerializer(serializers.ModelSerializer):
    project = UiProjectSerializer(read_only=True)
    project_id = serializers.IntegerField(write_only=True)
    elements_count = serializers.SerializerMethodField()
    children = serializers.SerializerMethodField()

    class Meta:
        model = ElementGroup
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    def validate_project_id(self, value):
        project = _load_object(UiProject, value, 'project_id')
        _ensure_project_access(self, project, 'project_id')
        return value

    def validate(self, attrs):
        project_id = attrs.get('project_id')
        project = _load_object(UiProject, project_id, 'project_id') if project_id is not None else getattr(self.instance, 'project', None)
        _ensure_project_access(self, project, 'project_id')
        parent_id = attrs.get('parent_group')
        if parent_id is not None:
            parent = parent_id if isinstance(parent_id, ElementGroup) else _load_object(ElementGroup, parent_id, 'parent_group')
            _same_project_error(project, parent, 'parent_group')
        return attrs

    def create(self, validated_data):
        project_id = validated_data.pop('project_id')
        validated_data['project'] = _load_object(UiProject, project_id, 'project_id')
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if 'project_id' in validated_data:
            project_id = validated_data.pop('project_id')
            validated_data['project'] = _load_object(UiProject, project_id, 'project_id')
        return super().update(instance, validated_data)

    def get_elements_count(self, obj):
        """获取分组下的元素数量"""
        return obj.elements.count()
    def get_children(self, obj):
        """获取子分组"""
        children = obj.elementgroup_set.all()
        return ElementGroupSerializer(children, many=True, context=self.context).data


class ElementGroupCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ElementGroup
        fields = ('project', 'name', 'description', 'parent_group', 'order')

    def validate(self, attrs):
        project = _ensure_project_access(self, attrs.get('project'))
        parent = attrs.get('parent_group')
        if parent is not None:
            _same_project_error(project, parent, 'parent_group')
        return attrs


class ElementEnhancedSerializer(serializers.ModelSerializer):
    """增强的元素序列化器，包含新字段"""
    project = UiProjectSerializer(read_only=True)
    group = ElementGroupSerializer(read_only=True)
    locator_strategy = LocatorStrategySerializer(read_only=True)
    created_by = UserSerializer(read_only=True)
    parent_element = serializers.SerializerMethodField()
    children_elements = serializers.SerializerMethodField()
    all_locators = serializers.SerializerMethodField()
    usage_scripts = serializers.SerializerMethodField()

    # Write-only fields for foreign keys
    project_id = serializers.IntegerField(write_only=True)
    group_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    locator_strategy_id = serializers.IntegerField()  # 允许读写,支持回显
    parent_element_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)

    class Meta:
        model = Element
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'created_by', 'usage_count', 'last_validated')

    def validate_project_id(self, value):
        project = _load_object(UiProject, value, 'project_id')
        _ensure_project_access(self, project, 'project_id')
        return value

    def validate_locator_strategy_id(self, value):
        _load_object(LocatorStrategy, value, 'locator_strategy_id')
        return value

    def validate(self, attrs):
        project_id = attrs.get('project_id')
        project = _load_object(UiProject, project_id, 'project_id') if project_id is not None else getattr(self.instance, 'project', None)
        _ensure_project_access(self, project, 'project_id')
        group_id = attrs.get('group_id')
        if group_id is not None:
            group = _load_object(ElementGroup, group_id, 'group_id')
            _same_project_error(project, group, 'group_id')
        parent_id = attrs.get('parent_element_id')
        if parent_id is not None:
            parent = _load_object(Element, parent_id, 'parent_element_id')
            _same_project_error(project, parent, 'parent_element_id')
        return attrs

    def get_parent_element(self, obj):
        """获取父元素信息"""
        if obj.parent_element:
            return {
                'id': obj.parent_element.id,
                'name': obj.parent_element.name,
                'page': obj.parent_element.page
            }
        return None

    def get_children_elements(self, obj):
        """获取子元素"""
        children = obj.element_set.all()
        return [{
            'id': child.id,
            'name': child.name,
            'element_type': child.element_type
        } for child in children]

    def get_all_locators(self, obj):
        """获取所有定位器（主要+备用）"""
        return obj.get_all_locators()

    def get_usage_scripts(self, obj):
        """获取使用此元素的脚本列表"""
        usages = obj.script_usages.select_related('script').all()[:5]  # 只返回前5个
        return [{
            'script_id': usage.script.id,
            'script_name': usage.script.name,
            'usage_type': usage.usage_type,
            'frequency': usage.frequency
        } for usage in usages]

    def create(self, validated_data):
        # 处理外键字段
        project_id = validated_data.pop('project_id')
        locator_strategy_id = validated_data.pop('locator_strategy_id')
        group_id = validated_data.pop('group_id', None)
        parent_element_id = validated_data.pop('parent_element_id', None)

        validated_data['project'] = UiProject.objects.get(id=project_id)
        validated_data['locator_strategy'] = LocatorStrategy.objects.get(id=locator_strategy_id)

        if group_id:
            validated_data['group'] = ElementGroup.objects.get(id=group_id)

        if parent_element_id:
            validated_data['parent_element'] = Element.objects.get(id=parent_element_id)

        return super().create(validated_data)

    def update(self, instance, validated_data):
        # 处理外键字段
        project_id = validated_data.pop('project_id', None)
        locator_strategy_id = validated_data.pop('locator_strategy_id', None)
        group_id = validated_data.pop('group_id', None)
        parent_element_id = validated_data.pop('parent_element_id', None)

        if project_id:
            validated_data['project'] = UiProject.objects.get(id=project_id)

        if locator_strategy_id:
            validated_data['locator_strategy'] = LocatorStrategy.objects.get(id=locator_strategy_id)

        if group_id is not None:  # 允许设置为None来清除分组
            if group_id:
                validated_data['group'] = ElementGroup.objects.get(id=group_id)
            else:
                validated_data['group'] = None

        if parent_element_id is not None:  # 允许设置为None来清除父元素
            if parent_element_id:
                validated_data['parent_element'] = Element.objects.get(id=parent_element_id)
            else:
                validated_data['parent_element'] = None

        return super().update(instance, validated_data)


class PageObjectSerializer(serializers.ModelSerializer):
    project = UiProjectSerializer(read_only=True)
    created_by = UserSerializer(read_only=True)
    elements_count = serializers.SerializerMethodField()
    elements = serializers.SerializerMethodField()
    project_id = serializers.IntegerField(write_only=True)

    class Meta:
        model = PageObject
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'created_by', 'template_code')

    def validate_project_id(self, value):
        project = _load_object(UiProject, value, 'project_id')
        _ensure_project_access(self, project, 'project_id')
        return value

    def validate(self, attrs):
        project_id = attrs.get('project_id')
        project = _load_object(UiProject, project_id, 'project_id') if project_id is not None else getattr(self.instance, 'project', None)
        _ensure_project_access(self, project, 'project_id')
        return attrs

    def create(self, validated_data):
        project_id = validated_data.pop('project_id')
        validated_data['project'] = _load_object(UiProject, project_id, 'project_id')
        validated_data.setdefault('created_by', _serializer_user(self))
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if 'project_id' in validated_data:
            project_id = validated_data.pop('project_id')
            validated_data['project'] = _load_object(UiProject, project_id, 'project_id')
        return super().update(instance, validated_data)

    def get_elements_count(self, obj):
        """获取页面对象包含的元素数量"""
        return obj.page_object_elements.count()

    def get_elements(self, obj):
        """获取页面对象包含的元素"""
        po_elements = obj.page_object_elements.select_related('element').all()
        return [{
            'id': po_element.id,
            'element_id': po_element.element.id,
            'element_name': po_element.element.name,
            'method_name': po_element.method_name,
            'is_property': po_element.is_property,
            'order': po_element.order
        } for po_element in po_elements]


class PageObjectCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PageObject
        fields = ('project', 'name', 'class_name', 'url_pattern', 'description')

    def validate_project(self, value):
        return _ensure_project_access(self, value)

    def create(self, validated_data):
        validated_data['created_by'] = _serializer_user(self)
        return super().create(validated_data)


class PageObjectElementSerializer(serializers.ModelSerializer):
    page_object = PageObjectSerializer(read_only=True)
    element = ElementEnhancedSerializer(read_only=True)
    page_object_id = serializers.IntegerField(write_only=True)
    element_id = serializers.IntegerField(write_only=True)

    class Meta:
        model = PageObjectElement
        fields = '__all__'
        read_only_fields = ('created_at',)

    def validate(self, attrs):
        page_object_id = attrs.get('page_object_id')
        page_object = _load_object(PageObject, page_object_id, 'page_object_id') if page_object_id is not None else getattr(self.instance, 'page_object', None)
        element_id = attrs.get('element_id')
        element = _load_object(Element, element_id, 'element_id') if element_id is not None else getattr(self.instance, 'element', None)
        _ensure_project_access(self, page_object.project, 'page_object_id')
        _same_project_error(page_object, element, 'element_id')
        return attrs

    def create(self, validated_data):
        page_object_id = validated_data.pop('page_object_id')
        element_id = validated_data.pop('element_id')
        validated_data['page_object'] = _load_object(PageObject, page_object_id, 'page_object_id')
        validated_data['element'] = _load_object(Element, element_id, 'element_id')
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if 'page_object_id' in validated_data:
            page_object_id = validated_data.pop('page_object_id')
            validated_data['page_object'] = _load_object(PageObject, page_object_id, 'page_object_id')
        if 'element_id' in validated_data:
            element_id = validated_data.pop('element_id')
            validated_data['element'] = _load_object(Element, element_id, 'element_id')
        return super().update(instance, validated_data)

    def validate_method_name(self, value):
        """验证方法名称是否符合命名规范"""
        import re
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', value):
            raise serializers.ValidationError("方法名称只能包含字母、数字和下划线，且不能以数字开头")
        return value


class ScriptStepSerializer(serializers.ModelSerializer):
    script = TestScriptSerializer(read_only=True)
    target_element = ElementEnhancedSerializer(read_only=True)
    page_object = PageObjectSerializer(read_only=True)

    script_id = serializers.IntegerField(write_only=True)
    target_element_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    page_object_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)

    class Meta:
        model = ScriptStep
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    def validate(self, attrs):
        script_id = attrs.get('script_id')
        script = _load_object(TestScript, script_id, 'script_id') if script_id is not None else getattr(self.instance, 'script', None)
        _ensure_project_access(self, script.project, 'script_id')
        target_element_id = attrs.get('target_element_id')
        target_element = _load_object(Element, target_element_id, 'target_element_id') if target_element_id is not None else getattr(self.instance, 'target_element', None)
        _same_project_error(script, target_element, 'target_element_id')
        page_object_id = attrs.get('page_object_id')
        page_object = _load_object(PageObject, page_object_id, 'page_object_id') if page_object_id is not None else getattr(self.instance, 'page_object', None)
        _same_project_error(script, page_object, 'page_object_id')
        action_type = attrs.get('action_type', getattr(self.instance, 'action_type', None))
        if action_type in ['CLICK', 'INPUT', 'SELECT', 'HOVER', 'VERIFY'] and target_element is None and page_object is None:
            raise serializers.ValidationError("此操作类型需要指定目标元素或页面对象")
        return attrs

    def create(self, validated_data):
        script_id = validated_data.pop('script_id')
        target_id = validated_data.pop('target_element_id', None)
        page_id = validated_data.pop('page_object_id', None)
        validated_data['script'] = _load_object(TestScript, script_id, 'script_id')
        validated_data['target_element'] = _load_object(Element, target_id, 'target_element_id') if target_id else None
        validated_data['page_object'] = _load_object(PageObject, page_id, 'page_object_id') if page_id else None
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if 'script_id' in validated_data:
            validated_data['script'] = _load_object(TestScript, validated_data.pop('script_id'), 'script_id')
        if 'target_element_id' in validated_data:
            target_id = validated_data.pop('target_element_id')
            validated_data['target_element'] = _load_object(Element, target_id, 'target_element_id') if target_id else None
        if 'page_object_id' in validated_data:
            page_id = validated_data.pop('page_object_id')
            validated_data['page_object'] = _load_object(PageObject, page_id, 'page_object_id') if page_id else None
        return super().update(instance, validated_data)


class ScriptElementUsageSerializer(serializers.ModelSerializer):
    script = TestScriptSerializer(read_only=True)
    element = ElementEnhancedSerializer(read_only=True)
    script_id = serializers.IntegerField(write_only=True)
    element_id = serializers.IntegerField(write_only=True)

    class Meta:
        model = ScriptElementUsage
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    def validate(self, attrs):
        script_id = attrs.get('script_id')
        script = _load_object(TestScript, script_id, 'script_id') if script_id is not None else getattr(self.instance, 'script', None)
        element_id = attrs.get('element_id')
        element = _load_object(Element, element_id, 'element_id') if element_id is not None else getattr(self.instance, 'element', None)
        _ensure_project_access(self, script.project, 'script_id')
        _same_project_error(script, element, 'element_id')
        return attrs

    def create(self, validated_data):
        script_id = validated_data.pop('script_id')
        element_id = validated_data.pop('element_id')
        validated_data['script'] = _load_object(TestScript, script_id, 'script_id')
        validated_data['element'] = _load_object(Element, element_id, 'element_id')
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if 'script_id' in validated_data:
            validated_data['script'] = _load_object(TestScript, validated_data.pop('script_id'), 'script_id')
        if 'element_id' in validated_data:
            validated_data['element'] = _load_object(Element, validated_data.pop('element_id'), 'element_id')
        return super().update(instance, validated_data)


class ScriptAnalysisSerializer(serializers.Serializer):
    """脚本分析结果序列化器"""
    element_usages = ScriptElementUsageSerializer(many=True, read_only=True)
    missing_elements = serializers.ListField(child=serializers.CharField(), read_only=True)
    recommendations = serializers.ListField(child=serializers.CharField(), read_only=True)
    complexity_score = serializers.IntegerField(read_only=True)


class ElementValidationSerializer(serializers.Serializer):
    """元素验证结果序列化器"""
    is_valid = serializers.BooleanField(read_only=True)
    validation_message = serializers.CharField(read_only=True)
    suggestions = serializers.ListField(child=serializers.CharField(), read_only=True)


class CodeGenerationSerializer(serializers.Serializer):
    """代码生成序列化器"""
    language = serializers.ChoiceField(choices=[('javascript', 'JavaScript'), ('python', 'Python')], default='javascript')
    framework = serializers.ChoiceField(choices=[('playwright', 'Playwright'), ('selenium', 'Selenium')], default='playwright')
    include_comments = serializers.BooleanField(default=True)

    def validate(self, data):
        # 可以添加更多验证逻辑
        return data


class TestCaseStepSerializer(serializers.ModelSerializer):
    """测试用例步骤序列化器"""
    test_case = serializers.PrimaryKeyRelatedField(queryset=TestCase.objects.all(), write_only=True)
    element = serializers.PrimaryKeyRelatedField(queryset=Element.objects.all(), allow_null=True, required=False)
    element_name = serializers.CharField(source='element.name', read_only=True)
    element_locator = serializers.CharField(source='element.locator_value', read_only=True)

    class Meta:
        model = TestCaseStep
        fields = [
            'id', 'test_case', 'step_number', 'action_type', 'element', 'element_name', 'element_locator',
            'input_value', 'wait_time', 'assert_type', 'assert_value', 'description', 'created_at'
        ]
        read_only_fields = ('created_at',)

    def validate(self, attrs):
        test_case = attrs.get('test_case', getattr(self.instance, 'test_case', None))
        element = attrs.get('element', getattr(self.instance, 'element', None))
        _ensure_project_access(self, test_case.project, 'test_case')
        _same_project_error(test_case, element, 'element')
        return attrs


class TestCaseSerializer(serializers.ModelSerializer):
    """测试用例序列化器"""
    steps = TestCaseStepSerializer(many=True, read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = TestCase
        fields = [
            'id', 'name', 'description', 'project', 'project_name', 'status', 'priority',
            'created_by', 'created_by_name', 'created_at', 'updated_at', 'steps'
        ]
        read_only_fields = ['created_by']

    def validate_project(self, value):
        project = _ensure_project_access(self, value)
        if self.instance is not None and self.instance.steps.filter(
            element__isnull=False
        ).exclude(element__project_id=project.id).exists():
            raise serializers.ValidationError('现有步骤元素必须属于目标项目')
        return project

    def create(self, validated_data):
        validated_data['created_by'] = _serializer_user(self)
        return super().create(validated_data)


class TestCaseExecutionSerializer(serializers.ModelSerializer):
    """测试用例执行记录序列化器"""
    test_case_name = serializers.CharField(source='test_case.name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    test_suite_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = TestCaseExecution
        fields = [
            'id', 'test_case', 'test_case_name', 'project', 'project_name',
            'test_suite', 'test_suite_name', 'execution_source', 'status',
            'engine', 'browser', 'headless', 'execution_logs', 'error_message',
            'screenshots', 'execution_time', 'started_at', 'finished_at',
            'created_by', 'created_by_name', 'created_at'
        ]
        read_only_fields = ['created_by']
    
    def validate(self, attrs):
        project = _ensure_project_access(self, attrs.get('project', getattr(self.instance, 'project', None)))
        test_case = attrs.get('test_case', getattr(self.instance, 'test_case', None))
        if attrs.get('engine', getattr(self.instance, 'engine', 'playwright')) == 'selenium' and not selenium_compatibility_enabled(
            getattr(self.context.get('request'), 'user', None)
        ):
            raise serializers.ValidationError({'engine': 'Selenium 兼容模式已被当前部署禁用'})

        test_suite = attrs.get('test_suite', getattr(self.instance, 'test_suite', None))
        if test_case is not None:
            _same_project_error(project, test_case, 'test_case')
        if test_suite is not None:
            _same_project_error(project, test_suite, 'test_suite')
            if test_case is not None and not test_suite.suite_test_cases.filter(test_case=test_case).exists():
                raise serializers.ValidationError({'test_suite': '测试套件不包含该测试用例'})
        return attrs

    def get_test_suite_name(self, obj):
        """获取测试套件名称"""
        return obj.test_suite.name if obj.test_suite else None
    
    def get_created_by_name(self, obj):
        """获取创建人姓名"""
        return obj.created_by.username if obj.created_by else '-'

    def create(self, validated_data):
        validated_data['created_by'] = _serializer_user(self)
        return super().create(validated_data)


class TestCaseRunSerializer(serializers.Serializer):
    """测试用例运行序列化器"""
    test_case_id = serializers.IntegerField()
    project_id = serializers.IntegerField()
    browser = serializers.ChoiceField(choices=['chrome', 'firefox', 'safari'], default='chrome')

    def validate(self, attrs):
        test_case = _load_object(TestCase, attrs.get('test_case_id'), 'test_case_id')
        project = _load_object(UiProject, attrs.get('project_id'), 'project_id')
        _ensure_project_access(self, project, 'project_id')
        _same_project_error(project, test_case, 'test_case_id')
        return attrs

    def validate_project_id(self, value):
        try:
            UiProject.objects.get(id=value)
        except UiProject.DoesNotExist:
            raise serializers.ValidationError("项目不存在")
        return value


class OperationRecordSerializer(serializers.ModelSerializer):
    """操作记录序列化器"""
    user_name = serializers.CharField(source='user.username', read_only=True)
    operation_type_display = serializers.CharField(source='get_operation_type_display', read_only=True)
    resource_type_display = serializers.CharField(source='get_resource_type_display', read_only=True)

    class Meta:
        model = OperationRecord
        fields = [
            'id', 'operation_type', 'operation_type_display', 'resource_type',
            'resource_type_display', 'resource_id', 'resource_name', 'description',
            'user', 'user_name', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


# ==================== 定时任务和通知相关序列化器 ====================

class UiScheduledTaskSerializer(serializers.ModelSerializer):
    """UI定时任务序列化器"""
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    test_suite_name = serializers.CharField(source='test_suite.name', read_only=True)
    task_type_display = serializers.CharField(source='get_task_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    trigger_type_display = serializers.CharField(source='get_trigger_type_display', read_only=True)
    notification_type_display = serializers.SerializerMethodField()

    class Meta:
        model = UiScheduledTask
        fields = [
            'id', 'name', 'description', 'task_type', 'task_type_display',
            'trigger_type', 'trigger_type_display', 'cron_expression',
            'interval_seconds', 'execute_at', 'project', 'project_name',
            'test_suite', 'test_suite_name', 'test_cases',
            'engine', 'browser', 'headless',
            'notify_on_success', 'notify_on_failure', 'notification_type', 'notification_type_display', 'notify_emails',
            'status', 'status_display',
            'last_run_time', 'next_run_time', 'total_runs',
            'successful_runs', 'failed_runs', 'last_result', 'error_message',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'created_by', 'last_run_time', 'next_run_time', 'total_runs',
            'successful_runs', 'failed_runs', 'last_result',
            'error_message', 'created_at', 'updated_at'
        ]

    def get_notification_type_display(self, obj):
        """获取通知类型显示"""
        if obj.notification_type:
            return obj.get_notification_type_display()
        return "-"

    def validate(self, attrs):
        """验证定时任务配置"""
        trigger_type = attrs.get('trigger_type')

        if trigger_type == 'CRON':
            if not attrs.get('cron_expression'):
                raise serializers.ValidationError("Cron表达式不能为空")

        elif trigger_type == 'INTERVAL':
            if not attrs.get('interval_seconds'):
                raise serializers.ValidationError("间隔秒数不能为空")
            if attrs['interval_seconds'] < 60:
                raise serializers.ValidationError("间隔秒数不能小于60秒")

        elif trigger_type == 'ONCE':
            if not attrs.get('execute_at'):
                raise serializers.ValidationError("执行时间不能为空")
            if attrs['execute_at'] <= timezone.now():
                raise serializers.ValidationError("执行时间必须大于当前时间")

        task_type = attrs.get('task_type', getattr(self.instance, 'task_type', None))
        if attrs.get('engine', getattr(self.instance, 'engine', 'playwright')) == 'selenium' and not selenium_compatibility_enabled(
            getattr(self.context.get('request'), 'user', None)
        ):
            raise serializers.ValidationError({'engine': 'Selenium 兼容模式已被当前部署禁用'})

        if task_type == 'TEST_SUITE' and not attrs.get('test_suite', getattr(self.instance, 'test_suite', None)):
            raise serializers.ValidationError("测试套件不能为空")
        raw_test_cases = attrs.get('test_cases')
        if raw_test_cases is None and self.instance is not None:
            raw_test_cases = list(self.instance.test_cases or [])
        raw_test_cases = raw_test_cases or []
        if task_type == 'TEST_CASE' and not raw_test_cases:
            raise serializers.ValidationError("至少选择一个测试用例")

        project = attrs.get('project') or getattr(self.instance, 'project', None)
        _ensure_project_access(self, project)
        suite = attrs.get('test_suite') or getattr(self.instance, 'test_suite', None)
        if suite and project and suite.project_id != project.id:
            raise serializers.ValidationError('测试套件必须属于同一项目')
        try:
            case_ids = [case.pk if isinstance(case, TestCase) else int(case) for case in raw_test_cases]
        except (TypeError, ValueError):
            raise serializers.ValidationError({'test_cases': '测试用例列表格式错误'})
        cases = list(TestCase.objects.filter(pk__in=case_ids))
        if len(cases) != len(set(case_ids)) or any(case.project_id != project.id for case in cases):
            raise serializers.ValidationError('所有测试用例必须属于同一项目')
        attrs['test_cases'] = case_ids

        return attrs

    def create(self, validated_data):
        """创建定时任务"""
        validated_data['created_by'] = self.context['request'].user
        instance = super().create(validated_data)
        # 计算下次运行时间
        instance.next_run_time = instance.calculate_next_run()
        instance.save()

        # 创建对应的通知设置（如果启用了通知）
        if instance.notify_on_success or instance.notify_on_failure:
            from .models import UiTaskNotificationSetting
            from apps.core.models import UnifiedNotificationConfig

            # 确定通知类型（默认为webhook）
            notification_type = validated_data.get('notification_type', 'webhook')

            # 根据通知类型选择合适的通知配置
            notification_config = None
            if notification_type in ['webhook', 'both']:
                # 如果需要Webhook通知，优先选择Webhook配置
                notification_config = UnifiedNotificationConfig.objects.filter(
                    config_type__in=['webhook_wechat', 'webhook_feishu', 'webhook_dingtalk'],
                    is_active=True
                ).first()

            if not notification_config:
                # 如果没有找到webhook配置或者是邮件通知，使用默认配置
                notification_config = UnifiedNotificationConfig.objects.filter(
                    is_default=True,
                    is_active=True
                ).first()

            # 创建通知设置
            UiTaskNotificationSetting.objects.create(
                task=instance,
                notification_type=notification_type,
                is_enabled=True,
                notify_on_success=instance.notify_on_success,
                notify_on_failure=instance.notify_on_failure,
                notification_config=notification_config
            )

        return instance

    def update(self, instance, validated_data):
        """更新定时任务"""
        # 更新任务基本信息
        instance = super().update(instance, validated_data)

        # 重新计算下次运行时间
        instance.next_run_time = instance.calculate_next_run()
        instance.save()

        # 更新通知设置
        if instance.notify_on_success or instance.notify_on_failure:
            from .models import UiTaskNotificationSetting
            from apps.core.models import UnifiedNotificationConfig

            # 确定通知类型（默认为webhook）
            notification_type = validated_data.get('notification_type', 'webhook')

            # 根据通知类型选择合适的通知配置
            notification_config = None
            if notification_type in ['webhook', 'both']:
                # 如果需要Webhook通知，优先选择Webhook配置
                notification_config = UnifiedNotificationConfig.objects.filter(
                    config_type__in=['webhook_wechat', 'webhook_feishu', 'webhook_dingtalk'],
                    is_active=True
                ).first()

            if not notification_config:
                # 如果没有找到webhook配置或者是邮件通知，使用默认配置
                notification_config = UnifiedNotificationConfig.objects.filter(
                    is_default=True,
                    is_active=True
                ).first()

            # 获取或创建通知设置
            notification_setting, created = UiTaskNotificationSetting.objects.get_or_create(
                task=instance,
                defaults={
                    'notification_type': notification_type,
                    'is_enabled': True,
                    'notify_on_success': instance.notify_on_success,
                    'notify_on_failure': instance.notify_on_failure,
                    'notification_config': notification_config
                }
            )

            # 如果通知设置已存在，更新它
            if not created:
                notification_setting.notification_type = notification_type
                notification_setting.is_enabled = True
                notification_setting.notify_on_success = instance.notify_on_success
                notification_setting.notify_on_failure = instance.notify_on_failure
                notification_setting.notification_config = notification_config
                notification_setting.save()
        else:
            # 如果不需要通知，禁用通知设置
            from .models import UiTaskNotificationSetting
            UiTaskNotificationSetting.objects.filter(task=instance).update(is_enabled=False)

        return instance


class AICaseSerializer(serializers.ModelSerializer):
    project = UiProjectSerializer(read_only=True)
    created_by = UserSerializer(read_only=True)
    project_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)

    class Meta:
        model = AICase
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'created_by')

    def validate_project_id(self, value):
        if value is None:
            return value
        project = _load_object(UiProject, value, 'project_id')
        _ensure_project_access(self, project, 'project_id')
        return value

    def create(self, validated_data):
        project_id = validated_data.pop('project_id', None)
        if project_id is not None:
            validated_data['project'] = _load_object(UiProject, project_id, 'project_id')
        validated_data['created_by'] = _serializer_user(self)
        return super().create(validated_data)


class AIExecutionRecordSerializer(serializers.ModelSerializer):
    project = UiProjectSerializer(read_only=True)
    ai_case = AICaseSerializer(read_only=True)
    executed_by = UserSerializer(read_only=True)
    project_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    ai_case_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    ai_case_name = serializers.CharField(source='ai_case.name', read_only=True)
    executed_by_name = serializers.CharField(source='executed_by.username', read_only=True)

    class Meta:
        model = AIExecutionRecord
        fields = [
            'id', 'project', 'project_id', 'project_name', 'ai_case', 'ai_case_id', 'ai_case_name', 'case_name',
            'task_description',
            'execution_mode', 'status', 'start_time', 'end_time', 'duration',
            'logs', 'steps_completed', 'planned_tasks', 'executed_by', 'executed_by_name',
            'gif_path', 'screenshots_sequence'
        ]
        read_only_fields = ('start_time', 'end_time', 'duration', 'executed_by', 'gif_path', 'screenshots_sequence')

    def validate(self, attrs):
        project_id = attrs.pop('project_id', None)
        project = _load_object(UiProject, project_id, 'project_id') if project_id is not None else getattr(self.instance, 'project', None)
        if project is not None:
            _ensure_project_access(self, project)
        ai_case_id = attrs.pop('ai_case_id', None) if 'ai_case_id' in attrs else None
        ai_case = _load_object(AICase, ai_case_id, 'ai_case_id') if ai_case_id is not None else getattr(self.instance, 'ai_case', None)
        if ai_case is not None and ai_case.project_id is None:
            user = _serializer_user(self)
            if not is_ui_admin(user) and ai_case.created_by_id != getattr(user, 'id', None):
                raise serializers.ValidationError({'ai_case_id': '无权使用其他用户的项目外 AI 用例'})
        if ai_case is not None:
            attrs['ai_case'] = ai_case
        attrs['project'] = project
        return attrs

    def create(self, validated_data):
        user = _serializer_user(self)
        if user is not None and getattr(user, 'is_authenticated', False):
            validated_data['executed_by'] = user
        return super().create(validated_data)


class UiNotificationLogSerializer(serializers.ModelSerializer):
    """UI通知日志序列化器"""
    recipient_names = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    notification_type_display = serializers.CharField(source='get_notification_type_display', read_only=True)
    retry_status = serializers.SerializerMethodField()
    task_type_display = serializers.SerializerMethodField()
    actual_notification_type_display = serializers.SerializerMethodField()

    class Meta:
        model = UiNotificationLog
        fields = [
            'id', 'task', 'task_name', 'notification_type',
            'notification_type_display', 'actual_notification_type_display', 'task_type_display',
            'sender_name', 'sender_email',
            'recipient_names', 'webhook_bot_info', 'notification_content',
            'status', 'status_display', 'error_message', 'response_info',
            'created_at', 'sent_at', 'retry_count', 'retry_status'
        ]
        read_only_fields = ['created_at', 'sent_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['webhook_bot_info'] = {'configured': bool(instance.webhook_bot_info)} if instance.webhook_bot_info else {}
        data['response_info'] = {'available': bool(instance.response_info)} if instance.response_info else {}
        return data

    def get_recipient_names(self, obj):
        """获取收件人姓名列表"""
        return obj.get_recipient_names()

    def get_retry_status(self, obj):
        """获取重试状态"""
        return obj.get_retry_status()

    def get_task_type_display(self, obj):
        """获取任务类型显示 - 使用保存的快照值"""
        if obj.task_type:
            # 使用保存的任务类型快照
            task_type_choices = dict(UiScheduledTask.TASK_TYPE_CHOICES)
            return task_type_choices.get(obj.task_type, obj.task_type)
        # 如果 task_type 为空，返回未记录，不要从 task 对象获取（避免显示修改后的值）
        return '未记录'

    def get_actual_notification_type_display(self, obj):
        """获取实际的通知类型显示 - 根据实际发送的通知来判断"""
        # 优先检查webhook_bot_info,如果存在则说明是webhook通知
        if obj.webhook_bot_info:
            bot_type = obj.webhook_bot_info.get('bot_type', '') or obj.webhook_bot_info.get('type', '')
            # 根据机器人类型返回友好名称
            type_map = {
                'wechat': '企微机器人',
                'feishu': '飞书机器人',
                'dingtalk': '钉钉机器人'
            }
            return type_map.get(bot_type, 'Webhook机器人')

        # 检查recipient_info,如果存在则说明是邮箱通知
        if obj.recipient_info:
            if isinstance(obj.recipient_info, list) and len(obj.recipient_info) > 0:
                return '邮箱通知'
            elif isinstance(obj.recipient_info, dict) and obj.recipient_info.get('email'):
                return '邮箱通知'

        # 如果都没有,回退到任务的notification_type
        if obj.task:
            notification_type = obj.task.notification_type
            type_map = {
                'email': '邮箱通知',
                'webhook': 'Webhook机器人',
                'both': '两种都发送'
            }
            return type_map.get(notification_type, notification_type)

        return '-'


class UiTaskNotificationSettingSerializer(serializers.ModelSerializer):
    """UI任务通知设置序列化器"""
    notification_type_display = serializers.CharField(source='get_notification_type_display', read_only=True)
    notification_config_name = serializers.CharField(source='notification_config.name', read_only=True)
    active_types = serializers.SerializerMethodField()

    class Meta:
        model = UiTaskNotificationSetting
        fields = [
            'id', 'task', 'notification_type', 'notification_type_display',
            'notification_config', 'notification_config_name', 'is_enabled',
            'notify_on_success', 'notify_on_failure', 'notify_on_timeout',
            'notify_on_error', 'custom_webhook_bots', 'active_types',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate_task(self, value):
        return _ensure_project_access(self, value.project, 'task')

    def get_active_types(self, obj):
        """获取激活的通知类型"""
        types = obj.get_active_notification_types()
        type_names = []
        if 'email' in types:
            type_names.append('邮箱')
        if 'webhook' in types:
            type_names.append('Webhook机器人')
        return ', '.join(type_names) if type_names else "无"

