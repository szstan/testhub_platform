import copy

import pytest

from apps.app_automation.scene_sensitive_flow import SensitiveFlowError, protect_sensitive_inputs


def test_secret_references_and_transitive_set_variables_mark_only_affected_inputs():
    variables = [{'name': 'pin', 'value': 'synthetic-pin-42', 'scope': 'local', 'sensitive': True},
                 {'name': 'city', 'value': '北京', 'scope': 'local'}]
    flow = [
        {'type': 'set_variable', 'config': {'name': 'copy_pin', 'value': '{{local.pin}}'}},
        {'type': 'if', 'config': {'then_steps': [
            {'type': 'input', 'config': {'value': 'prefix-{{local.copy_pin}}', 'sensitive': False}},
            {'type': 'input', 'config': {'value': '{{local.city}}'}},
        ], 'else_steps': []}},
    ]
    original = copy.deepcopy(flow)
    result = protect_sensitive_inputs(flow, variables)
    assert result[1]['config']['then_steps'][0]['config']['sensitive'] is True
    assert 'sensitive' not in result[1]['config']['then_steps'][1]['config']
    assert flow == original


def test_legacy_top_level_fields_and_known_secret_literals_are_marked():
    variables = [{'name': 'opaque', 'value': 'synthetic-private', 'sensitive': True}]
    flow = [{'type': 'input', 'value': 'synthetic-private', 'config': {'value': 'ignored'}}]
    assert protect_sensitive_inputs(flow, variables)[0]['config']['sensitive'] is True


def test_secret_alias_literal_is_rejected_without_echoing_alias():
    with pytest.raises(SensitiveFlowError) as error:
        protect_sensitive_inputs([{'type': 'input', 'config': {'value': 'secret://private_alias'}}], [])
    assert 'private_alias' not in str(error.value)


def test_output_token_references_are_conservatively_sensitive():
    flow = [{'type': 'input', 'config': {'value': '{{outputs.last.body.access_token}}'}}]
    assert protect_sensitive_inputs(flow, [])[0]['config']['sensitive'] is True
