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
    assert 'component required field added: Item.name' in changes
    assert 'component property removed: Item.old' in changes
    assert 'component property type changed: Item.name string->integer' in changes
