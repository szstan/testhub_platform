"""Prepare a scene's authorized data and project components before device use."""
from __future__ import annotations

from types import SimpleNamespace

from .scene_component_runtime import SceneComponentError, expand_project_flow
from .scene_data_runtime import SceneDataError
from .scene_sensitive_flow import SensitiveFlowError, protect_sensitive_inputs
from .scene_variable_compat import compatible_execution_variables


class ScenePreparationError(ValueError):
    """The scene cannot be prepared safely for execution."""


SCENE_CASE_CHANGED_MESSAGE = '测试用例已变更，请重新提交单步诊断'
_NESTED = ('steps', 'then_steps', 'else_steps', 'try_steps', 'catch_steps', 'finally_steps')


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
        flow = protect_sensitive_inputs(flow, variables)
    except (SceneDataError, SceneComponentError, SensitiveFlowError):
        raise ScenePreparationError('场景数据、密钥或组件配置无效，请检查项目权限及配置') from None
    return flow, variables
