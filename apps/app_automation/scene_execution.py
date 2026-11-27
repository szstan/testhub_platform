"""Prepare a scene's authorized data and project components before device use."""
from __future__ import annotations

import copy
import re
from types import SimpleNamespace

from .access import project_accessible
from .constants import (
    ANDROID_PACKAGE_NAME_PATTERN,
    ElementType,
    ExecutionEngine,
    NATIVE_ELEMENT_STRATEGIES,
)

from .scene_component_runtime import (
    SceneComponentError,
    expand_project_flow,
    validate_legacy_component_references,
)
from .models import AppElement
from .scene_data_runtime import SceneDataError
from .scene_sensitive_flow import SensitiveFlowError, protect_sensitive_inputs
from .scene_variable_compat import compatible_execution_variables


class ScenePreparationError(ValueError):
    """The scene cannot be prepared safely for execution."""


SCENE_CASE_CHANGED_MESSAGE = '测试用例已变更，请重新提交单步诊断'
_NESTED = ('steps', 'then_steps', 'else_steps', 'try_steps', 'catch_steps', 'finally_steps')
_APPIUM_SUPPORTED_ACTIONS = frozenset({
    'click', 'touch', 'double_click', 'long_press', 'input', 'text', 'clear_input',
    'wait_element', 'assert_element', 'assert_text', 'extract_text', 'wait', 'sleep',
    'app_start', 'app_stop', 'app_restart', 'set_variable', 'unset_variable',
    'extract_output', 'assert', 'assert_value', 'assert_response', 'api_request',
    'if', 'loop', 'sequence', 'try', 'swipe', 'drag', 'swipe_to',
    'image_exists_click', 'image_exists_click_chain', 'screenshot', 'key_event',
    'hide_keyboard', 'handle_permission', 'foreach_assert',
})
_APPIUM_LOCATOR_ACTIONS = frozenset({
    'click', 'touch', 'double_click', 'long_press', 'input', 'text', 'clear_input',
    'wait_element', 'assert_element', 'assert_text', 'extract_text',
})
_APPIUM_NATIVE_ELEMENT_ACTIONS = _APPIUM_LOCATOR_ACTIONS | {'wait'}
_APPIUM_VISUAL_ELEMENT_ACTIONS = frozenset({
    'click', 'touch', 'double_click', 'long_press', 'input', 'text', 'clear_input',
})
_APPIUM_INLINE_VISUAL_REFERENCES = {
    'swipe': {'element_id': 'selector'},
    'drag': {'start_element_id': 'start_selector', 'end_element_id': 'end_selector'},
    'swipe_to': {'target_element_id': 'target_selector'},
    'image_exists_click': {'element_id': 'selector', 'fallback_element_id': 'fallback_selector'},
    'image_exists_click_chain': {'element_id': 'selector', 'fallback_element_id': 'fallback_selector'},
    'foreach_assert': {'click_element_id': 'click_selector', 'ocr_element_id': 'ocr_selector'},
}
_APPIUM_INLINE_SELECTOR_TYPES = (
    'image', 'pos', 'region', 'relative', 'resource_id', 'text', 'content_desc', 'class',
)
_APPIUM_NATIVE_SELECTOR_TYPES = ('resource_id', 'text', 'content_desc', 'class')
_MAX_APPIUM_STEPS = 1000
_MAX_NATIVE_ELEMENT_ID_LENGTH = 18


def _appium_issue(code, message):
    return {'code': code, 'message': message}


def _native_element_config_is_valid(config, expected_package):
    if not isinstance(config, dict) or set(config) - {
        'package_name', 'strategy', 'value', 'container',
    } or not {'package_name', 'strategy', 'value'} <= set(config):
        return False
    if (
        not isinstance(config['package_name'], str)
        or config['package_name'] != expected_package
        or not re.fullmatch(ANDROID_PACKAGE_NAME_PATTERN, config['package_name'])
        or config['strategy'] not in NATIVE_ELEMENT_STRATEGIES
        or not isinstance(config['value'], str)
        or not config['value'].strip()
        or len(config['value']) > 512
    ):
        return False
    if 'container' in config:
        container = config['container']
        if (
            not isinstance(container, dict)
            or set(container) != {'strategy', 'value'}
            or container['strategy'] not in NATIVE_ELEMENT_STRATEGIES
            or not isinstance(container['value'], str)
            or not container['value'].strip()
            or len(container['value']) > 512
        ):
            return False
    return True


def _normalized_element_id(value):
    if type(value) is int and value > 0:
        return value
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and 0 < len(value) <= _MAX_NATIVE_ELEMENT_ID_LENGTH
        and int(value) > 0
    ):
        return int(value)
    return None


def _has_configured_value(value):
    return value is not None and value != '' and value != []


def _has_appium_inline_locator(action, options):
    selector_type = options.get('selector_type', 'image')
    selector = options.get('selector')
    if not _has_configured_value(selector) or selector_type not in _APPIUM_INLINE_SELECTOR_TYPES:
        return False
    if action in {'wait', 'wait_element', 'assert_element'}:
        return selector_type == 'image' or selector_type in _APPIUM_NATIVE_SELECTOR_TYPES
    if action in {'assert_text', 'extract_text'}:
        return selector_type == 'region' or selector_type in _APPIUM_NATIVE_SELECTOR_TYPES
    return action in _APPIUM_LOCATOR_ACTIONS


def _appium_action_target_issue(action, options):
    if action == 'swipe':
        coordinates = (
            _has_configured_value(options.get('start'))
            and _has_configured_value(options.get('end'))
        ) or (
            _has_configured_value(options.get('start_ratio'))
            and _has_configured_value(options.get('end_ratio'))
        ) or (
            options.get('selector_type') == 'pos'
            and _has_configured_value(options.get('selector'))
        )
        if coordinates or options.get('direction') in ('up', 'down', 'left', 'right'):
            return None
        return _appium_issue('explicit_target_required', 'Appium 滑动动作需要明确的方向或起止坐标')
    if action == 'drag':
        selector_types = (
            options.get('start_selector_type', 'image'),
            options.get('end_selector_type', 'image'),
        )
        if (
            not all(_has_configured_value(options.get(field)) for field in ('start_selector', 'end_selector'))
            or any(selector_type not in _APPIUM_INLINE_SELECTOR_TYPES for selector_type in selector_types)
        ):
            return _appium_issue('explicit_target_required', 'Appium 拖拽动作需要明确且受支持的起点和终点定位')
    if action == 'swipe_to':
        target_type = options.get('target_selector_type', 'image')
        if (
            not _has_configured_value(options.get('target_selector'))
            or target_type not in ('image', *_APPIUM_NATIVE_SELECTOR_TYPES)
        ):
            return _appium_issue('explicit_target_required', 'Appium 滑动查找动作需要明确的图片或原生定位')
    if action == 'image_exists_click':
        if (
            not _has_configured_value(options.get('selector'))
            or options.get('selector_type', 'image') not in _APPIUM_INLINE_SELECTOR_TYPES
        ):
            return _appium_issue('explicit_target_required', 'Appium 条件点击动作需要明确且受支持的主定位')
    if action == 'image_exists_click_chain':
        selector_types = (
            options.get('selector_type', 'image'),
            options.get('fallback_selector_type', 'image'),
        )
        if (
            not all(_has_configured_value(options.get(field)) for field in ('selector', 'fallback_selector'))
            or any(selector_type not in _APPIUM_INLINE_SELECTOR_TYPES for selector_type in selector_types)
        ):
            return _appium_issue('explicit_target_required', 'Appium 连续条件点击需要明确且受支持的主定位和备用定位')
    if action == 'foreach_assert' and (
        not _has_configured_value(options.get('click_selector'))
        or options.get('click_selector_type', 'image') not in _APPIUM_INLINE_SELECTOR_TYPES
    ):
        return _appium_issue('explicit_target_required', 'Appium 循环断言需要明确且受支持的点击定位')
    return None


def _resolve_appium_flow(test_case, flow, user, package_name):
    """Pin native definitions in ephemeral `resolved_appium_elements` step maps."""
    prepared = copy.deepcopy(flow)
    issues = []
    if not prepared:
        issues.append(_appium_issue('empty_flow', 'Appium 场景必须至少包含一个动作'))
    element_cache = {}
    state = {'steps': 0}

    def add_issue(code, message):
        issue = _appium_issue(code, message)
        if issue not in issues:
            issues.append(issue)

    def visit(steps, depth=0):
        if depth > 16 or not isinstance(steps, list):
            add_issue('invalid_flow', 'Appium 场景步骤格式无效或嵌套过深')
            return
        for step in steps:
            state['steps'] += 1
            if state['steps'] > _MAX_APPIUM_STEPS:
                add_issue('too_many_steps', 'Appium 场景最多支持 1000 个步骤')
                return
            if not isinstance(step, dict):
                add_issue('invalid_step', 'Appium 场景步骤必须是对象')
                continue

            action = step.get('type', step.get('action', step.get('kind')))
            if not isinstance(action, str) or action.lower() not in _APPIUM_SUPPORTED_ACTIONS:
                add_issue('unsupported_action', 'Appium 引擎不支持场景中的动作')
                continue
            action = action.lower()

            resolved = {}
            primary_visual_reference = False
            containers = [('step', step)]
            config = step.get('config')
            if isinstance(config, dict):
                containers.append(('config', config))
            selector_config = {**(config or {}), **step} if isinstance(config, dict) else step
            explicit_visual = _has_appium_inline_locator(action, selector_config)
            target_issue = _appium_action_target_issue(action, selector_config)
            if target_issue is not None:
                add_issue(target_issue['code'], target_issue['message'])
            for container_name, container in containers:
                container.pop('resolved_appium_elements', None)
                for field, raw_id in container.items():
                    if field != 'element_id' and not field.endswith('_element_id'):
                        continue
                    if raw_id in (None, ''):
                        continue
                    element_id = _normalized_element_id(raw_id)
                    if element_id is None:
                        add_issue('invalid_element_id', '定位元素 ID 无效')
                        continue
                    if element_id not in element_cache:
                        element_cache[element_id] = AppElement.objects.filter(
                            pk=element_id, project_id=test_case.project_id, is_active=True,
                        ).first()
                    element = element_cache[element_id]
                    if element is not None and element.element_type in {
                        ElementType.IMAGE, ElementType.POS, ElementType.REGION,
                    }:
                        valid_visual_reference = (
                            field == 'element_id'
                            and action in _APPIUM_VISUAL_ELEMENT_ACTIONS
                        ) or (
                            field == 'element_id'
                            and element.element_type == ElementType.IMAGE
                            and action in {'wait', 'wait_element', 'assert_element'}
                        ) or (
                            field == 'element_id'
                            and element.element_type == ElementType.REGION
                            and action in {'assert_text', 'extract_text'}
                            and selector_config.get('selector_type') == ElementType.REGION
                            and _has_configured_value(selector_config.get('selector'))
                        )
                        if valid_visual_reference:
                            primary_visual_reference = True
                            continue
                        # The editor also keeps the element ID next to an inline visual selector.
                        # These handlers already consume the typed selector fields.
                        selector_field = _APPIUM_INLINE_VISUAL_REFERENCES.get(action, {}).get(field)
                        if selector_field and (
                            _has_configured_value(selector_config.get(selector_field))
                            and selector_config.get(f'{selector_field}_type', 'image') == element.element_type
                        ):
                            continue
                        add_issue(
                            'unsupported_element_reference',
                            '此动作不支持通过该元素 ID 定位，请使用动作支持的显式选择器',
                        )
                        continue
                    if (
                        element is None or element.element_type != ElementType.NATIVE
                        or not _native_element_config_is_valid(element.config, package_name)
                    ):
                        add_issue(
                            'native_element_unavailable',
                            '定位元素无效、已停用或不属于当前项目/应用',
                        )
                        continue
                    if action in {'assert_text', 'extract_text'} and selector_config.get(
                        'selector_type'
                    ) not in (None, *_APPIUM_NATIVE_SELECTOR_TYPES):
                        add_issue(
                            'unsupported_element_reference',
                            '文本动作的元素引用类型与选择器类型不匹配',
                        )
                        continue
                    if field != 'element_id' or action not in _APPIUM_NATIVE_ELEMENT_ACTIONS:
                        add_issue('unsupported_element_reference', '此动作须选择图片或坐标，原生元素用于控件动作')
                        continue
                    locator = {
                        'element_id': element.pk,
                        'package_name': element.config['package_name'],
                        'strategy': element.config['strategy'],
                        'value': element.config['value'],
                    }
                    if 'container' in element.config:
                        locator['container'] = dict(element.config['container'])
                    path = field if container_name == 'step' else f'{container_name}.{field}'
                    resolved[path] = locator

            if (
                isinstance(action, str)
                and action in _APPIUM_LOCATOR_ACTIONS
                and not (resolved or primary_visual_reference or explicit_visual)
            ):
                add_issue('native_element_required', 'Appium 定位动作需要原生元素或显式的视觉定位')
            if resolved:
                step['resolved_appium_elements'] = resolved

            for _container_name, container in containers:
                for field in _NESTED:
                    nested = container.get(field)
                    if isinstance(nested, list):
                        visit(nested, depth + 1)

    visit(prepared)
    return prepared, issues


def _appium_prerequisite_report(test_case, *, user, flow, engine):
    issues = []
    project = test_case.project if test_case.project_id else None
    package_name = test_case.app_package.package_name if test_case.app_package_id else ''
    checks = []

    accessible = project_accessible(user, project)
    checks.append({'name': 'project_access', 'ready': accessible})
    if not accessible:
        issues.append(_appium_issue('project_access', '无权访问用例所属项目'))

    enabled = bool(project is not None and project.appium_enabled)
    checks.append({'name': 'project_appium_enabled', 'ready': enabled})
    if not enabled:
        issues.append(_appium_issue('project_appium_disabled', '用例所属项目未启用 Appium 引擎'))

    valid_package = (
        isinstance(package_name, str)
        and bool(re.fullmatch(ANDROID_PACKAGE_NAME_PATTERN, package_name))
    )
    checks.append({'name': 'android_package', 'ready': valid_package})
    if not valid_package:
        issues.append(_appium_issue('android_package', 'Appium 用例必须配置有效的 Android 应用包名'))

    expanded = copy.deepcopy(flow)
    if accessible and isinstance(expanded, list):
        try:
            if _contains_project_reference(expanded):
                expanded = expand_project_flow(SimpleNamespace(user=user), test_case, expanded)
            validate_legacy_component_references(expanded)
        except SceneComponentError:
            issues.append(_appium_issue('scene_components', '场景组件无效或无权访问'))

    if accessible and not any(issue['code'] == 'scene_components' for issue in issues):
        expanded, flow_issues = _resolve_appium_flow(
            test_case, expanded, user, package_name,
        )
        issues.extend(flow_issues)
    else:
        flow_issues = []

    action_ready = not any(issue['code'] in {
        'unsupported_action', 'invalid_flow', 'invalid_step', 'too_many_steps', 'empty_flow',
        'explicit_target_required',
    } for issue in flow_issues)
    locator_ready = not any(issue['code'] in {
        'invalid_element_id', 'unsupported_element_reference', 'native_element_unavailable',
        'native_element_required',
    } for issue in flow_issues)
    checks.extend([
        {'name': 'supported_actions', 'ready': action_ready},
        {'name': 'native_elements', 'ready': locator_ready},
    ])
    return expanded, {
        'scope': 'case_configuration',
        'engine': engine,
        'ready': not issues,
        'package_name': package_name if valid_package else '',
        'checks': checks,
        'issues': issues,
    }


def check_appium_prerequisites(test_case, *, user, engine=ExecutionEngine.APPIUM, flow=None):
    """Check saved scene prerequisites without connecting to or mutating a device.

    When ready, the worker preparation contract keeps each step's existing
    `element_id` and adds `resolved_appium_elements`, keyed by the original ID
    field path, with `{element_id, package_name, strategy, value, container?}`.
    """
    if flow is None:
        flow = saved_root_steps(test_case)
    _prepared_flow, report = _appium_prerequisite_report(
        test_case, user=user, flow=flow, engine=engine,
    )
    return report


def saved_root_steps(test_case):
    """Return only the saved top-level steps, without expanding component refs."""
    raw_flow = test_case.ui_flow
    if isinstance(raw_flow, list):
        return raw_flow
    if isinstance(raw_flow, dict):
        return raw_flow.get('steps', [])
    return []


def validate_saved_root_step(test_case, step_index):
    if type(step_index) is not int or step_index < 0:
        raise ScenePreparationError('步骤编号无效')
    steps = saved_root_steps(test_case)
    if not isinstance(steps, list) or step_index >= len(steps):
        raise ScenePreparationError('步骤编号超出用例范围')
    if not isinstance(steps[step_index], dict):
        raise ScenePreparationError('场景步骤无效')
    return steps[step_index]


def assert_scene_case_version(test_case, expected_case_updated_at):
    from django.utils.dateparse import parse_datetime

    if (
        test_case is None
        or not isinstance(expected_case_updated_at, str)
        or not expected_case_updated_at
        or len(expected_case_updated_at) > 64
    ):
        raise ScenePreparationError(SCENE_CASE_CHANGED_MESSAGE)
    expected = parse_datetime(expected_case_updated_at)
    if expected is None or expected != test_case.updated_at:
        raise ScenePreparationError(SCENE_CASE_CHANGED_MESSAGE)


def validate_diagnostic_selection(test_case, step_index, expected_case_updated_at):
    assert_scene_case_version(test_case, expected_case_updated_at)
    validate_saved_root_step(test_case, step_index)
    return step_index


def _contains_project_reference(steps, depth=0):
    if depth > 32 or not isinstance(steps, list):
        raise ScenePreparationError('场景嵌套步骤无效')
    for step in steps:
        if not isinstance(step, dict):
            raise ScenePreparationError('场景步骤无效')
        if step.get('kind') == 'custom' and step.get('version') is not None:
            return True
        for key in _NESTED:
            if isinstance(step.get(key), list) and _contains_project_reference(step[key], depth + 1):
                return True
        config = step.get('config')
        if isinstance(config, dict) and step.get('kind') != 'custom':
            for key in _NESTED:
                if isinstance(config.get(key), list) and _contains_project_reference(config[key], depth + 1):
                    return True
    return False


def prepare_scene_execution(
    test_case,
    execution=None,
    *,
    step_index=None,
    expected_case_updated_at=None,
):
    """Resolve data and project references before launching or touching a device."""
    if step_index is not None:
        validate_diagnostic_selection(test_case, step_index, expected_case_updated_at)
        flow = [validate_saved_root_step(test_case, step_index)]
    else:
        raw_flow = test_case.ui_flow
        if isinstance(raw_flow, list):
            flow = raw_flow
        elif isinstance(raw_flow, dict):
            flow = raw_flow.get('steps', [])
        else:
            flow = []
    if execution is not None and getattr(execution, 'test_case_id', None) != getattr(test_case, 'pk', None):
        raise ScenePreparationError('执行记录与测试用例不匹配')
    if execution is None:
        execution = SimpleNamespace(dataset_id=None, user=None)
    try:
        variables = compatible_execution_variables(execution, test_case)
        if _contains_project_reference(flow):
            flow = expand_project_flow(execution, test_case, flow)
        validate_legacy_component_references(flow)
        flow = protect_sensitive_inputs(flow, variables)
    except (SceneDataError, SceneComponentError, SensitiveFlowError):
        raise ScenePreparationError('场景数据、密钥或组件配置无效，请检查项目权限及配置') from None

    engine = getattr(execution, 'engine', None) or getattr(test_case, 'engine', ExecutionEngine.LEGACY)
    if engine == ExecutionEngine.APPIUM:
        flow, report = _appium_prerequisite_report(
            test_case,
            user=getattr(execution, 'user', None),
            flow=flow,
            engine=engine,
        )
        if not report['ready']:
            raise ScenePreparationError(report['issues'][0]['message'])
    return flow, variables
