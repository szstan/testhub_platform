"""Shared variable resolution must enforce data-factory budgets before dispatch."""
from unittest.mock import patch

import pytest

from apps.core.variable_resolver import VariableResolver


@pytest.mark.parametrize(
    ('expression', 'generator'),
    [
        ('${random_int(0, 10, 10001)}', 'apps.core.variable_resolver.RandomTools.random_int'),
        ('${random_string(8, all, 10001)}', 'apps.core.variable_resolver.RandomTools.random_string'),
        ('${random_digits(8, 10001)}', 'apps.core.variable_resolver.RandomTools.random_string'),
        ('${generate_chinese_phone(10001)}', 'apps.core.variable_resolver.TestDataTools.generate_chinese_phone'),
        ('${generate_user_profile(10001)}', 'apps.core.variable_resolver.TestDataTools.generate_user_profile'),
        ('${get_next_runs("* * * * *", 10001)}', 'apps.core.variable_resolver.CrontabTools.get_next_runs'),
    ],
)
def test_count_based_factories_reject_over_budget_before_dispatch(expression, generator):
    with patch(generator) as generate:
        assert VariableResolver().resolve(expression) == expression
    generate.assert_not_called()


def test_random_string_length_is_checked_before_generation():
    with patch('apps.core.variable_resolver.RandomTools.random_string') as generate:
        assert VariableResolver().resolve('${random_string(4097)}') == '${random_string(4097)}'
    generate.assert_not_called()


def test_resolver_rejects_too_many_expressions_before_running_them():
    values = []
    resolver = VariableResolver(on_value=values.append)
    template = '${timestamp()} ' * 10001

    with pytest.raises(ValueError, match='数量'):
        resolver.resolve(template)

    assert values == []


def test_resolver_observes_raw_generated_output_before_interpolation():
    observed = []
    with patch(
        'apps.core.variable_resolver.RandomTools.random_string',
        return_value={'success': True, 'result': 'resolver-runtime-value'},
    ):
        resolved = VariableResolver(on_value=observed.append).resolve('key=${random_string(8)}')

    assert resolved == 'key=resolver-runtime-value'
    assert observed == ['resolver-runtime-value']


def test_resolution_budget_counts_and_renders_across_repeated_resolve_calls(monkeypatch):
    from apps.core import variable_resolver

    monkeypatch.setattr(variable_resolver, 'MAX_RANDOM_COUNT', 2)
    monkeypatch.setattr(variable_resolver, 'MAX_RANDOM_TOTAL_OUTPUT', 8)
    resolver = VariableResolver()
    expression = '${random_string(4)}'

    with patch(
        'apps.core.variable_resolver.RandomTools.random_string',
        return_value={'success': True, 'result': 'Ab12'},
    ) as generate:
        assert resolver.resolve(expression) == 'Ab12'
        assert resolver.resolve(expression) == 'Ab12'
        with pytest.raises(ValueError, match='数量'):
            resolver.resolve(expression)

    assert generate.call_count == 2
    assert resolver.budget.count == 2
    assert resolver.budget.output == 8


def test_resolution_budget_isolated_between_resolver_instances(monkeypatch):
    from apps.core import variable_resolver

    monkeypatch.setattr(variable_resolver, 'MAX_RANDOM_COUNT', 1)
    monkeypatch.setattr(variable_resolver, 'MAX_RANDOM_TOTAL_OUTPUT', 4)
    expression = '${random_string(4)}'

    with patch(
        'apps.core.variable_resolver.RandomTools.random_string',
        return_value={'success': True, 'result': 'Ab12'},
    ) as generate:
        assert VariableResolver().resolve(expression) == 'Ab12'
        assert VariableResolver().resolve(expression) == 'Ab12'

    assert generate.call_count == 2


def test_generation_batch_output_budget_is_reserved_before_dispatch(monkeypatch):
    from apps.core import variable_resolver

    monkeypatch.setattr(variable_resolver, 'MAX_RANDOM_TOTAL_OUTPUT', 20)
    expression = '${random_string(8, all, 2)}'
    with patch('apps.core.variable_resolver.RandomTools.random_string') as generate:
        assert VariableResolver().resolve(expression) == expression
    generate.assert_not_called()
