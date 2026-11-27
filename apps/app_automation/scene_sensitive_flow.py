"""Keep secret-derived scene inputs on the protected device-input path."""
from __future__ import annotations

import copy
import re


class SensitiveFlowError(ValueError):
    """An unsafe sensitive value was supplied directly in a scene step."""


_STEP_FIELDS = ('steps', 'then_steps', 'else_steps', 'try_steps', 'catch_steps', 'finally_steps')
_REFERENCE = re.compile(r'\{\{\s*([^}]+?)\s*\}\}|\$\{\s*([^}]+?)\s*\}')
_EXACT_SCENE_REFERENCE = re.compile(
    r'(?:\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*?)\s*\}\}'
    r'|\$\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*?)\s*\})\Z'
)
_SECRET_NAME = re.compile(r'password|passwd|secret|token|credential|api[_-]?key|密码|密钥|令牌', re.IGNORECASE)


def _input_expression(step):
    config = step.get('config') if isinstance(step.get('config'), dict) else {}
    action = str(step.get('type', '')).lower()
    fields = ('text', 'value') if action == 'text' else ('value',)
    for field in fields:
        if field in step:
            return step[field]
        if field in config:
            return config[field]
    return None


def _safe_sensitive_input_value(value):
    if (
        value is None
        or isinstance(value, str) and not value.strip()
        or type(value) in (list, dict) and not value
    ):
        return True
    if not isinstance(value, str):
        return False
    match = _EXACT_SCENE_REFERENCE.fullmatch(value)
    return bool(match and (match.group(1) or match.group(2)) != 'params')


def protect_sensitive_inputs(flow, variables):
    """Return a copied flow whose secret-derived text inputs are marked sensitive.

    Secret aliases belong in server-resolved variables, never literal step data.
    Taint is propagated through set-variable steps and control-flow children.
    Values are used only in memory and are never emitted in errors or logs.
    """
    if not isinstance(flow, list) or not isinstance(variables, list):
        raise SensitiveFlowError('场景流程或变量配置无效')
    result = copy.deepcopy(flow)
    names = set()
    values = set()
    for item in variables:
        if not isinstance(item, dict):
            continue
        name, value = item.get('name'), item.get('value')
        if item.get('sensitive') is True and isinstance(name, str):
            names.add(name)
            if isinstance(value, str) and value:
                values.add(value)

    def steps_in(sequence, depth=0):
        if depth > 32 or not isinstance(sequence, list):
            raise SensitiveFlowError('场景嵌套步骤无效')
        for step in sequence:
            if not isinstance(step, dict):
                raise SensitiveFlowError('场景步骤无效')
            yield step
            for field in _STEP_FIELDS:
                if isinstance(step.get(field), list):
                    yield from steps_in(step[field], depth + 1)
            config = step.get('config')
            if isinstance(config, dict) and step.get('kind') != 'custom':
                for field in _STEP_FIELDS:
                    if isinstance(config.get(field), list):
                        yield from steps_in(config[field], depth + 1)

    all_steps = list(steps_in(result))
    if len(all_steps) > 10_000:
        raise SensitiveFlowError('场景步骤数量超过限制')

    def is_sensitive(value):
        if not isinstance(value, str):
            return False
        if value.startswith('secret://'):
            raise SensitiveFlowError('请先在场景变量中配置密钥别名，再在步骤中引用该变量')
        if any(value == secret or (len(secret) >= 4 and secret in value) for secret in values):
            return True
        for match in _REFERENCE.finditer(value):
            expression = (match.group(1) or match.group(2)).strip()
            parts = expression.split('.')
            if parts and parts[0] in {'local', 'global', 'outputs', 'steps'}:
                parts = parts[1:]
            candidate = parts[0].split('[', 1)[0] if parts else ''
            unscoped = '.'.join(parts)
            if candidate in names or unscoped in names or _SECRET_NAME.search(expression):
                return True
        return False

    # A secret may be copied through a later set-variable step. Iterate to a
    # fixed point so the result does not depend on source ordering or branches.
    for _ in range(min(len(all_steps) + 1, 32)):
        previous = len(names)
        for step in all_steps:
            action = str(step.get('type', '')).lower()
            config = step.get('config') if isinstance(step.get('config'), dict) else {}
            if action == 'api_request':
                extracts = step.get('extracts', config.get('extracts', []))
                if isinstance(extracts, list):
                    for extract in extracts:
                        if not isinstance(extract, dict):
                            continue
                        name = extract.get('name')
                        path = extract.get('path', '')
                        if isinstance(name, str) and (
                            _SECRET_NAME.search(str(name)) or _SECRET_NAME.search(str(path))
                        ):
                            names.add(name)
            elif action == 'extract_output':
                name = step.get('name', config.get('name'))
                source = step.get('source', config.get('source'))
                path = step.get('path', config.get('path', ''))
                if isinstance(name, str) and (
                    is_sensitive(source)
                    or _SECRET_NAME.search(str(path))
                    or _SECRET_NAME.search(name)
                ):
                    names.add(name)
            if action != 'set_variable':
                continue
            name = config.get('name', step.get('name'))
            value = step.get('value', config.get('value'))
            if isinstance(name, str) and is_sensitive(value):
                names.add(name)
        if len(names) == previous:
            break
    else:
        raise SensitiveFlowError('敏感变量引用链过深')

    for step in all_steps:
        if str(step.get('type', '')).lower() not in {'input', 'text'}:
            continue
        config = step.get('config') if isinstance(step.get('config'), dict) else None
        value = _input_expression(step)
        containers = [step] + ([config] if config is not None else [])
        if any(container.get('sensitive') is True for container in containers):
            for container in containers:
                for field in ('value', 'text'):
                    if field in container and not _safe_sensitive_input_value(container[field]):
                        raise SensitiveFlowError('敏感输入配置无效')
        if is_sensitive(value):
            if config is not None:
                config['sensitive'] = True
            else:
                step['sensitive'] = True
    return result
