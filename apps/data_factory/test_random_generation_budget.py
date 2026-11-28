from unittest.mock import Mock, patch

import pytest

from apps.data_factory.serializers import ToolExecuteSerializer
from apps.data_factory.tools import random_tools
from apps.data_factory.tools.random_tools import RandomTools, validate_generation_budget


def _serializer(tool_name, input_data):
    return ToolExecuteSerializer(data={
        'tool_name': tool_name,
        'tool_category': 'random',
        'tool_scenario': 'random',
        'input_data': input_data,
    })


def test_validate_generation_budget_enforces_count_length_and_product(monkeypatch):
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_COUNT', 3)
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_LENGTH', 4)
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_TOTAL_OUTPUT', 10)

    assert validate_generation_budget(3) is None
    assert validate_generation_budget(2, 4) is None
    for count in (0, 4, True, 1.5, '2'):
        with pytest.raises(ValueError):
            validate_generation_budget(count)
    for length in (0, 5, True, 1.5, '2'):
        with pytest.raises(ValueError):
            validate_generation_budget(1, length)
    with pytest.raises(ValueError, match='数据量'):
        validate_generation_budget(3, 4)


@pytest.mark.parametrize(('call', 'generator_path'), [
    (lambda: RandomTools.random_int(0, 1, 3), 'apps.data_factory.tools.random_tools.random.randint'),
    (lambda: RandomTools.random_float(0, 1, count=3), 'apps.data_factory.tools.random_tools.random.uniform'),
    (lambda: RandomTools.random_string(length=1, count=3), 'apps.data_factory.tools.random_tools.random.choice'),
    (lambda: RandomTools.random_uuid(count=3), 'apps.data_factory.tools.random_tools.uuid.uuid4'),
    (lambda: RandomTools.random_mac_address(count=3), 'apps.data_factory.tools.random_tools.random.randint'),
    (lambda: RandomTools.random_ip_address(count=3), 'apps.data_factory.tools.random_tools.random.randint'),
    (lambda: RandomTools.random_date('2024-01-01', '2024-12-31', count=3), 'apps.data_factory.tools.random_tools.random.randint'),
    (lambda: RandomTools.random_boolean(count=3), 'apps.data_factory.tools.random_tools.random.choice'),
    (lambda: RandomTools.random_color(count=3), 'apps.data_factory.tools.random_tools.random.randint'),
    (lambda: RandomTools.random_password(length=1, count=3), 'apps.data_factory.tools.random_tools.random.choice'),
    (lambda: RandomTools.random_sequence([1, 2, 3], count=3, unique=True), 'apps.data_factory.tools.random_tools.random.sample'),
])
def test_every_random_generator_rejects_count_before_generation(monkeypatch, call, generator_path):
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_COUNT', 2)
    with patch(generator_path) as generate:
        result = call()
    assert 'error' in result
    generate.assert_not_called()


def test_variable_length_budgets_precede_generation(monkeypatch):
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_TOTAL_OUTPUT', 5)
    with patch('apps.data_factory.tools.random_tools.random.choice') as choose:
        assert 'error' in RandomTools.random_string(length=3, count=2)
        choose.assert_not_called()
        assert 'error' in RandomTools.random_password(length=3, count=2)
        choose.assert_not_called()
    with patch('apps.data_factory.tools.random_tools.random.uniform') as uniform:
        assert 'error' in RandomTools.random_float(0, 1, precision=3, count=2)
        uniform.assert_not_called()
    with patch('apps.data_factory.tools.random_tools.random.randint') as randint:
        assert 'error' in RandomTools.random_mac_address(separator='--')
        randint.assert_not_called()
    with patch('apps.data_factory.tools.random_tools.random.sample') as sample:
        assert 'error' in RandomTools.random_sequence(list(range(8)), count=6, unique=True)
        sample.assert_not_called()


def test_normal_random_generators_still_accept_small_requests():
    results = [
        RandomTools.random_int(1, 9),
        RandomTools.random_float(0, 1),
        RandomTools.random_string(length=2),
        RandomTools.random_uuid(),
        RandomTools.random_mac_address(),
        RandomTools.random_ip_address(4),
        RandomTools.random_ip_address(6),
        RandomTools.random_date('2024-01-01', '2024-12-31'),
        RandomTools.random_boolean(),
        RandomTools.random_color('hex'),
        RandomTools.random_color('rgb'),
        RandomTools.random_color('rgba'),
        RandomTools.random_password(length=2),
        RandomTools.random_sequence(['a', 'b']),
        RandomTools.random_sequence(['a', 'b'], count=2, unique=True),
    ]
    assert all(result.get('success') for result in results)


def test_random_sequence_rejects_selected_payload_over_budget(monkeypatch):
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_LENGTH', 8)
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_TOTAL_OUTPUT', 4)
    with patch('apps.data_factory.tools.random_tools.random.choice', return_value='abc') as choose:
        result = RandomTools.random_sequence(['abc'])
    assert 'error' in result
    choose.assert_called_once()


def test_fixed_output_budget_precedes_uuid_and_date_work(monkeypatch):
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_TOTAL_OUTPUT', 8)
    with patch('apps.data_factory.tools.random_tools.uuid.uuid4') as generate_uuid:
        assert 'error' in RandomTools.random_uuid()
        generate_uuid.assert_not_called()

    date_parser = Mock()
    monkeypatch.setattr(random_tools, 'datetime', type('DateTimeStub', (), {'strptime': date_parser}))
    with patch('apps.data_factory.tools.random_tools.random.randint') as randint:
        assert 'error' in RandomTools.random_date('2024-01-01', '2024-12-31')
        date_parser.assert_not_called()
        randint.assert_not_called()


def test_serializer_uses_shared_budget_for_length_precision_separator_and_date(monkeypatch):
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_COUNT', 10)
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_LENGTH', 16)
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_TOTAL_OUTPUT', 10)
    requests = [
        ('random_string', {'length': 3, 'count': 4}),
        ('random_float', {'precision': 6, 'count': 2}),
        ('random_mac_address', {'separator': '--'}),
        ('random_date', {'date_format': '%100000000Y'}),
    ]

    for tool_name, input_data in requests:
        serializer = _serializer(tool_name, input_data)
        assert not serializer.is_valid(), (tool_name, serializer.errors)
        assert 'input_data' in serializer.errors


def test_serializer_rejects_invalid_count_without_generation(monkeypatch):
    monkeypatch.setattr(random_tools, 'MAX_RANDOM_COUNT', 2)
    serializer = _serializer('random_boolean', {'count': 3})
    assert not serializer.is_valid()
    assert 'input_data' in serializer.errors
