import pytest

from apps.app_automation.models import AppTestCase
from apps.app_automation.serializers import AppTestCaseSerializer


def validate(payload, *, instance=None, partial=False):
    serializer = AppTestCaseSerializer(instance=instance, data=payload, partial=partial)
    return serializer, serializer.is_valid()


def nested_flow(step, location):
    if location == 'top_level':
        return [step]
    if location == 'legacy_control':
        return [{'type': 'if', 'then_steps': [step]}]
    if location == 'config_control':
        return [{'type': 'if', 'config': {'else_steps': [step]}}]
    if location == 'inline_steps':
        return [{'kind': 'custom', 'type': 'inline_fixture', 'steps': [step]}]
    return [{'kind': 'custom', 'type': 'inline_fixture', 'config': {'finally_steps': [step]}}]


def test_sensitive_variables_accept_only_exact_secret_aliases_and_empty_values():
    variables = [
        {'name': 'password', 'sensitive': True, 'value': 'secret://login.password-1'},
        {'name': 'empty', 'sensitive': True, 'value': ''},
        {'name': 'unset', 'sensitive': True, 'value': None},
        {'name': 'legacy', 'value': 'synthetic-legacy-value'},
        {'name': 'legacy_object', 'value': {'nested': 'legacy-value'}},
    ]

    serializer, valid = validate({'name': 'synthetic case', 'variables': variables})

    assert valid, serializer.errors
    assert serializer.validated_data['variables'] == variables


@pytest.mark.parametrize('private_value', [
    'synthetic-private-password',
    ' secret://login ',
    'secret://bad/alias',
    '{{local.password}}',
    {'nested': 'synthetic-private-password'},
])
def test_sensitive_variables_reject_non_alias_values_without_echoing(private_value, caplog):
    serializer, valid = validate({
        'name': 'synthetic case',
        'variables': [{'name': 'password', 'sensitive': True, 'value': private_value}],
    })

    assert not valid
    assert 'variables' in serializer.errors
    assert 'synthetic-private-password' not in str(serializer.errors)
    assert 'synthetic-private-password' not in caplog.text


@pytest.mark.parametrize('location', [
    'top_level', 'legacy_control', 'config_control', 'inline_steps', 'inline_config',
])
def test_sensitive_input_steps_reject_plaintext_at_supported_depths(location, caplog):
    private_value = 'synthetic-private-input'
    step = {'type': 'input', 'sensitive': True, 'value': private_value}
    serializer, valid = validate({
        'name': 'synthetic case',
        'ui_flow': nested_flow(step, location),
    })

    assert not valid
    assert 'ui_flow' in serializer.errors
    assert private_value not in str(serializer.errors)
    assert private_value not in caplog.text


@pytest.mark.parametrize('location', ['top_level', 'config_control', 'inline_config'])
def test_sensitive_text_steps_reject_direct_secret_references(location):
    step = {'type': 'text', 'config': {'sensitive': True, 'text': 'secret://login.password'}}
    serializer, valid = validate({
        'name': 'synthetic case',
        'ui_flow': nested_flow(step, location),
    })

    assert not valid
    assert 'ui_flow' in serializer.errors
    assert 'secret://login.password' not in str(serializer.errors)


@pytest.mark.django_db
def test_sensitive_steps_accept_exact_variable_references_and_empty_values():
    flow = [
        {'type': 'input', 'sensitive': True, 'value': '{{local.password}}'},
        {'type': 'text', 'config': {'sensitive': True, 'text': '${global.pin}'}},
        {'type': 'if', 'then_steps': [
            {'type': 'input', 'config': {'sensitive': True, 'value': ''}},
        ]},
        {'type': 'if', 'config': {'else_steps': [
            {'kind': 'custom', 'type': 'inline_fixture', 'steps': [
                {'type': 'text', 'sensitive': True, 'text': '{{local.profile.password}}'},
            ]},
        ]}},
        {'kind': 'custom', 'type': 'inline_fixture', 'config': {'finally_steps': [
            {'type': 'text', 'config': {'sensitive': True, 'text': '{{outputs.last.value}}'}},
        ]}},
    ]

    serializer, valid = validate({'name': 'synthetic case', 'ui_flow': flow})

    assert valid, serializer.errors
    assert serializer.validated_data['ui_flow'] == flow


def test_unmarked_legacy_variables_and_steps_are_preserved():
    variables = [{'name': 'legacy', 'value': 'synthetic-legacy-password'}]
    flow = [
        {'type': 'input', 'value': 'synthetic-legacy-input'},
        {'type': 'text', 'config': {'text': 'secret://legacy-value'}},
    ]

    serializer, valid = validate({
        'name': 'synthetic case', 'variables': variables, 'ui_flow': flow,
    })

    assert valid, serializer.errors
    assert serializer.validated_data['variables'] == variables
    assert serializer.validated_data['ui_flow'] == flow


def test_partial_update_validates_only_supplied_fields():
    instance = AppTestCase(
        name='existing case',
        variables=[{'name': 'password', 'sensitive': True, 'value': 'stored-private-value'}],
        ui_flow=[{'type': 'input', 'sensitive': True, 'value': 'stored-private-input'}],
    )

    serializer, valid = validate({'name': 'renamed case'}, instance=instance, partial=True)

    assert valid, serializer.errors

    serializer, valid = validate(
        {'variables': [{'name': 'legacy', 'value': 'legacy-value'}]},
        instance=instance,
        partial=True,
    )
    assert valid, serializer.errors


def test_sensitive_flow_rejects_nesting_beyond_the_bound():
    flow = [{'type': 'input', 'sensitive': True, 'value': '{{local.password}}'}]
    for _ in range(17):
        flow = [{'type': 'if', 'config': {'then_steps': flow}}]

    serializer, valid = validate({'name': 'synthetic case', 'ui_flow': flow})

    assert not valid
    assert 'ui_flow' in serializer.errors
    assert 'local.password' not in str(serializer.errors)
