from unittest.mock import patch
import re

import pytest
from rest_framework.test import APIClient

from apps.data_factory.models import DataFactoryRecord
from apps.data_factory.tools import regex_limits
from apps.data_factory.tools.string_tools import StringTools
from apps.users.models import User


@pytest.mark.parametrize('operation', ['test', 'replace'])
@pytest.mark.parametrize('pattern', ['(ab){50000}', '(?:(ab){100}){100}', 'a{99999999999999999999}', '()' * 40])
def test_expensive_compilation_is_rejected_before_regex_allocates(operation, pattern):
    with patch.object(regex_limits.regex, 'compile', side_effect=AssertionError('unsafe compilation')) as compile_pattern:
        if operation == 'test':
            result = StringTools.regex_test(pattern, '')
        else:
            result = StringTools.replace_string('', pattern, 'replacement', is_regex=True)
    assert 'error' in result
    compile_pattern.assert_not_called()


def test_literal_braces_and_normal_quantifiers_are_not_rejected_as_repeat_counts():
    assert StringTools.regex_test(r'\{50000\}', '{50000}')['matches'] == ['{50000}']
    assert StringTools.regex_test(r'[{50000}]+', '50000')['matches'] == ['50000']
    assert StringTools.regex_test('(ab){3}', 'ababab')['matches'] == ['ab']
    assert StringTools.regex_test('(?i)^ab$', 'AB')['matches'] == ['AB']


def test_unsupported_extensions_do_not_bypass_compilation_analysis():
    with patch.object(regex_limits.regex, 'compile', side_effect=AssertionError('unanalysed extension')) as compile_pattern:
        result = StringTools.regex_test('(?R)', '')
    assert 'error' in result
    compile_pattern.assert_not_called()


def test_user_patterns_are_not_retained_in_the_global_regex_cache():
    actual_compile = regex_limits.regex.compile
    with patch.object(regex_limits.regex, 'compile', wraps=actual_compile) as compile_pattern:
        assert StringTools.regex_test('a', 'a')['matches'] == ['a']
    assert compile_pattern.call_args.kwargs['cache_pattern'] is False


def test_large_capture_references_are_budgeted_before_expansion_or_joining():
    text = 'a' * 64_000
    captures = []

    class Match:
        def start(self):
            return 0

        def end(self):
            return len(text)

        def group(self, group):
            captures.append(group)
            return text

        def expand(self, _template):
            raise AssertionError('unbounded capture expansion')

    class Pattern:
        groups = 1
        groupindex = {}

        def sub(self, callback, _text, **_kwargs):
            return callback(Match())

    with patch.object(regex_limits.regex, 'compile', return_value=Pattern()):
        result = StringTools.replace_string(text, '(a+)', r'\1' * 16000, is_regex=True)
    assert '超过大小上限' in result.get('error', '')
    assert captures == [1]


def test_bounded_replacement_preserves_python_templates_and_rejects_unknown_groups():
    pattern = r'(?P<char>[A-Z])(\d)'
    template = r'\g<char>:\2:\g<0>'
    text = 'A1 B2'
    result = StringTools.replace_string(text, pattern, template, is_regex=True)
    assert result == {'result': re.sub(pattern, template, text), 'replacements': 2}
    assert 'error' in StringTools.replace_string(text, pattern, r'\g<missing>', is_regex=True)


@pytest.mark.django_db
def test_regex_compile_budget_is_an_api_error_without_saved_output():
    user = User.objects.create_user(username='regex-compile-budget-user')
    client = APIClient()
    client.force_authenticate(user)
    response = client.post('/api/data-factory/', {
        'tool_name': 'regex_test', 'tool_category': 'string', 'tool_scenario': 'string',
        'input_data': {'pattern': '(ab){50000}', 'text': ''},
    }, format='json')
    assert response.status_code == 400, response.data
    assert '编译' in response.data['error']
    assert not DataFactoryRecord.objects.filter(user=user).exists()
