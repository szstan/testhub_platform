from collections import Counter
from pathlib import Path

import pytest
import yaml


PACK = Path(__file__).resolve().parents[3] / 'apps/core/management/commands/ui-component-pack.yaml'
LEGACY_TYPES = {
    'click', 'input', 'swipe', 'api_request', 'image_exists_click', 'image_exists_click_chain',
    'long_press', 'double_click', 'drag', 'swipe_to', 'assert', 'foreach_assert', 'wait', 'if',
    'loop', 'sequence', 'try', 'set_variable', 'unset_variable', 'extract_output', 'screenshot',
}
ADDED_TYPES = {
    'clear_input', 'key_event', 'hide_keyboard', 'app_start', 'app_stop', 'app_restart',
    'handle_permission', 'wait_element', 'assert_element', 'assert_text', 'assert_value',
    'assert_response', 'extract_text',
}


@pytest.fixture(scope='module')
def catalog():
    data = yaml.safe_load(PACK.read_text(encoding='utf-8'))
    return data['components']


def test_catalog_keeps_legacy_types_and_has_unique_identifiers(catalog):
    identifiers = [item['type'] for item in catalog]
    assert all(count == 1 for count in Counter(identifiers).values())
    assert LEGACY_TYPES | ADDED_TYPES <= set(identifiers)
    assert all(item.get('name') and item.get('category') for item in catalog)


def test_required_fields_and_defaults_match_schema_types(catalog):
    expected_types = {'string': str, 'number': (int, float), 'boolean': bool, 'array': list, 'object': dict}
    for item in catalog:
        schema = item['schema']
        properties = schema.get('properties', {})
        assert set(schema.get('required', [])) <= set(properties), item['type']
        for name, value in item.get('default_config', {}).items():
            spec = properties.get(name, {})
            kind = spec.get('type')
            if kind in expected_types:
                assert isinstance(value, expected_types[kind]), (item['type'], name)
                assert not (kind == 'number' and isinstance(value, bool)), (item['type'], name)
            if 'enum' in spec:
                assert value in spec['enum'], (item['type'], name)


def test_device_commands_are_declared_as_bounded_choices(catalog):
    definitions = {item['type']: item for item in catalog}
    assert set(definitions['key_event']['schema']['properties']['key']['enum']) == {
        'BACK', 'HOME', 'ENTER', 'TAB', 'DELETE', 'ESCAPE', 'APP_SWITCH', 'VOLUME_UP', 'VOLUME_DOWN',
    }
    assert set(definitions['handle_permission']['schema']['properties']['choice']['enum']) == {
        'allow', 'deny', 'while_in_use',
    }
    for name in ('key_event', 'hide_keyboard', 'app_start', 'app_stop', 'app_restart', 'handle_permission'):
        fields = definitions[name]['schema']['properties']
        assert not {'shell', 'command', 'adb_args'} & set(fields), name


def test_new_assertions_use_explicit_supported_selector_and_comparison_contracts(catalog):
    definitions = {item['type']: item for item in catalog}
    selector_types = set(definitions['assert_element']['schema']['properties']['selector_type']['enum'])
    assert {'image', 'resource_id', 'text', 'content_desc', 'class'} <= selector_types
    assert not {'pos', 'region'} & selector_types
    states = definitions['assert_element']['schema']['properties']['state']['enum']
    assert {'visible', 'hidden', 'enabled', 'checked', 'selected'} <= set(states)
    for name in ('assert_value', 'assert_response'):
        operators = definitions[name]['schema']['properties']['operator']['enum']
        assert {'==', '!=', '>', '>=', '<', '<=', 'contains', 'regex'} <= set(operators)
