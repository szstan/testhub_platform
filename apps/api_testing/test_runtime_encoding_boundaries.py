"""Root closure checks for pre-dispatch budgets and equivalent URL encodings."""
import json
from unittest.mock import patch

import pytest
from requests import Response

from apps.api_testing.case_tasks import execute_api_case_execution
from apps.api_testing.models import (
    ApiCaseExecution,
    ApiTestCase,
    ApiTestCaseStep,
    RequestHistory,
    TestExecution as ApiSuiteExecution,
    TestSuite as ApiTestSuite,
    TestSuiteRequest as ApiTestSuiteRequest,
)
from apps.api_testing.test_execution_integration import graph
from apps.api_testing.utils import execute_api_request, execute_test_suite
from apps.core.variable_resolver import ResolutionBudget, VariableResolver
from apps.quality_intelligence.services.redaction import REDACTION_MARKER


@pytest.mark.parametrize(
    ('function', 'arguments', 'factory', 'limit'),
    [
        ('base64', '"' + 'a' * 4000 + '"', 'EncryptionTools.base64_encode', 4096),
        ('base64_encode', '"' + 'a' * 4000 + '"', 'EncodingTools.base64_encode', 4096),
        (
            'base64_encode', json.dumps('😀' * 64, ensure_ascii=False) + ', "unicode_escape"',
            'EncodingTools.base64_encode', 512,
        ),
        ('case_convert', '"ß", "upper"', 'StringTools.case_convert', 1),
    ],
    ids=['base64-alias', 'base64-tool', 'unicode-escape', 'unicode-uppercase'],
)
def test_encoding_aliases_and_unicode_expansion_reserve_before_dispatch(function, arguments, factory, limit):
    expression = '${' + function + '(' + arguments + ')}'
    resolver = VariableResolver(budget=ResolutionBudget(max_output=limit))
    with patch(
        'apps.core.variable_resolver.' + factory,
        return_value={'success': True, 'result': 'unexpected-factory-output'},
    ) as generate:
        assert resolver.resolve(expression) == expression
    generate.assert_not_called()


@pytest.mark.parametrize('kind', ['request', 'case', 'suite'])
def test_mixed_case_percent_hex_runtime_values_are_redacted_from_persisted_evidence(graph, kind):
    graph.definition.method = 'POST'
    graph.definition.body = {'type': 'json', 'data': {'echo': 'prefix${random_string(4)}suffix'}}
    graph.definition.save(update_fields=['method', 'body'])
    plain_url = 'https://api.example.test/api/v1/items?left=Alpha&right=Beta'
    response = Response()
    response.status_code = 200
    response.encoding = 'utf-8'
    response.headers['Content-Type'] = 'application/json'
    response._content = json.dumps({
        'echo_lower_hex': 'prefixAb%2f1suffix',
        'echo_upper_hex': 'prefixAb%2F1suffix',
        'safe': plain_url,
    }).encode('utf-8')

    with patch(
        'apps.core.variable_resolver.RandomTools.random_string',
        return_value={'success': True, 'result': 'Ab/1'},
    ), patch(
        'apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url,
    ), patch('apps.api_testing.utils.guarded_request', return_value=response) as transport:
        if kind == 'case':
            case = ApiTestCase.objects.create(project=graph.project, name='encoded echo', created_by=graph.owner)
            ApiTestCaseStep.objects.create(test_case=case, request=graph.definition, name='echo')
            execution = ApiCaseExecution.objects.create(
                project=graph.project, test_case=case, test_case_name=case.name, executed_by=graph.owner,
            )
            result = execute_api_case_execution.run(execution.pk)
            execution.refresh_from_db()
            evidence = execution.results
        elif kind == 'suite':
            suite = ApiTestSuite.objects.create(project=graph.project, name='encoded suite', created_by=graph.owner)
            ApiTestSuiteRequest.objects.create(test_suite=suite, request=graph.definition)
            result = execute_test_suite(suite, None, graph.owner)
            execution = ApiSuiteExecution.objects.get(pk=result['execution_id'])
            history = RequestHistory.objects.get(request=graph.definition)
            evidence = {'suite': execution.results, 'request': history.request_data, 'response': history.response_data}
        else:
            result = execute_api_request(graph.definition, None, graph.owner)
            history = RequestHistory.objects.get(request=graph.definition)
            evidence = {'request': history.request_data, 'response': history.response_data}

    assert result['success'] is True, result
    assert transport.call_args.kwargs['json'] == {'echo': 'prefixAb/1suffix'}
    serialized = json.dumps(evidence, ensure_ascii=False)
    assert 'Ab%2f1' not in serialized
    assert 'Ab%2F1' not in serialized
    assert REDACTION_MARKER in serialized
    assert plain_url in serialized
