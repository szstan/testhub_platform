"""Behavioral regressions for administrator access across mixed API suites."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.api_testing.models import (
    ApiCaseExecution, ApiProject, RequestHistory,
    TestExecution as SuiteExecution, TestSuite as Suite,
    TestSuiteCase as SuiteCase, TestSuiteRequest as SuiteRequest,
)
from apps.api_testing.test_execution_integration import create_case, graph, response


@pytest.fixture(params=['is_staff', 'is_superuser'])
def administrator(request):
    admin = get_user_model().objects.create_user(
        username=f'case-flow-{request.param}', **{request.param: True},
    )
    client = APIClient()
    client.force_authenticate(user=admin)
    return SimpleNamespace(user=admin, client=client)


@pytest.mark.parametrize('resource', [
    'projects', 'collections', 'requests', 'test-cases', 'test-suites',
    'test-suite-requests', 'test-suite-cases', 'test-executions', 'histories', 'case-executions',
])
def test_admin_can_read_case_definitions_members_and_evidence(graph, administrator, resource):
    case = create_case(graph)
    suite = Suite.objects.create(project=graph.project, name='Admin suite', created_by=graph.owner)
    old_member = SuiteRequest.objects.create(test_suite=suite, request=graph.definition)
    new_member = SuiteCase.objects.create(test_suite=suite, test_case=case, order=1)
    execution = SuiteExecution.objects.create(
        test_suite=suite, executed_by=graph.owner, status='COMPLETED', results=[],
    )
    history = RequestHistory.objects.create(
        request=graph.definition, executed_by=graph.owner, status_code=200,
        response_time=1, request_data={}, response_data={},
    )
    case_execution = ApiCaseExecution.objects.create(
        project=graph.project, test_case=case, test_case_name=case.name,
        executed_by=graph.owner, status='COMPLETED',
    )
    resources = {
        'projects': graph.project, 'collections': graph.collection,
        'requests': graph.definition, 'test-cases': case, 'test-suites': suite,
        'test-suite-requests': old_member, 'test-suite-cases': new_member,
        'test-executions': execution, 'histories': history, 'case-executions': case_execution,
    }
    result = administrator.client.get(f'/api/api-testing/{resource}/{resources[resource].pk}/')
    assert result.status_code == 200, result.data


def test_admin_can_edit_compose_execute_and_inspect_mixed_suite(graph, administrator):
    client = administrator.client
    case = create_case(graph)
    created = client.post('/api/api-testing/test-suites/', {
        'project': graph.project.pk, 'name': 'Admin-created suite',
    }, format='json')
    assert created.status_code == 201, created.data
    suite_id = created.data['id']
    edited = client.patch(f'/api/api-testing/test-suites/{suite_id}/', {
        'name': 'Admin-edited suite',
    }, format='json')
    assert edited.status_code == 200, edited.data
    added_request = client.post(f'/api/api-testing/test-suites/{suite_id}/add-requests/', {
        'request_ids': [graph.definition.pk],
    }, format='json')
    assert added_request.status_code == 200, added_request.data
    added_case = client.post(f'/api/api-testing/test-suites/{suite_id}/add-cases/', {
        'case_ids': [case.pk],
    }, format='json')
    assert added_case.status_code == 200, added_case.data
    member = SuiteRequest.objects.get(test_suite_id=suite_id, request=graph.definition)
    edited_member = client.patch(f'/api/api-testing/test-suite-requests/{member.pk}/', {
        'assertions': [{'type': 'status_code', 'expected': 200}],
    }, format='json')
    assert edited_member.status_code == 200, edited_member.data
    ordered = client.post(f'/api/api-testing/test-suites/{suite_id}/reorder/', {
        'items': [{'kind': 'case', 'id': case.pk}, {'kind': 'request', 'id': graph.definition.pk}],
    }, format='json')
    assert ordered.status_code == 200, ordered.data
    foreign_project = ApiProject.objects.create(
        name='Admin separate project', project_type='HTTP', status='IN_PROGRESS', owner=graph.owner,
    )
    invalid_move = client.patch(f'/api/api-testing/test-suites/{suite_id}/', {
        'project': foreign_project.pk,
    }, format='json')
    assert invalid_move.status_code == 400
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.api_testing.utils.guarded_request', side_effect=[response(401), response(200)],
    ):
        executed = client.post(f'/api/api-testing/test-suites/{suite_id}/execute/', {}, format='json')
    assert executed.status_code == 200, executed.data
    assert executed.data['passed_requests'] == 2
    assert [row['kind'] for row in executed.data['results']] == ['case', 'request']
    evidence = client.get(f"/api/api-testing/test-executions/{executed.data['id']}/")
    assert evidence.status_code == 200, evidence.data
    stats = client.get('/api/api-testing/dashboard/stats/')
    assert stats.status_code == 200
    assert stats.data['case_count'] == 1
    assert stats.data['suite_count'] == 1
    assert stats.data['project_count'] == 2
