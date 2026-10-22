from scripts.api_contract_check import find_breaking_changes


def test_openapi_contract_allows_additive_path_and_property():
    previous = {
        'paths': {'/api/items/': {'get': {'responses': {'200': {}}}}},
        'components': {'schemas': {'Item': {'type': 'object', 'properties': {'name': {'type': 'string'}}}}},
    }
    current = {
        'paths': {
            '/api/items/': {'get': {'responses': {'200': {}}}},
            '/api/items/{id}/': {'get': {'responses': {'200': {}}}},
        },
        'components': {'schemas': {'Item': {'type': 'object', 'properties': {
            'name': {'type': 'string'}, 'label': {'type': 'string'},
        }}}},
    }

    assert find_breaking_changes(previous, current) == []


def test_openapi_contract_detects_required_and_removed_changes():
    previous = {
        'paths': {'/api/items/': {'get': {'responses': {'200': {}, '404': {}}}}},
        'components': {'schemas': {'Item': {
            'type': 'object',
            'properties': {'name': {'type': 'string'}, 'old': {'type': 'string'}},
            'required': [],
        }}},
    }
    current = {
        'paths': {'/api/items/': {'get': {
            'parameters': [{'in': 'query', 'name': 'tenant', 'required': True}],
            'responses': {'200': {}},
        }}},
        'components': {'schemas': {'Item': {
            'type': 'object',
            'properties': {'name': {'type': 'integer'}},
            'required': ['name'],
        }}},
    }

    changes = find_breaking_changes(previous, current)

    assert 'required parameter added: GET /api/items/ query:tenant' in changes
    assert 'response status removed: GET /api/items/ 404' in changes
    assert 'required field added: component Item.name' in changes
    assert 'property removed: component Item.old' in changes
    assert 'schema type changed: component Item.name string->integer' in changes


def test_openapi_contract_detects_path_level_and_nested_changes():
    previous = {
        'paths': {'/api/items/{id}/': {
            'parameters': [{'in': 'path', 'name': 'id', 'required': False}],
            'get': {'responses': {'200': {}}},
        }},
        'components': {'schemas': {'Envelope': {
            'type': 'object',
            'properties': {'items': {'type': 'array', 'items': {
                'type': 'object', 'properties': {'state': {'type': 'string', 'enum': ['ok', 'bad']}}
            }}},
        }}},
    }
    current = {
        'paths': {'/api/items/{id}/': {
            'parameters': [{'in': 'path', 'name': 'id', 'required': True}],
            'get': {'responses': {'200': {}}},
        }},
        'components': {'schemas': {'Envelope': {
            'type': 'object',
            'properties': {'items': {'type': 'array', 'items': {
                'type': 'object', 'properties': {'state': {'type': 'string', 'enum': ['ok']}}
            }}},
        }}},
    }

    changes = find_breaking_changes(previous, current)

    assert 'required parameter added: GET /api/items/{id}/ path:id' in changes
    assert 'enum value removed: component Envelope.items[].state=bad' in changes


def test_openapi_contract_accepts_structured_enum_and_ref_rename():
    previous = {
        'components': {'schemas': {
            'Old': {'type': 'object', 'properties': {'state': {'type': 'string'}}},
            'Envelope': {'type': 'object', 'properties': {
                'value': {'$ref': '#/components/schemas/Old'},
                'kind': {'enum': [{'mode': 'a'}, {'mode': 'b'}]},
            }},
        }},
    }
    current = {
        'components': {'schemas': {
            'New': {'type': 'object', 'properties': {'state': {'type': 'string'}}},
            'Old': {'type': 'object', 'properties': {'state': {'type': 'string'}}},
            'Envelope': {'type': 'object', 'properties': {
                'value': {'$ref': '#/components/schemas/New'},
                'kind': {'enum': [{'mode': 'a'}]},
            }},
        }},
    }

    changes = find_breaking_changes(previous, current)

    assert 'schema reference changed' not in '\\n'.join(changes)
    assert "enum value removed: component Envelope.kind={'mode': 'b'}" in changes
