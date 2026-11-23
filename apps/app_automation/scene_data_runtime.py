"""Bind authorized APP scene data and server-owned secrets for one execution."""
from __future__ import annotations

import re

from django.conf import settings

from .access import project_accessible
from .dataset_utils import DatasetError, bind_row_variables, select_dataset_row


class SceneDataError(ValueError):
    """A dataset, access grant or secret binding is invalid."""


def _secret_reference(value):
    return isinstance(value, str) and value.startswith('secret://')


_SECRET_REFERENCE = re.compile(r'secret://([A-Za-z0-9][A-Za-z0-9_.-]{0,127})\Z')
_SECRET_ALIAS = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_PROJECT_ID = re.compile(r'[1-9][0-9]{0,19}\Z')


def _configured_aliases(project_id):
    config = getattr(settings, 'APP_SCENE_SECRET_ALIASES', {})
    if not isinstance(config, dict) or set(config) - {'projects', 'global'}:
        raise SceneDataError('服务端密钥配置无效')
    projects = config.get('projects', {})
    shared = config.get('global', {})
    if not isinstance(projects, dict) or len(projects) > 256:
        raise SceneDataError('服务端密钥配置无效')

    def valid_alias_map(values):
        return (
            isinstance(values, dict)
            and len(values) <= 256
            and all(
                isinstance(alias, str)
                and _SECRET_ALIAS.fullmatch(alias)
                and isinstance(value, str)
                and value.strip()
                and len(value) <= 16_384
                for alias, value in values.items()
            )
        )

    if (not valid_alias_map(shared)
            or any(not isinstance(key, str) or not _PROJECT_ID.fullmatch(key)
                   or not valid_alias_map(values)
                   for key, values in projects.items())
            or sum(len(values) for values in projects.values()) + len(shared) > 4096):
        raise SceneDataError('服务端密钥配置无效')
    aliases = dict(shared)
    if project_id is not None:
        project_key = str(project_id)
        if not _PROJECT_ID.fullmatch(project_key):
            raise SceneDataError('服务端密钥配置无效')
        aliases.update(projects.get(project_key, {}))
    return aliases


def _resolve_secret_alias(execution, test_case, alias, aliases):
    project = getattr(test_case, 'project', None)
    project_id = getattr(test_case, 'project_id', None)
    if (project is None or project_id is None or str(project.pk) != str(project_id)
            or execution.user is None or not project_accessible(execution.user, project)):
        raise SceneDataError('数据集或密钥别名配置无效')
    try:
        return aliases[alias]
    except KeyError:
        raise SceneDataError('数据集或密钥别名配置无效') from None


def resolve_secret_reference(execution, test_case, value):
    """Resolve one exact secret reference in the current execution's project."""
    match = _SECRET_REFERENCE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise SceneDataError('数据集或密钥别名配置无效')
    aliases = _configured_aliases(getattr(test_case, 'project_id', None))
    return _resolve_secret_alias(execution, test_case, match.group(1), aliases)


def execution_variables(execution, test_case):
    """Return in-memory variables; never log or persist resolved secret values.

    APP_SCENE_SECRET_ALIASES is a server-owned project-scoped mapping from
    permitted aliases to secret values. Clients supply aliases only; names are
    never interpreted as environment variable names. Dataset project and user
    membership are rechecked when the worker starts.
    """
    variables = test_case.variables or []
    if not isinstance(variables, list):
        raise SceneDataError('场景变量配置无效')
    row = {}
    sensitive_columns = set()
    if execution.dataset_id is not None:
        dataset = execution.dataset
        if (dataset is None or test_case.project_id is None or
                dataset.project_id != test_case.project_id or execution.user is None or
                not project_accessible(execution.user, test_case.project)):
            raise SceneDataError('数据集不存在、项目不匹配或执行者已无访问权限')
        if not isinstance(dataset.sensitive_columns, list) or any(
            not isinstance(item, str) for item in dataset.sensitive_columns
        ):
            raise SceneDataError('数据集敏感列配置无效')
        sensitive_columns = set(dataset.sensitive_columns)
        if type(execution.dataset_row_index) is not int or execution.dataset_row_index < 0:
            raise SceneDataError('数据集行号无效')
        try:
            row = select_dataset_row(dataset.rows, index=execution.dataset_row_index)
        except DatasetError:
            raise SceneDataError('数据集行不存在或内容无效') from None

    aliases = _configured_aliases(test_case.project_id)
    sensitive_names = set(sensitive_columns)
    for item in variables:
        if isinstance(item, dict):
            name = item.get('name')
            if isinstance(name, str) and (item.get('sensitive') is True or _secret_reference(item.get('value'))):
                sensitive_names.add(name)
    for name, value in row.items():
        if _secret_reference(value):
            sensitive_names.add(name)
    try:
        bound = bind_row_variables(
            row, variables, secret_aliases=set(aliases),
            secret_resolver=lambda alias: _resolve_secret_alias(execution, test_case, alias, aliases),
        )
    except (DatasetError, TypeError, ValueError):
        raise SceneDataError('数据集或密钥别名配置无效') from None
    for item in bound:
        if item.get('name') in sensitive_names:
            item['sensitive'] = True
    return bound
