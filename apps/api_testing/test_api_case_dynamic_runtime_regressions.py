"""Dynamic resolver outputs must be bounded and scrubbed before evidence persists."""
import json
from urllib.parse import quote, quote_plus
from unittest.mock import patch

import pytest
from requests import Response

from apps.api_testing.case_tasks import execute_api_case_execution
from apps.api_testing.evidence_redaction import remember_runtime_values, runtime_scrubber
from apps.api_testing.models import (
    ApiCaseExecution, ApiTestCase, ApiTestCaseStep, RequestHistory,
    TestExecution as SuiteExecutionRecord,
    TestSuite as ApiTestSuite,
    TestSuiteRequest as ApiTestSuiteRequest,
)
from apps.api_testing.test_execution_integration import graph
from apps.api_testing.utils import execute_api_request, execute_test_suite
from apps.quality_intelligence.services.redaction import REDACTION_MARKER


RUNTIME_VALUE = 'rr003-runtime+secret/with space'


def test_short_runtime_values_do_not_erase_unrelated_evidence():
    literals = set()
    remember_runtime_values('/', literals)
    remember_runtime_values('1', literals)
    scrub = runtime_scrubber(literals)

    assert scrub('/api/v1/items') == '/api/v1/items'
    assert scrub('/') == REDACTION_MARKER
    assert scrub('status 1, count 10') == f'status {REDACTION_MARKER}, count 10'


def test_embedded_short_values_and_encoded_forms_are_redacted_without_erasing_paths():
    literals = set()
    for value in ('Ab12', '&', '1', '汉'):
        remember_runtime_values(value, literals)
    scrub = runtime_scrubber(literals)

    assert scrub('prefixAb12suffix') == f'prefix{REDACTION_MARKER}suffix'
    assert scrub('a&amp;b') == f'a{REDACTION_MARKER}b'
    assert scrub('a&b') == f'a{REDACTION_MARKER}b'
    assert scrub('prefix%26suffix') == f'prefix{REDACTION_MARKER}suffix'
    json_encoded = json.dumps('汉', ensure_ascii=True)[1:-1]
    assert scrub(f'prefix{json_encoded}suffix') == f'prefix{REDACTION_MARKER}suffix'
    assert scrub('/api/v1/items') == '/api/v1/items'
    assert scrub('https://api.example.test/api/v1/items?left=alpha&right=beta') == (
        'https://api.example.test/api/v1/items?left=alpha&right=beta'
    )


def http_response(payload):
    response = Response()
    response.status_code = 200
    response._content = json.dumps(payload).encode('utf-8')
    response.encoding = 'utf-8'
    response.headers['Content-Type'] = 'application/json'
    return response


def test_dynamic_values_are_scrubbed_from_request_history_without_changing_wire_values(graph):
    request = graph.definition
    request.method = 'POST'
    request.url = 'https://api.example.test/${random_string(32)}'
    request.headers = {'X-Runtime': 'prefix-${random_string(32)}'}
    request.params = {'lookup': '${random_string(32)}'}
    request.body = {'type': 'json', 'data': {'value': '${random_string(32)}'}}
    request.assertions = [{'type': 'json_path', 'json_path': '$.echo', 'expected': 'different'}]
    request.save()

    payload = {
        'echo': RUNTIME_VALUE,
        'encoded': quote(RUNTIME_VALUE, safe=''),
        'plus_encoded': quote_plus(RUNTIME_VALUE, safe=''),
        'nested': {'echo': RUNTIME_VALUE},
        'safe': 'visible response evidence',
    }
    with patch(
        'apps.core.variable_resolver.RandomTools.random_string',
        return_value={'success': True, 'result': RUNTIME_VALUE},
    ), patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=http_response(payload),
    ) as transport:
        result = execute_api_request(request, None, graph.owner)

    assert result['success'] is True
    sent = transport.call_args.kwargs
    assert sent['url'] == f'https://api.example.test/{RUNTIME_VALUE}'
    assert sent['headers']['X-Runtime'] == f'prefix-{RUNTIME_VALUE}'
    assert sent['params']['lookup'] == RUNTIME_VALUE
    assert sent['json'] == {'value': RUNTIME_VALUE}

    history = RequestHistory.objects.get(request=request)
    evidence = json.dumps({
        'request': history.request_data,
        'response': history.response_data,
        'assertions': history.assertions_results,
    })
    assert RUNTIME_VALUE not in evidence
    assert quote(RUNTIME_VALUE, safe='') not in evidence
    assert quote_plus(RUNTIME_VALUE, safe='') not in evidence
    assert REDACTION_MARKER in evidence
    assert history.response_data['json']['safe'] == 'visible response evidence'
    assert history.assertions_results[0]['actual'] == REDACTION_MARKER


def test_dynamic_values_are_scrubbed_from_persisted_case_transport_errors(graph):
    case = ApiTestCase.objects.create(project=graph.project, name='Dynamic error evidence', created_by=graph.owner)
    graph.definition.method = 'POST'
    graph.definition.body = {'type': 'json', 'data': {'value': '${random_string(32)}'}}
    graph.definition.save(update_fields=['method', 'body'])
    ApiTestCaseStep.objects.create(test_case=case, request=graph.definition, name='Dynamic failure')
    execution = ApiCaseExecution.objects.create(
        project=graph.project, test_case=case, test_case_name=case.name, executed_by=graph.owner,
    )

    with patch(
        'apps.core.variable_resolver.RandomTools.random_string',
        return_value={'success': True, 'result': RUNTIME_VALUE},
    ), patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request',
        side_effect=RuntimeError(f'upstream echoed {RUNTIME_VALUE} and {quote_plus(RUNTIME_VALUE, safe="")}'),
    ) as transport:
        outcome = execute_api_case_execution.run(execution.pk)

    assert outcome['status'] == 'FAILED'
    assert transport.call_args.kwargs['json'] == {'value': RUNTIME_VALUE}
    execution.refresh_from_db()
    evidence = json.dumps(execution.results)
    assert execution.status == 'FAILED'
    assert RUNTIME_VALUE not in evidence
    assert quote(RUNTIME_VALUE, safe='') not in evidence
    assert quote_plus(RUNTIME_VALUE, safe='') not in evidence
    assert REDACTION_MARKER in evidence


def _configure_short_runtime_request(request):
    request.method = 'POST'
    request.url = 'https://api.example.test/prefix${random_string(4)}suffix'
    request.headers = {'X-Short': 'prefix${random_string(4)}suffix'}
    request.params = {'lookup': 'prefix${random_string(4)}suffix'}
    request.body = {
        'type': 'json',
        'data': {
            'echo': 'prefix${random_string(4)}suffix',
            'separator': 'a{{separator}}b',
        },
    }
    request.assertions = [{'type': 'contains', 'expected': 'prefixAb12suffix'}]
    request.save()


def _short_runtime_response():
    return http_response({
        'echo': 'prefixAb12suffix',
        'html': 'a&amp;b',
        'raw': 'a&b',
        'url': 'prefix%26suffix',
        'safe': 'https://api.example.test/api/v1/items?left=alpha&right=beta',
    })


def test_short_runtime_values_are_scrubbed_from_persisted_case_evidence(graph):
    case = ApiTestCase.objects.create(project=graph.project, name='Short runtime case', created_by=graph.owner)
    _configure_short_runtime_request(graph.definition)
    ApiTestCaseStep.objects.create(test_case=case, request=graph.definition, name='Echo short values')
    execution = ApiCaseExecution.objects.create(
        project=graph.project, test_case=case, test_case_name=case.name, executed_by=graph.owner,
    )

    with patch(
        'apps.core.variable_resolver.RandomTools.random_string',
        return_value={'success': True, 'result': 'Ab12'},
    ) as generate, patch(
        'apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url,
    ), patch(
        'apps.api_testing.utils.guarded_request', return_value=_short_runtime_response(),
    ) as transport:
        outcome = execute_api_case_execution.run(execution.pk, {'separator': '&'})

    assert outcome['success'] is True
    sent = transport.call_args.kwargs
    assert sent['url'] == 'https://api.example.test/prefixAb12suffix'
    assert sent['headers']['X-Short'] == 'prefixAb12suffix'
    assert sent['params']['lookup'] == 'prefixAb12suffix'
    assert sent['json'] == {'echo': 'prefixAb12suffix', 'separator': 'a&b'}
    assert generate.call_count == 4

    execution.refresh_from_db()
    evidence = json.dumps(execution.results, ensure_ascii=False)
    assert execution.status == 'COMPLETED'
    assert execution.results[0]['assertions_results'][0]['passed'] is True
    assert 'Ab12' not in evidence
    assert '&amp;' not in evidence
    assert '%26' not in evidence
    assert execution.results[0]['response_data']['json']['html'] == f'a{REDACTION_MARKER}b'
    assert execution.results[0]['response_data']['json']['raw'] == f'a{REDACTION_MARKER}b'
    assert execution.results[0]['response_data']['json']['safe'] == (
        'https://api.example.test/api/v1/items?left=alpha&right=beta'
    )
    assert execution.results[0]['url'] == f'https://api.example.test/prefix{REDACTION_MARKER}suffix'


@pytest.mark.parametrize(
    ('limit_name', 'limit', 'expected_dispatches'),
    [
        ('MAX_RANDOM_COUNT', 3, 3),
        ('MAX_RANDOM_TOTAL_OUTPUT', 16, 4),
    ],
)
def test_request_resolution_shares_budget_across_url_headers_params_and_nested_body(
    graph, monkeypatch, limit_name, limit, expected_dispatches,
):
    from apps.core import variable_resolver

    monkeypatch.setattr(variable_resolver, limit_name, limit)
    expression = '${random_string(4)}'
    graph.definition.method = 'POST'
    graph.definition.url = f'https://api.example.test/{expression}'
    graph.definition.headers = {'X-One': expression}
    graph.definition.params = {'lookup': expression}
    graph.definition.body = {
        'type': 'json',
        'data': {'first': expression, 'nested': {'second': expression}},
    }
    graph.definition.save(update_fields=['method', 'url', 'headers', 'params', 'body'])

    with patch(
        'apps.core.variable_resolver.RandomTools.random_string',
        return_value={'success': True, 'result': 'Ab12'},
    ) as generate, patch(
        'apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url,
    ), patch('apps.api_testing.utils.guarded_request') as transport:
        result = execute_api_request(graph.definition, None, graph.owner)

    assert result['success'] is False
    assert generate.call_count == expected_dispatches
    transport.assert_not_called()


def test_short_runtime_values_are_scrubbed_from_suite_and_request_history(graph):
    _configure_short_runtime_request(graph.definition)
    suite = ApiTestSuite.objects.create(project=graph.project, name='Short runtime suite', created_by=graph.owner)
    ApiTestSuiteRequest.objects.create(test_suite=suite, request=graph.definition)

    with patch(
        'apps.core.variable_resolver.RandomTools.random_string',
        return_value={'success': True, 'result': 'Ab12'},
    ) as generate, patch(
        'apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url,
    ), patch(
        'apps.api_testing.utils.guarded_request', return_value=_short_runtime_response(),
    ) as transport:
        result = execute_test_suite(suite, None, graph.owner, variable_overrides={'separator': '&'})

    assert result['success'] is True
    sent = transport.call_args.kwargs
    assert sent['url'] == 'https://api.example.test/prefixAb12suffix'
    assert sent['headers']['X-Short'] == 'prefixAb12suffix'
    assert sent['params']['lookup'] == 'prefixAb12suffix'
    assert sent['json'] == {'echo': 'prefixAb12suffix', 'separator': 'a&b'}
    assert generate.call_count == 4

    execution = SuiteExecutionRecord.objects.get(pk=result['execution_id'])
    history = RequestHistory.objects.get(request=graph.definition)
    evidence = json.dumps({
        'suite': execution.results,
        'request': history.request_data,
        'response': history.response_data,
        'assertions': history.assertions_results,
    }, ensure_ascii=False)
    assert 'Ab12' not in evidence
    assert '&amp;' not in evidence
    assert '%26' not in evidence
    assert history.response_data['json']['html'] == f'a{REDACTION_MARKER}b'
    assert history.response_data['json']['raw'] == f'a{REDACTION_MARKER}b'
    assert history.response_data['json']['safe'] == (
        'https://api.example.test/api/v1/items?left=alpha&right=beta'
    )
    assert execution.results[0]['url'] == f'https://api.example.test/prefix{REDACTION_MARKER}suffix'
    assert execution.results[0]['assertions_results'][0]['passed'] is True


@pytest.mark.parametrize(
    ('overrides', 'expected_body'),
    [({}, {'template': True}), ({'body': {}}, None), ({'body': None}, None)],
)
def test_body_override_inherits_only_when_omitted(graph, overrides, expected_body):
    graph.definition.method = 'POST'
    graph.definition.body = {'type': 'json', 'data': {'template': True}}
    graph.definition.save(update_fields=['method', 'body'])
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=http_response({'ok': True}),
    ) as transport:
        result = execute_api_request(
            graph.definition, None, graph.owner,
            request_overrides=overrides,
            record_history=False,
        )

    assert result['success'] is True
    if expected_body is None:
        assert 'json' not in transport.call_args.kwargs
        assert 'data' not in transport.call_args.kwargs
        assert 'files' not in transport.call_args.kwargs
    else:
        assert transport.call_args.kwargs['json'] == expected_body
