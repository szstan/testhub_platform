"""Regressions at the legacy HTTP, suite and unified-execution integration boundaries."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from requests import Response as HTTPResponse
from rest_framework.test import APIClient

from apps.api_testing.models import (
    ApiCollection, ApiProject, ApiRequest, ApiTestCase, ApiTestCaseStep,
    RequestHistory, TestSuite as ApiSuite, TestSuiteCase as SuiteCase,
    TestSuiteRequest as SuiteRequest,
)
from apps.executions.source_workers import _api_execute


@pytest.fixture
def graph(db):
    owner = get_user_model().objects.create_user(username='api-case-integration-owner')
    project = ApiProject.objects.create(name='Integration project', project_type='HTTP', status='IN_PROGRESS', owner=owner)
    collection = ApiCollection.objects.create(project=project, name='HTTP definitions')
    definition = ApiRequest.objects.create(
        name='Login', collection=collection, method='POST', url='https://example.test/login',
        params={'source': 'template'}, headers={'X-Original': 'true'},
        body={'type': 'json', 'data': {'username': 'original'}},
        assertions=[{'type': 'status_code', 'expected': 200}], created_by=owner,
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    return SimpleNamespace(owner=owner, project=project, collection=collection, definition=definition, client=client)


def create_case(graph, name='Negative login'):
    case = ApiTestCase.objects.create(project=graph.project, name=name, created_by=graph.owner)
    ApiTestCaseStep.objects.create(test_case=case, request=graph.definition, name='Wrong password', assertions=[{'type': 'status_code', 'expected': 401}])
    return case


def response(code=200, body=None):
    return SimpleNamespace(
        status_code=code, headers={'content-type': 'application/json'},
        text=json.dumps(body if body is not None else {'ok': True}),
    )


def test_legacy_request_preview_uses_shared_transport_without_modifying_definition(graph):
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=response(401),
    ) as transport:
        result = graph.client.post(
            f'/api/api-testing/requests/{graph.definition.pk}/execute/',
            {'body': {'type': 'raw', 'data': 'wrong-password'}, 'assertions': [{'type': 'status_code', 'expected': 401}]},
            format='json',
        )
    assert result.status_code == 200
    assert result.data['passed'] is True
    assert transport.call_args.kwargs['data'] == 'wrong-password'
    assert 'json' not in transport.call_args.kwargs
    graph.definition.refresh_from_db()
    assert graph.definition.body == {'type': 'json', 'data': {'username': 'original'}}
    assert graph.definition.assertions == [{'type': 'status_code', 'expected': 200}]


def test_legacy_preview_keeps_falsy_json_bodies_and_persists_assertion_failure(graph):
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=response(500),
    ) as transport:
        result = graph.client.post(
            f'/api/api-testing/requests/{graph.definition.pk}/execute/',
            {'body': {'type': 'json', 'data': False}}, format='json',
        )
    assert result.status_code == 200
    assert transport.call_args.kwargs['json'] is False
    assert result.data['passed'] is False
    assert result.data['assertions_results'][0]['passed'] is False
    assert RequestHistory.objects.get(pk=result.data['id']).assertions_results[0]['passed'] is False


def test_legacy_transport_failure_returns_redacted_history_and_an_error(graph):
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', side_effect=RuntimeError('transport unavailable'),
    ):
        result = graph.client.post(f'/api/api-testing/requests/{graph.definition.pk}/execute/', {}, format='json')
    assert result.status_code == 400
    assert result.data['passed'] is False
    assert 'transport unavailable' in result.data['error_message']
    assert RequestHistory.objects.filter(pk=result.data['id']).exists()


def test_mixed_suite_http_execution_obeys_global_order_and_negative_case_assertions(graph):
    case = create_case(graph)
    suite = ApiSuite.objects.create(project=graph.project, name='Mixed suite', created_by=graph.owner)
    SuiteRequest.objects.create(test_suite=suite, request=graph.definition, order=1)
    SuiteCase.objects.create(test_suite=suite, test_case=case, order=0)
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', side_effect=[response(401), response(200)],
    ) as transport:
        result = graph.client.post(f'/api/api-testing/test-suites/{suite.pk}/execute/', {}, format='json')
    assert result.status_code == 200
    assert transport.call_count == 2
    assert result.data['status'] == 'COMPLETED'
    assert result.data['total_requests'] == 2
    assert result.data['passed_requests'] == 2
    assert [row['kind'] for row in result.data['results']] == ['case', 'request']
    assert result.data['results'][0]['results'][0]['status_code'] == 401


def test_suite_edited_assertions_are_used_by_the_shared_http_execution(graph):
    suite = ApiSuite.objects.create(project=graph.project, name='Request suite', created_by=graph.owner)
    member = SuiteRequest.objects.create(test_suite=suite, request=graph.definition)
    saved = graph.client.patch(
        f'/api/api-testing/test-suite-requests/{member.pk}/',
        {'assertions': [{'type': 'json_path', 'json_path': '$.ok', 'expected': False}]}, format='json',
    )
    assert saved.status_code == 200
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=response(200, {'ok': True}),
    ):
        result = graph.client.post(f'/api/api-testing/test-suites/{suite.pk}/execute/', {}, format='json')
    assert result.status_code == 200
    assert result.data['status'] == 'FAILED'
    assert result.data['failed_requests'] == 1
    assert any(not item['passed'] for item in result.data['results'][0]['assertions_results'])


def test_appending_an_old_request_after_a_case_at_order_zero_preserves_the_case_first(graph):
    suite = ApiSuite.objects.create(project=graph.project, name='Append suite', created_by=graph.owner)
    SuiteCase.objects.create(test_suite=suite, test_case=create_case(graph), order=0)
    result = graph.client.post(
        f'/api/api-testing/test-suites/{suite.pk}/add-requests/',
        {'request_ids': [graph.definition.pk, graph.definition.pk]}, format='json',
    )
    assert result.status_code == 200
    assert SuiteRequest.objects.filter(test_suite=suite).count() == 1
    assert SuiteRequest.objects.get(test_suite=suite).order == 1


@pytest.mark.parametrize('resource', ['requests', 'collections', 'projects'])
def test_request_case_references_return_conflict_for_destructive_api_calls(graph, resource):
    case = create_case(graph)
    instance = {'requests': graph.definition, 'collections': graph.collection, 'projects': graph.project}[resource]
    result = graph.client.delete(f'/api/api-testing/{resource}/{instance.pk}/')
    assert result.status_code == 409
    assert ApiTestCase.objects.filter(pk=case.pk).exists()
    assert ApiRequest.objects.filter(pk=graph.definition.pk).exists()


@pytest.mark.parametrize('resource', ['requests', 'collections'])
def test_request_case_references_prevent_cross_project_definition_moves(graph, resource):
    create_case(graph)
    other = ApiProject.objects.create(name='Other project', project_type='HTTP', status='IN_PROGRESS', owner=graph.owner)
    target = ApiCollection.objects.create(project=other, name='Target')
    if resource == 'requests':
        result = graph.client.patch(f'/api/api-testing/requests/{graph.definition.pk}/', {'collection': target.pk}, format='json')
    else:
        result = graph.client.patch(f'/api/api-testing/collections/{graph.collection.pk}/', {'project': other.pk}, format='json')
    assert result.status_code == 400
    graph.definition.refresh_from_db()
    graph.collection.refresh_from_db()
    assert graph.definition.collection_id == graph.collection.pk
    assert graph.collection.project_id == graph.project.pk


def test_dashboard_case_count_is_scoped_to_project_access(graph):
    create_case(graph)
    outsider = get_user_model().objects.create_user(username='api-case-count-outsider')
    hidden_project = ApiProject.objects.create(name='Hidden', project_type='HTTP', status='IN_PROGRESS', owner=outsider)
    ApiTestCase.objects.create(project=hidden_project, name='Hidden case', created_by=outsider)
    result = graph.client.get('/api/api-testing/dashboard/stats/')
    assert result.status_code == 200
    assert result.data['case_count'] == 1
    assert result.data['project_count'] == 1


@pytest.mark.parametrize('assertions, expected', [
    ([{'passed': False}], False), ([{'passed': True}], True), ([], True),
])
def test_unified_single_request_execution_honors_assertion_verdict(graph, assertions, expected):
    with patch('apps.api_testing.utils.execute_api_request', return_value={'success': True, 'assertions_results': assertions}):
        success, _ = _api_execute(graph.definition, SimpleNamespace(kind='request'), graph.owner)
    assert success is expected


@pytest.mark.parametrize('status_code', [401, 500])
def test_suite_assertions_still_run_for_negative_http_responses(graph, status_code):
    graph.definition.assertions = [{'type': 'status_code', 'expected': status_code}]
    graph.definition.save(update_fields=['assertions'])
    suite = ApiSuite.objects.create(project=graph.project, name='Negative HTTP suite', created_by=graph.owner)
    SuiteRequest.objects.create(
        test_suite=suite, request=graph.definition,
        assertions=[{'type': 'json_path', 'json_path': '$.ok', 'expected': False}],
    )
    negative_response = HTTPResponse()
    negative_response.status_code = status_code
    negative_response._content = b'{"ok":true}'
    negative_response.encoding = 'utf-8'
    negative_response.headers['Content-Type'] = 'application/json'
    # requests.Response is falsey for 4xx/5xx, but its body must still be asserted.
    assert not negative_response
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', return_value=negative_response,
    ):
        result = graph.client.post(f'/api/api-testing/test-suites/{suite.pk}/execute/', {}, format='json')
    assert result.status_code == 200, result.data
    assert result.data['status'] == 'FAILED'
    assert result.data['failed_requests'] == 1
    assertions = result.data['results'][0]['assertions_results']
    assert assertions[0]['passed'] is True
    assert assertions[1]['type'] == 'json_path'
    assert assertions[1]['passed'] is False
