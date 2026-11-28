"""Untrusted data-factory operations must have hard allocation/time budgets."""
from unittest.mock import patch

import pytest
from django.urls import include, path
from rest_framework.test import APIClient

from apps.data_factory.models import DataFactoryRecord
from apps.data_factory.tools.random_tools import MAX_RANDOM_COUNT, RandomTools
from apps.data_factory.tools.string_tools import StringTools
from apps.users.models import User

urlpatterns = [path('api/data-factory/', include('apps.data_factory.urls'))]


@pytest.fixture(autouse=True)
def isolated_urls(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def client(db):
    user = User.objects.create_user(username='bounded-tool-owner')
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.mark.parametrize('count', [0, -1, MAX_RANDOM_COUNT + 1, 10**20, True, 1.5, '100', None])
def test_random_int_rejects_bad_count_before_allocating_or_recording(client, count):
    with patch('apps.data_factory.tools.random_tools.random.randint') as generate:
        response = client.post('/api/data-factory/', {
            'tool_name': 'random_int', 'tool_category': 'random', 'tool_scenario': 'random',
            'input_data': {'min_val': 0, 'max_val': 10, 'count': count},
        }, format='json')
    assert response.status_code == 400, response.data
    generate.assert_not_called()
    assert not DataFactoryRecord.objects.exists()


def test_direct_random_tool_is_bounded_and_valid_count_still_works():
    with patch('apps.data_factory.tools.random_tools.random.randint', return_value=5) as generate:
        assert 'error' in RandomTools.random_int(1, 10, 10**9)
        generate.assert_not_called()
        assert RandomTools.random_int(1, 10)['result'] == 5
        assert RandomTools.random_int(1, 10, 3)['result'] == [5, 5, 5]


def test_regex_test_preserves_matches_groups_flags_and_invalid_syntax():
    result = StringTools.regex_test(r'(?P<word>[a-z]+)-(\d+)', 'A-1 b-2', flags='i')
    assert result['matches'] == [('A', '1'), ('b', '2')]
    assert result['groups'] == ('A', '1')
    assert result['groupdict'] == {'word': 'A'}
    assert StringTools.regex_test('[', 'text')['is_valid'] is False


@pytest.mark.parametrize('operation', ['test', 'replace'])
def test_pathological_regex_has_hard_timeout(operation):
    pattern = r'(a|aa)+$'
    text = 'a' * 128 + '!'
    if operation == 'test':
        result = StringTools.regex_test(pattern, text)
    else:
        result = StringTools.replace_string(text, pattern, 'safe', is_regex=True)
    assert '超时' in result.get('error', ''), result


def test_regex_input_matches_and_groups_are_bounded():
    assert 'error' in StringTools.regex_test('a' * 2049, 'a')
    assert 'error' in StringTools.regex_test('a', 'a' * 64_001)
    assert 'error' in StringTools.regex_test('', 'a' * 1001)
    assert 'error' in StringTools.regex_test('()' * 40, 'a')


def test_replacement_bounds_output_and_uses_actual_match_count():
    result = StringTools.replace_string('a1 b2', r'([a-z])(\d)', r'\2\1', is_regex=True)
    assert result == {'result': '1a 2b', 'replacements': 2}
    assert 'error' in StringTools.replace_string('a' * 64000, '', 'b' * 64000)
    assert 'error' in StringTools.replace_string('a' * 1001, 'a', 'b', is_regex=True)


def test_regex_timeout_is_an_api_error_and_is_not_saved(client):
    response = client.post('/api/data-factory/', {
        'tool_name': 'regex_test', 'tool_category': 'string', 'tool_scenario': 'string',
        'input_data': {'pattern': '(a|aa)+$', 'text': 'a' * 128 + '!'},
    }, format='json')
    assert response.status_code == 400, response.data
    assert '超时' in response.data['error']
    assert not DataFactoryRecord.objects.exists()


def test_random_output_product_is_validated_before_generating(client):
    response = client.post('/api/data-factory/', {
        'tool_name': 'random_string', 'tool_category': 'random', 'tool_scenario': 'random',
        'input_data': {'length': 4096, 'count': MAX_RANDOM_COUNT},
    }, format='json')
    assert response.status_code == 400, response.data
    assert not DataFactoryRecord.objects.exists()
