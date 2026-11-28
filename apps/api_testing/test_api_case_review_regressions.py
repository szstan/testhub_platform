"""Review regressions for assertion truth and transient evidence boundaries."""
import json
from unittest.mock import patch

import pytest

from apps.api_testing.case_runner import run_api_test_case
from apps.api_testing.models import (
    ApiTestCase, ApiTestCaseStep, RequestHistory, TestExecution as ApiSuiteExecution,
    TestSuite as ApiSuite, TestSuiteRequest as ApiSuiteRequest,
)
from apps.api_testing.test_api_case_execution_evidence import http_response
from apps.api_testing.test_execution_integration import graph
from apps.api_testing.utils import execute_api_request, execute_assertions, execute_test_suite, redact_response_data
from apps.quality_intelligence.services.redaction import REDACTION_MARKER


def test_jsonpath_null_requires_an_existing_match():
    response = http_response(200, {'present': None})
    result = execute_assertions(response, [
        {'type': 'json_path', 'json_path': '$.missing', 'expected': None},
        {'type': 'json_path', 'json_path': '$.present', 'expected': None},
    ])
    assert [item['passed'] for item in result] == [False, True]


def test_jsonpath_compares_structures_independent_of_object_key_order():
    response = http_response(200, {'value': {'a': [1, {'b': True, 'c': 2}], 'd': None}})
    result = execute_assertions(response, [
        {'type': 'json_path', 'json_path': '$.value', 'expected': {'d': None, 'a': [1, {'c': 2, 'b': True}]}},
        {'type': 'json_path', 'json_path': '$.value', 'expected': {'d': None, 'a': [1, {'c': 2, 'b': 1}]}},
    ])
    assert [item['passed'] for item in result] == [True, False]


def test_legacy_jsonpath_scalar_string_expectation_remains_supported():
    result = execute_assertions(http_response(200, {'count': 1}), [
        {'type': 'json_path', 'json_path': '$.count', 'expected': '1'},
    ])
    assert result[0]['passed'] is True


@pytest.mark.parametrize('source', ['initial', 'extracted'])
@pytest.mark.parametrize('padding', [0, 180, 4080])
def test_long_runtime_values_are_scrubbed_before_response_and_assertion_bounds(graph, source, padding):
    value = 'review-synthetic-transient-' + 'x' * 5000
    case = ApiTestCase.objects.create(project=graph.project, name='Long response evidence', created_by=graph.owner)
    ApiTestCaseStep.objects.create(
        test_case=case, request=graph.definition, name='Echo long value',
        assertions=[{'type': 'contains', 'expected': 'prefix'}],
        extractors=[{'name': 'receipt', 'source': 'json_path', 'expression': '$.echo', 'required': True}] if source == 'extracted' else [],
    )
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=http_response(200, {
            'echo': value, 'message': 'prefix ' + 'p' * padding + value, 'safe': 'visible',
        }),
    ):
        result = run_api_test_case(case, None, graph.owner, variable_overrides={'receipt': value} if source == 'initial' else {})
    assert result['success'] is True
    evidence = json.dumps(result)
    assert 'review-synthetic-transient-' not in evidence
    assert REDACTION_MARKER in evidence
    assert result['results'][0]['response_data']['json']['safe'] == 'visible'
    assert not RequestHistory.objects.filter(request=graph.definition).exists()


def test_long_runtime_error_is_scrubbed_before_error_truncation(graph):
    value = 'review-synthetic-transient-' + 'x' * 5000
    case = ApiTestCase.objects.create(project=graph.project, name='Long error evidence', created_by=graph.owner)
    ApiTestCaseStep.objects.create(test_case=case, request=graph.definition, name='Failure')
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', side_effect=RuntimeError('upstream echoed ' + value),
    ):
        result = run_api_test_case(case, None, graph.owner, variable_overrides={'receipt': value})
    assert result['success'] is False
    assert 'review-synthetic-transient-' not in json.dumps(result)
    assert REDACTION_MARKER in result['results'][0]['error']


@pytest.mark.parametrize('name', ['accessToken', 'refreshToken', 'clientSecret', 'privateKey', 'sessionId'])
def test_response_evidence_redacts_camel_case_secret_keys(name):
    response = http_response(200, {name: 'review-synthetic-secret', 'safe': 'visible'})
    safe = redact_response_data(response)
    assert 'review-synthetic-secret' not in json.dumps(safe)
    assert safe['json'][name] == REDACTION_MARKER
    assert safe['json']['safe'] == 'visible'


@pytest.mark.parametrize('assertion_type', ['json_path', 'header'])
def test_deferred_case_evidence_preserves_sensitive_assertion_redaction(graph, assertion_type):
    value = 'review-synthetic-secret'
    assertion = {'type': assertion_type, 'expected': value}
    if assertion_type == 'json_path':
        assertion['json_path'] = '$.accessToken'
    else:
        assertion['header_name'] = 'X-Auth-Token'
    response = http_response(200, {'accessToken': value})
    response.headers['X-Auth-Token'] = value
    case = ApiTestCase.objects.create(project=graph.project, name='Sensitive assertion', created_by=graph.owner)
    ApiTestCaseStep.objects.create(test_case=case, request=graph.definition, name='Sensitive evidence', assertions=[assertion])
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=response,
    ):
        result = run_api_test_case(case, None, graph.owner)
    assert result['success'] is True
    assert value not in json.dumps(result)
    evidence = result['results'][0]['assertions_results'][0]
    assert evidence['actual'] == evidence['expected'] == REDACTION_MARKER


def test_deferred_evidence_cannot_be_used_with_persistent_request_history():
    with pytest.raises(ValueError, match='in-memory case execution'):
        execute_api_request(None, None, None, defer_evidence_redaction=True)


@pytest.mark.parametrize('actual,expected,passed', [
    (1, '1', False), ('1', '1', True), (True, 'True', False), (None, 'None', False),
])
def test_new_case_jsonpath_expectations_keep_their_json_types(graph, actual, expected, passed):
    created = graph.client.post('/api/api-testing/test-cases/', {
        'project': graph.project.pk, 'name': 'Typed assertion',
        'steps': [{
            'request': graph.definition.pk, 'name': 'Typed JSON value',
            'assertions': [{'type': 'json_path', 'json_path': '$.value', 'expected': expected}],
        }],
    }, format='json')
    assert created.status_code == 201, created.data
    case = ApiTestCase.objects.get(pk=created.data['id'])
    assert case.steps.get().assertions[0]['comparison'] == 'json'
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=http_response(200, {'value': actual}),
    ):
        result = run_api_test_case(case, None, graph.owner)
    assert result['success'] is passed
    assert result['results'][0]['assertions_results'][0]['passed'] is passed


@pytest.mark.parametrize('long_value', [False, True])
def test_suite_request_history_scrubs_dataset_values_under_benign_field_names(graph, long_value):
    value = 'review-synthetic-dataset-' + ('x' * 5000 if long_value else 'abc')
    graph.definition.params = {'lookup': '{{dataset_value}}'}
    graph.definition.body = {'type': 'json', 'data': {'reference': '{{dataset_value}}'}}
    graph.definition.save()
    suite = ApiSuite.objects.create(project=graph.project, name='Dataset suite', created_by=graph.owner)
    ApiSuiteRequest.objects.create(
        test_suite=suite, request=graph.definition,
        assertions=[{'type': 'contains', 'expected': value}],
    )
    supplied = {'dataset_value': value}
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=http_response(200, {'echo': value, 'safe': 'visible'}),
    ) as transport:
        result = execute_test_suite(suite, None, graph.owner, variable_overrides=supplied)
    assert result['success'] is True
    assert transport.call_args.kwargs['params']['lookup'] == value
    assert transport.call_args.kwargs['json']['reference'] == value
    history = RequestHistory.objects.get(request=graph.definition)
    execution = ApiSuiteExecution.objects.get(pk=result['execution_id'])
    for evidence in [history.request_data, history.response_data, history.assertions_results, execution.results, result]:
        assert 'review-synthetic-dataset-' not in json.dumps(evidence)
    assert history.request_data['params']['lookup'] == REDACTION_MARKER
    assert history.response_data['json']['echo'] == REDACTION_MARKER
    assert history.response_data['json']['safe'] == 'visible'
    assert supplied == {'dataset_value': value}


def test_unnamed_suite_assertion_failure_has_a_readable_history_summary(graph):
    suite = ApiSuite.objects.create(project=graph.project, name='Unnamed assertion', created_by=graph.owner)
    ApiSuiteRequest.objects.create(
        test_suite=suite, request=graph.definition,
        assertions=[{'type': 'status_code', 'name': '', 'expected': 201}],
    )
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=http_response(200, {'ok': True}),
    ):
        result = execute_test_suite(suite, None, graph.owner)
    execution = ApiSuiteExecution.objects.get(pk=result['execution_id'])
    assert execution.status == 'FAILED'
    assert execution.failed_requests == 1
    summary = execution.results[0]['error']
    assert '未命名断言' in summary
    assert '期望值与实际值不一致' in summary
    assert 'None' not in summary
    assertions = execution.results[0]['assertions_results']
    assert assertions[-1]['expected'] == 201
    assert assertions[-1]['actual'] == 200
