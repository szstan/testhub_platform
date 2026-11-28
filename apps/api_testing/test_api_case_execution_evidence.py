"""Case evidence must be useful without persisting transient response variables."""
import json
from unittest.mock import patch

import pytest
from requests import Response

from apps.api_testing.case_runner import run_api_test_case
from apps.api_testing.case_tasks import execute_api_case_execution
from apps.api_testing.models import ApiCaseExecution, ApiTestCase, ApiTestCaseStep, RequestHistory
from apps.api_testing.test_execution_integration import graph
from apps.quality_intelligence.services.redaction import REDACTION_MARKER


def http_response(code, payload):
    result = Response()
    result.status_code = code
    result._content = json.dumps(payload).encode('utf-8')
    result.encoding = 'utf-8'
    result.headers['Content-Type'] = 'application/json'
    return result


def make_case(graph):
    return ApiTestCase.objects.create(project=graph.project, name='Evidence flow', created_by=graph.owner)


def test_persisted_case_snapshots_redact_echoed_and_overwritten_runtime_values(graph):
    case = make_case(graph)
    first_value, next_value, input_value = 'receipt-before-rotation', 'receipt-after-rotation', 'input-session-value'
    ApiTestCaseStep.objects.create(
        test_case=case, name='Expected negative response', request=graph.definition, order=0,
        assertions=[{'type': 'status_code', 'expected': 401}, {'type': 'json_path', 'json_path': '$.message', 'expected': first_value}],
        extractors=[{'name': 'receipt', 'source': 'json_path', 'expression': '$.message', 'required': True}],
    )
    ApiTestCaseStep.objects.create(
        test_case=case, name='Use and rotate value', request=graph.definition, order=1,
        request_overrides={'params': {'reply': '{{receipt}}', 'context': '{{session_ref}}'}},
        assertions=[{'type': 'status_code', 'expected': 200}],
        extractors=[{'name': 'receipt', 'source': 'json_path', 'expression': '$.next', 'required': True}],
    )
    execution = ApiCaseExecution.objects.create(
        project=graph.project, test_case=case, test_case_name=case.name, executed_by=graph.owner,
    )
    supplied = {'session_ref': input_value}
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', side_effect=[
            http_response(401, {'message': first_value}),
            http_response(200, {'echo': first_value, 'next': next_value, 'context': input_value, 'safe': 'visible'}),
        ],
    ) as transport:
        executed = execute_api_case_execution.run(execution.pk, supplied)
    assert executed['success'] is True
    assert transport.call_count == 2
    assert transport.call_args_list[1].kwargs['params']['reply'] == first_value
    assert transport.call_args_list[1].kwargs['params']['context'] == input_value
    execution.refresh_from_db()
    assert execution.status == 'COMPLETED'
    assert execution.passed_steps == 2
    assert execution.results[0]['response_data']['json']['message'] == REDACTION_MARKER
    assert execution.results[1]['response_data']['json']['echo'] == REDACTION_MARKER
    assert execution.results[1]['response_data']['json']['safe'] == 'visible'
    assert execution.results[0]['assertions_results'][1]['expected'] == REDACTION_MARKER
    assert execution.results[0]['extracted_variables'] == ['receipt']
    serialized = json.dumps(execution.results)
    assert all(value not in serialized for value in (first_value, next_value, input_value))
    assert '_raw_response' not in serialized
    assert not RequestHistory.objects.filter(request=graph.definition).exists()
    assert supplied == {'session_ref': input_value}
    graph.definition.refresh_from_db()
    case.refresh_from_db()
    assert graph.definition.params == {'source': 'template'}
    assert case.variables == {}


def test_required_extraction_runs_for_falsey_negative_response_objects(graph):
    case = make_case(graph)
    ApiTestCaseStep.objects.create(
        test_case=case, name='Required missing value', request=graph.definition, order=0,
        assertions=[{'type': 'status_code', 'expected': 401}],
        extractors=[{'name': 'receipt', 'source': 'json_path', 'expression': '$.missing', 'required': True}],
    )
    ApiTestCaseStep.objects.create(test_case=case, name='Must skip', request=graph.definition, order=1)
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=http_response(401, {}),
    ) as transport:
        result = run_api_test_case(case, None, graph.owner)
    assert result['success'] is False
    assert result['failed_count'] == 1
    assert result['skipped_count'] == 1
    assert 'receipt' in result['results'][0]['error']
    transport.assert_called_once()


def test_runtime_redaction_does_not_change_false_assertion_verdicts(graph):
    case = make_case(graph)
    ApiTestCaseStep.objects.create(
        test_case=case, name='False must remain failure', request=graph.definition,
        assertions=[{'type': 'json_path', 'json_path': '$.ok', 'expected': True}],
    )
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=http_response(200, {'ok': False}),
    ):
        result = run_api_test_case(case, None, graph.owner, variable_overrides={'flag': False})
    assert result['success'] is False
    assert result['results'][0]['assertions_results'][0]['passed'] is False


def test_transport_error_evidence_scrubs_values_under_nonsecret_names(graph):
    case = make_case(graph)
    ApiTestCaseStep.objects.create(test_case=case, name='Transport failure', request=graph.definition)
    value = 'opaque-runtime-identifier'
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', side_effect=RuntimeError(f'upstream echoed {value}'),
    ):
        result = run_api_test_case(case, None, graph.owner, variable_overrides={'reference': value})
    assert result['success'] is False
    assert value not in json.dumps(result)
    assert REDACTION_MARKER in result['results'][0]['error']


@pytest.mark.parametrize('disabled_step', [False, True])
def test_case_without_enabled_steps_is_a_failed_execution(graph, disabled_step):
    case = make_case(graph)
    if disabled_step:
        ApiTestCaseStep.objects.create(test_case=case, name='Disabled', request=graph.definition, enabled=False)
    with patch('apps.api_testing.utils.guarded_request') as transport:
        result = run_api_test_case(case, None, graph.owner)
    assert result['success'] is False
    assert result['error']
    assert result['skipped_count'] == int(disabled_step)
    transport.assert_not_called()
