"""Bind dataset rows while preserving legacy scene-variable structures."""
from __future__ import annotations

import copy
from types import SimpleNamespace

from .scene_data_runtime import SceneDataError, execution_variables, resolve_secret_reference


def compatible_execution_variables(execution, test_case):
    """Overlay a dataset row without rejecting legacy JSON values/metadata."""
    original = test_case.variables or []
    if not isinstance(original, list):
        raise SceneDataError('场景变量配置无效')
    proxy_case = SimpleNamespace(
        variables=[], project_id=getattr(test_case, 'project_id', None),
        project=getattr(test_case, 'project', None),
    )
    row_items = execution_variables(execution, proxy_case)
    rows = {item['name']: item for item in row_items}

    def resolve_nested(value, depth=0):
        if depth > 32:
            raise SceneDataError('场景变量嵌套过深')
        if isinstance(value, str) and value.startswith('secret://'):
            return resolve_secret_reference(execution, test_case, value), True
        if isinstance(value, (list, dict)):
            sensitive = False
            result = {} if isinstance(value, dict) else []
            iterator = value.items() if isinstance(value, dict) else enumerate(value)
            for key, child in iterator:
                resolved, child_sensitive = resolve_nested(child, depth + 1)
                sensitive = sensitive or child_sensitive
                if isinstance(result, dict):
                    result[key] = resolved
                else:
                    result.append(resolved)
            return result, sensitive
        return value, False

    result = []
    seen = set()
    for raw in original:
        item = copy.deepcopy(raw)
        if not isinstance(item, dict):
            result.append(item)
            continue
        name = item.get('name')
        if not isinstance(name, str) or not name:
            result.append(item)
            continue
        seen.add(name)
        if name in rows:
            item['value'] = rows[name]['value']
            if rows[name].get('sensitive') is True:
                item['sensitive'] = True
        else:
            item['value'], contains_secret = resolve_nested(item.get('value'))
            if contains_secret:
                item['sensitive'] = True
        result.append(item)
    for item in row_items:
        if item['name'] not in seen:
            result.append(item)
    return result
