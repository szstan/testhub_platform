"""Expand project-scoped reusable APP scene components at execution time."""
from __future__ import annotations

import copy
import json
import re

from .access import project_accessible
from .component_templates import ComponentTemplateError, expand_steps
from .models import AppProjectComponent


class SceneComponentError(ValueError):
    """A reusable scene component cannot be safely expanded."""


_STEP_FIELDS = ('steps', 'then_steps', 'else_steps', 'try_steps', 'catch_steps', 'finally_steps')
_MAX_STEPS = 1000
_MAX_DEPTH = 16
_MAX_BYTES = 1_048_576
_SCENE_VARIABLE_REFERENCE = re.compile(
    r'(?:\{\{\s*([A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*\s*\}\}'
    r'|\$\{\s*([A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*\s*\})\Z'
)
_PARAM_REFERENCE = re.compile(r'^\{\{\s*params\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$')
_PARAM_TOKEN = re.compile(r'\{\{\s*params\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}')
_SENSITIVE_NAME = re.compile(r'password|passwd|secret|token|credential|api[_-]?key|密码|密钥|令牌', re.I)


def _is_sensitive_parameter_spec(spec):
    return isinstance(spec, dict) and any(
        spec.get(flag) is True for flag in ('sensitive', 'secret', 'writeOnly')
    )


def _empty_sensitive_value(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _safe_sensitive_value(value):
    if _empty_sensitive_value(value):
        return True
    if not isinstance(value, str):
        return False
    match = _SCENE_VARIABLE_REFERENCE.fullmatch(value)
    return bool(match and (match.group(1) or match.group(2)) != 'params')


def _parameter_specs(definition):
    schema = definition.get('schema', {})
    defaults = definition.get('default_config', {})
    if not isinstance(schema, dict) or not isinstance(defaults, dict):
        raise SceneComponentError('组件参数配置无效')
    properties = schema.get('properties', {})
    if not isinstance(properties, dict):
        raise SceneComponentError('组件参数配置无效')
    return properties, defaults


_SCHEMA_BRANCH_KEYWORDS = frozenset({
    '$ref', '$dynamicRef', 'patternProperties', 'propertyNames', 'unevaluatedProperties',
    'unevaluatedItems', 'additionalItems', 'dependencies', 'dependentSchemas',
    'allOf', 'anyOf', 'oneOf', 'not', 'if', 'then', 'else', 'prefixItems', 'contains',
})
_SCHEMA_TYPES = frozenset({'array', 'boolean', 'integer', 'null', 'number', 'object', 'string'})
_MAX_VALIDATION_NODES = 100_000


def _validation_failure():
    raise SceneComponentError('组件参数配置无效')


def _check_schema_node(spec, budget, depth=0):
    if depth > _MAX_DEPTH or type(spec) is not dict:
        _validation_failure()
    budget['nodes'] += 1
    if budget['nodes'] > _MAX_VALIDATION_NODES:
        _validation_failure()
    if any(keyword in spec for keyword in _SCHEMA_BRANCH_KEYWORDS):
        _validation_failure()
    if any(flag in spec and type(spec[flag]) is not bool
           for flag in ('sensitive', 'secret', 'writeOnly')):
        _validation_failure()
    declared_type = spec.get('type')
    if declared_type is not None:
        types = declared_type if type(declared_type) is list else [declared_type]
        if not types or any(type(item) is not str or item not in _SCHEMA_TYPES for item in types):
            _validation_failure()
    if 'required' in spec and (
            type(spec['required']) is not list
            or any(type(name) is not str for name in spec['required'])):
        _validation_failure()
    properties = spec.get('properties', {})
    if type(properties) is not dict or any(type(name) is not str for name in properties):
        _validation_failure()
    for child in properties.values():
        _check_schema_node(child, budget, depth + 1)
    if 'items' in spec:
        _check_schema_node(spec['items'], budget, depth + 1)
    additional = spec.get('additionalProperties', True)
    if type(additional) is dict:
        _check_schema_node(additional, budget, depth + 1)
    elif type(additional) is not bool:
        _validation_failure()


def _schema_container_type(spec, expected):
    if spec is None:
        return False
    declared = spec.get('type')
    return (
        'properties' in spec and expected == 'object'
        or 'items' in spec and expected == 'array'
        or declared == expected
        or type(declared) is list and expected in declared
    )


def _schema_contains_sensitive(spec, name_sensitive=False, depth=0):
    if depth > _MAX_DEPTH or type(spec) is not dict:
        _validation_failure()
    if name_sensitive or _is_sensitive_parameter_spec(spec):
        return True
    properties = spec.get('properties', {})
    if type(properties) is not dict:
        _validation_failure()
    for name, child in properties.items():
        if type(name) is not str or type(child) is not dict:
            _validation_failure()
        if _schema_contains_sensitive(child, bool(_SENSITIVE_NAME.search(name)), depth + 1):
            return True
    if 'items' in spec and _schema_contains_sensitive(spec['items'], depth=depth + 1):
        return True
    additional = spec.get('additionalProperties', True)
    return type(additional) is dict and _schema_contains_sensitive(additional, depth=depth + 1)


def _empty_taint():
    return {'root': set(), 'schemas': {}}


def _merge_taint(target, source):
    target['root'].update(source['root'])
    for name, specs in source['schemas'].items():
        existing = target['schemas'].setdefault(name, [])
        for spec in specs:
            if spec not in existing:
                existing.append(spec)
    return target


def _validate_sensitive_value(value, spec, *, name_sensitive=False, allow_parameter_references=False,
                              budget=None, depth=0, sensitive_names=()):
    if depth > _MAX_DEPTH:
        _validation_failure()
    if budget is not None:
        budget['nodes'] += 1
        if budget['nodes'] > _MAX_VALIDATION_NODES:
            _validation_failure()

    sensitive = name_sensitive or _is_sensitive_parameter_spec(spec)
    if sensitive:
        if _safe_sensitive_value(value):
            return _empty_taint()
        if allow_parameter_references and isinstance(value, str):
            parameter = _PARAM_REFERENCE.fullmatch(value)
            if parameter:
                taint = _empty_taint()
                taint['root'].add(parameter.group(1))
                return taint
        _validation_failure()

    if isinstance(value, str):
        if allow_parameter_references:
            parameter = _PARAM_REFERENCE.fullmatch(value)
            if parameter:
                if spec is not None and _schema_contains_sensitive(spec):
                    taint = _empty_taint()
                    taint['schemas'][parameter.group(1)] = [spec]
                    return taint
                return _empty_taint()
        if (spec is not None and (
                'properties' in spec or 'items' in spec
                or _schema_container_type(spec, 'object') or _schema_container_type(spec, 'array'))
                and not _safe_sensitive_value(value)):
            _validation_failure()
        return _empty_taint()

    if type(value) is dict:
        if spec is not None and 'items' in spec:
            _validation_failure()
        properties = spec.get('properties', {}) if spec is not None else {}
        additional = spec.get('additionalProperties', True) if spec is not None else True
        taint = _empty_taint()
        for name, child_value in value.items():
            if type(name) is not str:
                _validation_failure()
            child_spec = properties.get(name)
            if child_spec is None and type(additional) is dict:
                child_spec = additional
            if child_spec is None and additional is False:
                _validation_failure()
            _merge_taint(taint, _validate_sensitive_value(
                child_value, child_spec,
                name_sensitive=(bool(_SENSITIVE_NAME.search(name))
                                or (depth == 0 and name in sensitive_names)),
                allow_parameter_references=allow_parameter_references,
                budget=budget, depth=depth + 1, sensitive_names=sensitive_names,
            ))
        return taint

    if type(value) is list:
        if spec is not None and 'properties' in spec:
            _validation_failure()
        item_spec = spec.get('items') if spec is not None else None
        taint = _empty_taint()
        for item in value:
            _merge_taint(taint, _validate_sensitive_value(
                item, item_spec, allow_parameter_references=allow_parameter_references,
                budget=budget, depth=depth + 1, sensitive_names=sensitive_names,
            ))
        return taint

    if spec is not None and (
            'properties' in spec or 'items' in spec
            or _schema_container_type(spec, 'object') or _schema_container_type(spec, 'array')):
        _validation_failure()
    return _empty_taint()


def _validate_sensitive_mapping(values, properties, sensitive_names, extra_schemas,
                                *, additional_properties=True,
                                allow_parameter_references=False, budget=None):
    if values is None:
        values = {}
    if type(values) is not dict:
        _validation_failure()
    taint = _empty_taint()
    for name, value in values.items():
        if type(name) is not str:
            _validation_failure()
        specs = []
        if name in properties:
            specs.append(properties[name])
        elif type(additional_properties) is dict:
            specs.append(additional_properties)
        elif additional_properties is False:
            _validation_failure()
        for spec in extra_schemas.get(name, ()):
            if spec not in specs:
                specs.append(spec)
        if not specs:
            specs = [None]
        for spec in specs:
            _merge_taint(taint, _validate_sensitive_value(
                value, spec,
                name_sensitive=(name in sensitive_names or bool(_SENSITIVE_NAME.search(name))),
                allow_parameter_references=allow_parameter_references,
                budget=budget,
            ))
    return taint


def _validate_sensitive_schema_constraints(spec, budget, *, name_sensitive=False,
                                           sensitive_names=()):
    sensitive = name_sensitive or _is_sensitive_parameter_spec(spec)

    def validate(value):
        if sensitive and type(value) in (dict, list) and not value:
            budget['nodes'] += 1
            if budget['nodes'] > _MAX_VALIDATION_NODES:
                _validation_failure()
            return
        _validate_sensitive_value(
            value, spec, name_sensitive=sensitive, budget=budget,
            sensitive_names=sensitive_names,
        )

    for keyword in ('enum', 'examples'):
        if keyword not in spec:
            continue
        values = spec[keyword]
        if type(values) is not list:
            _validation_failure()
        for value in values:
            validate(value)

    if 'const' in spec:
        validate(spec['const'])


def _validate_schema_defaults(spec, *, name_sensitive=False, budget=None, depth=0,
                              sensitive_names=()):
    if depth > _MAX_DEPTH or type(spec) is not dict:
        _validation_failure()
    if budget is None:
        budget = {'nodes': 0}
    sensitive = name_sensitive or _is_sensitive_parameter_spec(spec)
    if 'default' in spec:
        _validate_sensitive_value(spec['default'], spec, name_sensitive=sensitive, budget=budget)
    _validate_sensitive_schema_constraints(
        spec, budget, name_sensitive=sensitive,
        sensitive_names=sensitive_names if depth == 0 else (),
    )
    properties = spec.get('properties', {})
    for name, child in properties.items():
        child_sensitive = bool(_SENSITIVE_NAME.search(name)) or (depth == 0 and name in sensitive_names)
        _validate_schema_defaults(
            child, name_sensitive=(sensitive or child_sensitive),
            budget=budget, depth=depth + 1,
        )
    if 'items' in spec:
        _validate_schema_defaults(spec['items'], name_sensitive=sensitive, budget=budget, depth=depth + 1)
    additional = spec.get('additionalProperties', True)
    if type(additional) is dict:
        _validate_schema_defaults(additional, name_sensitive=sensitive, budget=budget, depth=depth + 1)


def validate_sensitive_component_data(schema, configured_defaults, *, sensitive_names=(),
                                      supplied_values=(), extra_schemas=None,
                                      allow_parameter_references=False):
    """Validate sensitive defaults and values recursively; return template taint when requested."""
    if type(schema) is not dict or type(configured_defaults) is not dict:
        _validation_failure()
    properties = schema.get('properties', {})
    if type(properties) is not dict:
        _validation_failure()
    required = schema.get('required', [])
    if type(required) is not list or any(type(name) is not str for name in required):
        _validation_failure()
    if any(name not in properties for name in required):
        _validation_failure()
    if extra_schemas is None:
        extra_schemas = {}
    elif type(extra_schemas) is not dict:
        _validation_failure()
    normalized_extras = {}
    for name, specs in extra_schemas.items():
        if type(name) is not str or type(specs) is not list:
            _validation_failure()
        filtered = [spec for spec in specs if name not in properties or spec is not properties[name]]
        if filtered:
            normalized_extras[name] = filtered
    extra_schemas = normalized_extras
    supplied_values = list(supplied_values)
    payload = {'schema': schema, 'default_config': configured_defaults,
               'supplied_values': supplied_values, 'extra_schemas': extra_schemas}
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        _validation_failure()
    if len(encoded) > _MAX_BYTES:
        _validation_failure()

    budget = {'nodes': 0}
    _check_schema_node(schema, budget)
    for spec in extra_schemas.values():
        if type(spec) is not list:
            _validation_failure()
        for item in spec:
            _check_schema_node(item, budget)

    sensitive_names = set(sensitive_names)
    sensitive_names.update(name for name in configured_defaults
                           if type(name) is str and _SENSITIVE_NAME.search(name))
    extras = extra_schemas
    taint = _empty_taint()
    _validate_schema_defaults(schema, budget=budget, sensitive_names=sensitive_names)
    for name, spec_list in extras.items():
        name_sensitive = name in sensitive_names or bool(_SENSITIVE_NAME.search(name))
        for spec in spec_list:
            _validate_schema_defaults(spec, name_sensitive=name_sensitive, budget=budget)

    root_additional = schema.get('additionalProperties', True)
    _merge_taint(taint, _validate_sensitive_mapping(
        configured_defaults, properties, sensitive_names, extras,
        additional_properties=root_additional,
        allow_parameter_references=False, budget=budget,
    ))
    for supplied in supplied_values:
        _merge_taint(taint, _validate_sensitive_mapping(
            supplied, properties, sensitive_names, extras,
            additional_properties=root_additional,
            allow_parameter_references=allow_parameter_references, budget=budget,
        ))
    return taint


def validate_component_definition_sensitive_data(schema, configured_defaults, steps):
    """Component-create entry point sharing the runtime parameter sensitivity rules."""
    sensitive_names = _sensitive_template_parameters(steps)
    return validate_sensitive_component_data(
        schema, configured_defaults, sensitive_names=sensitive_names,
    )


def _sensitive_template_parameters(steps):

    sensitive = set()

    def visit(sequence, depth=0):
        if depth > _MAX_DEPTH or not isinstance(sequence, list):
            raise SceneComponentError('组件嵌套过深或步骤格式无效')
        for step in sequence:
            if not isinstance(step, dict):
                raise SceneComponentError('场景步骤格式无效')
            action = step.get('type')
            config = step.get('config')
            containers = [step]
            if isinstance(config, dict):
                containers.append(config)
            if (isinstance(action, str) and action.lower() in {'input', 'text'}
                    and any(container.get('sensitive') is True for container in containers)):
                for container in containers:
                    for key in ('value', 'text'):
                        if key not in container:
                            continue
                        value = container[key]
                        if _empty_sensitive_value(value) or _safe_sensitive_value(value):
                            continue
                        if isinstance(value, str):
                            parameter = _PARAM_REFERENCE.fullmatch(value)
                            if parameter:
                                sensitive.add(parameter.group(1))
                                continue
                        raise SceneComponentError('敏感组件输入配置无效')

            nested_containers = [step]
            if step.get('kind') == 'custom':
                if isinstance(step.get('steps'), list):
                    nested_containers.append({'steps': step['steps']})
            elif isinstance(config, dict):
                nested_containers.append(config)
            for nested_container in nested_containers:
                for field in _STEP_FIELDS:
                    nested = nested_container.get(field)
                    if nested is not None:
                        if not isinstance(nested, list):
                            raise SceneComponentError('嵌套步骤格式无效')
                        visit(nested, depth + 1)

    visit(steps)
    return sensitive


def _substitute_template_params(value, params):
    if isinstance(value, dict):
        return {key: _substitute_template_params(child, params) for key, child in value.items()}
    if isinstance(value, list):
        return [_substitute_template_params(child, params) for child in value]
    if not isinstance(value, str):
        return value

    exact = _PARAM_REFERENCE.fullmatch(value)
    if exact and exact.group(1) in params:
        return copy.deepcopy(params[exact.group(1)])

    def replace(match):
        name = match.group(1)
        if name not in params:
            return match.group(0)
        child = params[name]
        if child is None:
            return 'null'
        if child is True:
            return 'true'
        if child is False:
            return 'false'
        if isinstance(child, (str, int, float)):
            return str(child)
        raise SceneComponentError('组件参数配置无效')

    return _PARAM_TOKEN.sub(replace, value)


def _definition_defaults(properties, configured_defaults):
    result = {}
    for name, spec in properties.items():
        if isinstance(spec, dict) and 'default' in spec:
            result[name] = copy.deepcopy(spec['default'])
    result.update(copy.deepcopy(configured_defaults))
    return result


def _template_parameter_names(value):
    names = set()
    if isinstance(value, dict):
        for child in value.values():
            names.update(_template_parameter_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_template_parameter_names(child))
    elif isinstance(value, str):
        names.update(match.group(1) for match in _PARAM_TOKEN.finditer(value))
    return names


def _validate_component_reference(source, definition, sensitive_parameters=None):
    properties, configured_defaults = _parameter_specs(definition)
    if isinstance(sensitive_parameters, dict):
        sensitive = set(sensitive_parameters.get('sensitive', ()))
        extra_schemas = sensitive_parameters.get('schemas', {})
    elif sensitive_parameters is None:
        sensitive = {
            name for name, spec in properties.items()
            if isinstance(name, str) and (
                _SENSITIVE_NAME.search(name) or _is_sensitive_parameter_spec(spec)
            )
        }
        sensitive.update(name for name in configured_defaults if _SENSITIVE_NAME.search(str(name)))
        sensitive.update(_sensitive_template_parameters(definition.get('steps')))
        extra_schemas = {}
    else:
        sensitive = set(sensitive_parameters)
        extra_schemas = {}

    supplied_values = []
    for key in ('params', 'config'):
        if key not in source:
            continue
        supplied = source[key]
        if supplied is None:
            supplied = {}
        if not isinstance(supplied, dict):
            raise SceneComponentError('组件参数配置无效')
        supplied_values.append(supplied)
    validate_sensitive_component_data(
        definition.get('schema', {}), configured_defaults,
        sensitive_names=sensitive, supplied_values=supplied_values,
        extra_schemas=extra_schemas,
    )

    defaults = _definition_defaults(properties, configured_defaults)
    selected = source.get('params', source.get('config', {}))
    if selected is None:
        selected = {}
    if not isinstance(selected, dict):
        raise SceneComponentError('组件参数配置无效')
    effective = defaults
    effective.update(copy.deepcopy(selected))
    return effective


def validate_project_component_references(flow, project, user):
    """Reject persisted sensitive literals in project refs, including nested refs."""
    if not isinstance(flow, list):
        return

    cache = {}
    access = {'allowed': None}
    budget = {'steps': 0}

    def resolve(component_type, version):
        if project is None or not isinstance(component_type, str) or not component_type:
            raise SceneComponentError('项目组件不存在或无权访问')
        if access['allowed'] is None:
            access['allowed'] = bool(
                user is not None
                and getattr(user, 'is_authenticated', False)
                and project_accessible(user, project)
            )
        if not access['allowed']:
            raise SceneComponentError('项目组件不存在或无权访问')
        identity = (component_type, version)
        if identity not in cache:
            component = AppProjectComponent.objects.filter(
                project_id=project.pk, type=component_type, version=version,
            ).first()
            if component is None:
                raise SceneComponentError('项目组件不存在或无权访问')
            cache[identity] = {
                'project_id': component.project_id,
                'type': component.type,
                'version': component.version,
                'schema': component.schema,
                'default_config': component.default_config,
                'steps': component.steps,
            }
        return cache[identity]

    sensitivity_cache = {}
    sensitivity_budget = {'steps': 0}

    def component_sensitive_contract(component_type, version, definition, active, depth):
        identity = (component_type, version)
        if depth > _MAX_DEPTH or identity in active:
            raise SceneComponentError('组件嵌套过深或存在循环引用')
        if identity in sensitivity_cache:
            return sensitivity_cache[identity]

        properties, configured_defaults = _parameter_specs(definition)
        sensitive = {
            name for name, spec in properties.items()
            if isinstance(name, str) and (
                _SENSITIVE_NAME.search(name) or _is_sensitive_parameter_spec(spec)
            )
        }
        sensitive.update(name for name in configured_defaults if _SENSITIVE_NAME.search(str(name)))
        sensitive.update(_sensitive_template_parameters(definition.get('steps')))
        inherited_schemas = {name: [spec] for name, spec in properties.items()}

        parameter_names = {
            name for name in properties
            if isinstance(name, str) and _PARAM_REFERENCE.fullmatch(f'{{{{params.{name}}}}}')
        }
        parameter_names.update(
            name for name in configured_defaults
            if isinstance(name, str) and _PARAM_REFERENCE.fullmatch(f'{{{{params.{name}}}}}')
        )
        parameter_names.update(_template_parameter_names(definition.get('steps')))
        symbolic_params = {name: f'{{{{params.{name}}}}}' for name in parameter_names}

        def inspect(sequence, params, flow_depth):
            if flow_depth > _MAX_DEPTH or not isinstance(sequence, list):
                raise SceneComponentError('组件嵌套过深或步骤格式无效')
            for original in sequence:
                sensitivity_budget['steps'] += 1
                if sensitivity_budget['steps'] > _MAX_STEPS or not isinstance(original, dict):
                    raise SceneComponentError('组件步骤数量超过限制')
                step = _substitute_template_params(original, params)
                if step.get('kind') == 'custom':
                    inline_steps = step.get('steps')
                    if inline_steps is not None and not isinstance(inline_steps, list):
                        raise SceneComponentError('组件步骤格式无效')
                    if isinstance(inline_steps, list):
                        inline_params = step.get('params', step.get('config', {}))
                        if inline_params is None:
                            inline_params = {}
                        if not isinstance(inline_params, dict):
                            raise SceneComponentError('组件参数配置无效')
                        child_params = dict(params)
                        child_params.update(inline_params)
                        inspect(inline_steps, child_params, flow_depth + 1)
                        continue
                    child_version = step.get('version')
                    if child_version is None:
                        continue
                    child_type = step.get('type')
                    if not isinstance(child_version, str) or not child_version:
                        raise SceneComponentError('项目组件版本无效')
                    child_definition = resolve(child_type, child_version)
                    child_contract = component_sensitive_contract(
                        child_type, child_version, child_definition, active + (identity,), depth + 1,
                    )
                    supplied_values = []
                    for field in ('params', 'config'):
                        if field not in step:
                            continue
                        supplied = step[field]
                        if supplied is None:
                            supplied = {}
                        if not isinstance(supplied, dict):
                            raise SceneComponentError('组件参数配置无效')
                        supplied_values.append(supplied)
                    taint = validate_sensitive_component_data(
                        child_definition.get('schema', {}), child_definition.get('default_config', {}),
                        sensitive_names=child_contract['sensitive'],
                        supplied_values=supplied_values,
                        extra_schemas=child_contract['schemas'],
                        allow_parameter_references=True,
                    )
                    sensitive.update(taint['root'])
                    for name, specs in taint['schemas'].items():
                        inherited = inherited_schemas.setdefault(name, [])
                        for spec in specs:
                            if spec not in inherited:
                                inherited.append(spec)
                    continue

                nested_containers = [step]
                config = step.get('config')
                if step.get('kind') != 'custom' and isinstance(config, dict):
                    nested_containers.append(config)
                for nested_container in nested_containers:
                    for field in _STEP_FIELDS:
                        nested = nested_container.get(field)
                        if nested is not None:
                            if not isinstance(nested, list):
                                raise SceneComponentError('嵌套步骤格式无效')
                            inspect(nested, params, flow_depth + 1)

        inspect(definition.get('steps'), symbolic_params, 0)
        validate_sensitive_component_data(
            definition.get('schema', {}), configured_defaults,
            sensitive_names=sensitive, extra_schemas=inherited_schemas,
        )
        contract = {'sensitive': frozenset(sensitive), 'schemas': inherited_schemas}
        sensitivity_cache[identity] = contract
        return contract

    def visit(sequence, params=None, active=(), depth=0, in_template=False):
        if depth > _MAX_DEPTH or not isinstance(sequence, list):
            raise SceneComponentError('组件嵌套过深或步骤格式无效')
        for original in sequence:
            budget['steps'] += 1
            if budget['steps'] > _MAX_STEPS:
                raise SceneComponentError('组件步骤数量超过限制')
            if not isinstance(original, dict):
                raise SceneComponentError('场景步骤格式无效')
            step = _substitute_template_params(original, params or {}) if params is not None else original
            if step.get('kind') == 'custom':
                inline_steps = step.get('steps')
                if inline_steps is not None and not isinstance(inline_steps, list):
                    raise SceneComponentError('组件步骤格式无效')
                if isinstance(inline_steps, list):
                    raw_params = step.get('params', step.get('config', {}))
                    if raw_params is None:
                        raw_params = {}
                    if not isinstance(raw_params, dict):
                        raise SceneComponentError('组件参数配置无效')
                    inline_params = dict(params or {})
                    inline_params.update(raw_params)
                    visit(inline_steps, inline_params if params is not None or raw_params else None,
                          active, depth + 1, in_template)
                    continue
                version = step.get('version')
                if version is None:
                    continue
                component_type = step.get('type')
                if not isinstance(version, str) or not version:
                    raise SceneComponentError('项目组件版本无效')
                definition = resolve(component_type, version)
                identity = (component_type, version)
                if identity in active:
                    raise SceneComponentError('组件引用循环')
                sensitive_contract = component_sensitive_contract(
                    component_type, version, definition, active, depth,
                )
                effective = _validate_component_reference(step, definition, sensitive_contract)
                visit(definition.get('steps'), effective, active + (identity,), depth + 1, True)
                continue

            action = step.get('type')
            config = step.get('config')
            containers = [step]
            if isinstance(config, dict):
                containers.append(config)
            if (in_template and isinstance(action, str) and action.lower() in {'input', 'text'}
                    and any(container.get('sensitive') is True for container in containers)):
                for container in containers:
                    for key in ('value', 'text'):
                        if key in container and not _safe_sensitive_value(container[key]):
                            raise SceneComponentError('敏感输入配置无效')

            nested_containers = [step]
            if isinstance(config, dict):
                nested_containers.append(config)
            for nested_container in nested_containers:
                for field in _STEP_FIELDS:
                    nested = nested_container.get(field)
                    if nested is not None:
                        if not isinstance(nested, list):
                            raise SceneComponentError('嵌套步骤格式无效')
                        visit(nested, params, active, depth + 1, in_template)

    visit(flow)


def _validate_expanded_sensitive_inputs(steps):
    budget = {'steps': 0}

    def visit(sequence, depth=0):
        if depth > _MAX_DEPTH or not isinstance(sequence, list):
            raise SceneComponentError('展开后的步骤格式无效')
        for step in sequence:
            budget['steps'] += 1
            if budget['steps'] > _MAX_STEPS or not isinstance(step, dict):
                raise SceneComponentError('展开后的步骤格式无效')
            action = step.get('type')
            config = step.get('config')
            containers = [step]
            if isinstance(config, dict):
                containers.append(config)
            if (isinstance(action, str) and action.lower() in {'input', 'text'}
                    and any(container.get('sensitive') is True for container in containers)):
                for container in containers:
                    for key in ('value', 'text'):
                        if key in container and not _safe_sensitive_value(container[key]):
                            raise SceneComponentError('展开后的敏感输入配置无效')

            nested_containers = [step]
            if step.get('kind') != 'custom' and isinstance(config, dict):
                nested_containers.append(config)
            for nested_container in nested_containers:
                for field in _STEP_FIELDS:
                    nested = nested_container.get(field)
                    if nested is not None:
                        if not isinstance(nested, list):
                            raise SceneComponentError('展开后的嵌套步骤格式无效')
                        visit(nested, depth + 1)

    visit(steps)


def expand_project_flow(execution, test_case, flow):
    """Expand version-pinned project refs; retain legacy custom wrappers unchanged.

    The caller must pass the execution's authenticated user. Membership is
    checked again at resolution, including after a task has been queued. Only
    project definitions are looked up here; legacy global/inline components
    remain under the existing runner's compatibility behavior.
    """
    try:
        payload = json.dumps(flow, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        raise SceneComponentError('场景流程格式无效') from None
    if not isinstance(flow, list) or len(payload) > _MAX_BYTES:
        raise SceneComponentError('场景流程格式无效或超过大小限制')

    validate_project_component_references(flow, test_case.project, execution.user)

    access_checked = {'allowed': None}

    def resolve(project_id, component_type, version):
        if execution.user is None or test_case.project_id != project_id:
            return None
        if access_checked['allowed'] is None:
            access_checked['allowed'] = project_accessible(execution.user, test_case.project)
        if not access_checked['allowed']:
            return None
        component = AppProjectComponent.objects.filter(
            project_id=project_id, type=component_type, version=version,
        ).first()
        if component is None:
            return None
        return {
            'project_id': component.project_id,
            'type': component.type,
            'version': component.version,
            'schema': component.schema,
            'default_config': component.default_config,
            'steps': component.steps,
        }

    budget = {'steps': 0, 'bytes': len(payload)}

    def visit_sequence(sequence, depth):
        if depth > _MAX_DEPTH or not isinstance(sequence, list):
            raise SceneComponentError('组件嵌套过深或步骤格式无效')
        output = []
        for source in sequence:
            if not isinstance(source, dict):
                raise SceneComponentError('场景步骤格式无效')
            if source.get('kind') == 'custom' and source.get('version') is not None:
                if test_case.project_id is None:
                    raise SceneComponentError('项目组件需要关联项目')
                try:
                    expanded = expand_steps(
                        [source], project_id=test_case.project_id, resolve_template=resolve,
                        max_depth=_MAX_DEPTH, max_steps=_MAX_STEPS, max_payload_bytes=_MAX_BYTES,
                    )
                except ComponentTemplateError:
                    raise SceneComponentError('项目组件不存在、无权访问或配置无效') from None
                _validate_expanded_sensitive_inputs(expanded)
                budget['bytes'] += len(json.dumps(expanded, ensure_ascii=False, allow_nan=False).encode('utf-8'))
                if budget['bytes'] > _MAX_BYTES:
                    raise SceneComponentError('展开后的场景超过大小限制')
                output.extend(visit_sequence(expanded, depth + 1))
                continue
            step = copy.deepcopy(source)
            budget['steps'] += 1
            if budget['steps'] > _MAX_STEPS:
                raise SceneComponentError('展开后的步骤数量超过限制')
            for field in _STEP_FIELDS:
                if field in step and step[field] is not None:
                    if not isinstance(step[field], list):
                        raise SceneComponentError('嵌套步骤格式无效')
                    step[field] = visit_sequence(step[field], depth + 1)
            config = step.get('config')
            # Legacy custom config is parameter data; its `steps` key is not
            # necessarily a nested flow and must retain the old behavior.
            if isinstance(config, dict) and step.get('kind') != 'custom':
                for field in _STEP_FIELDS:
                    if field in config and config[field] is not None:
                        if not isinstance(config[field], list):
                            raise SceneComponentError('嵌套步骤格式无效')
                        config[field] = visit_sequence(config[field], depth + 1)
            output.append(step)
        return output

    try:
        result = visit_sequence(flow, 0)
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')) > _MAX_BYTES:
            raise SceneComponentError('展开后的场景超过大小限制')
    except (RecursionError, TypeError, ValueError) as exc:
        if isinstance(exc, SceneComponentError):
            raise
        raise SceneComponentError('场景组件展开失败') from None
    return result
