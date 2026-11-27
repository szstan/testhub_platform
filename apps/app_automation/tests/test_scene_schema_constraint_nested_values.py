import pytest

from apps.app_automation.scene_component_runtime import (
    SceneComponentError,
    validate_sensitive_component_data,
)


_CONSTRAINTS = ('enum', 'const', 'examples')


def _constraint_value(keyword, value):
    return [value] if keyword in ('enum', 'examples') else value


def _nested_constraint_schema(location, keyword, candidate, *, sensitive=True):
    properties = {'password': {'type': 'string', 'sensitive': True}} if sensitive else {}
    if location == 'object':
        target = {'type': 'object', 'properties': properties}
        schema = {'properties': {'account_data': target}}
        value = candidate
    elif location == 'array':
        target = {'type': 'array', 'items': {
            'type': 'object', 'properties': properties,
        }}
        schema = {'properties': {'records': target}}
        value = [candidate]
    else:
        target = {'type': 'object', 'properties': {'account_data': {
            'type': 'object', 'properties': properties,
        }}}
        schema = target
        value = {'account_data': candidate}
    target[keyword] = _constraint_value(keyword, value)
    return schema


@pytest.mark.parametrize('location', ('object', 'array', 'root'))
@pytest.mark.parametrize('keyword', _CONSTRAINTS)
def test_non_sensitive_container_constraints_reject_nested_sensitive_literals(location, keyword):
    private_value = f'synthetic-private-{location}-{keyword}'
    schema = _nested_constraint_schema(
        location, keyword, {'password': private_value},
    )

    with pytest.raises(SceneComponentError) as exc_info:
        validate_sensitive_component_data(schema, {})

    assert private_value not in str(exc_info.value)


@pytest.mark.parametrize('location', ('object', 'array', 'root'))
@pytest.mark.parametrize('keyword', _CONSTRAINTS)
def test_non_sensitive_container_constraints_reject_undeclared_sensitive_keys(location, keyword):
    private_value = f'synthetic-private-undeclared-{location}-{keyword}'
    schema = _nested_constraint_schema(
        location, keyword, {'password': private_value}, sensitive=False,
    )

    with pytest.raises(SceneComponentError) as exc_info:
        validate_sensitive_component_data(schema, {})

    assert private_value not in str(exc_info.value)


def test_root_constraint_respects_template_sensitive_parameter_names():
    private_value = 'synthetic-private-template-parameter'
    schema = {'type': 'object', 'properties': {
        'account_data': {'type': 'object'},
    }, 'enum': [{'account_data': private_value}]}

    with pytest.raises(SceneComponentError):
        validate_sensitive_component_data(
            schema, {}, sensitive_names={'account_data'},
        )


def test_nested_non_sensitive_object_enum_choices_are_preserved():
    schema = {'properties': {'account_data': {
        'type': 'object', 'properties': {'status': {'type': 'string'}},
        'enum': [{'status': 'synthetic-ready'}, {'status': 'synthetic-paused'}],
    }}}

    validate_sensitive_component_data(schema, {})


def test_nested_sensitive_enum_variable_references_are_preserved():
    schema = {'properties': {'account_data': {
        'type': 'object', 'properties': {
            'password': {'type': 'string', 'sensitive': True},
        }, 'enum': [{'password': '{{local.password}}'}],
    }}}

    validate_sensitive_component_data(schema, {})


def test_non_sensitive_constraint_values_share_the_validation_node_budget(monkeypatch):
    from apps.app_automation import scene_component_runtime

    monkeypatch.setattr(scene_component_runtime, '_MAX_VALIDATION_NODES', 4)
    schema = {'properties': {'status': {
        'type': 'string', 'enum': ['ready', 'paused', 'running'],
    }}}

    with pytest.raises(SceneComponentError):
        validate_sensitive_component_data(schema, {})
