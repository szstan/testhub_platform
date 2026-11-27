import copy
import json

import pytest

from apps.app_automation import component_templates as module


def encoded(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8'))


def test_large_valid_component_encoding_work_is_linear(monkeypatch):
    steps = [{'type': 'wait', 'config': {'timeout': 0, 'metadata': 'x' * 400}} for _ in range(999)]
    definition = {'project_id': 1, 'type': 'bulk', 'version': '1', 'schema': {},
                  'default_config': {}, 'steps': steps}
    reference = {'kind': 'custom', 'type': 'bulk', 'version': '1', 'config': {}}
    work = {'bytes': 0}
    original = module._encoded_size

    def measured(value, path):
        size = original(value, path)
        work['bytes'] += size
        return size

    monkeypatch.setattr(module, '_encoded_size', measured)
    result = module.expand_steps([reference], project_id=1, resolve_template=lambda *args: definition)
    assert result == steps
    # All encodings together remain a small multiple of the final payload,
    # instead of hundreds of MiB for this sub-MiB component.
    assert work['bytes'] < 6 * encoded(result)


@pytest.mark.parametrize('text', ['普通文案🚀', 'quote " and slash \\ and newline\n'])
def test_incremental_size_preserves_exact_utf8_cumulative_limit(text):
    steps = [{'type': 'wait', 'config': {'metadata': text}}, {'type': 'wait'}]
    original = copy.deepcopy(steps)
    exact = 2 * encoded(steps)
    result = module.expand_steps(steps, project_id=1, resolve_template=lambda *args: None,
                                 max_payload_bytes=exact)
    assert result == original
    assert steps == original
    with pytest.raises(module.ExpansionLimitError):
        module.expand_steps(steps, project_id=1, resolve_template=lambda *args: None,
                            max_payload_bytes=exact - 1)


def test_flattened_templates_keep_exact_size_and_do_not_mutate_sources():
    leaf = {'type': 'wait', 'config': {'metadata': '文案🚀'}}
    definition = {'project_id': 1, 'type': 'child', 'version': '1', 'schema': {},
                  'default_config': {}, 'steps': [leaf]}
    source = [{'kind': 'custom', 'type': 'child', 'version': '1', 'params': {}},
              {'kind': 'custom', 'type': 'child', 'version': '1', 'params': {}}]
    before = copy.deepcopy((source, definition))
    expected = [leaf, leaf]
    exact = encoded(source) + 2 * encoded(definition) + encoded(expected)
    assert module.expand_steps(source, project_id=1, resolve_template=lambda *args: definition,
                               max_payload_bytes=exact) == expected
    assert (source, definition) == before
    with pytest.raises(module.ExpansionLimitError):
        module.expand_steps(source, project_id=1, resolve_template=lambda *args: definition,
                            max_payload_bytes=exact - 1)
