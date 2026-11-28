"""Queued API case snapshot and list-query regressions."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.api_testing.case_runner import run_api_test_case
from apps.api_testing.case_tasks import execute_api_case_execution
from apps.api_testing.models import (
    ApiCaseExecution,
    ApiCollection,
    ApiProject,
    ApiRequest,
    ApiTestCase,
    ApiTestCaseStep,
    Environment,
)
from apps.api_testing.test_api_cases import _api_workspace, _response


def _case_graph(username):
    user, project, collection, api_request = _api_workspace(username)
    environment = Environment.objects.create(
        name='queued environment',
        scope='LOCAL',
        project=project,
        variables={'host': 'api-a.example.test', 'credential': 'credential-a'},
        created_by=user,
    )
    case = ApiTestCase.objects.create(
        project=project,
        name='queued case',
        environment=environment,
        created_by=user,
    )
    ApiTestCaseStep.objects.create(test_case=case, name='send', request=api_request)
    client = APIClient()
    client.force_authenticate(user=user)
    return SimpleNamespace(
        user=user,
        project=project,
        collection=collection,
        api_request=api_request,
        environment=environment,
        case=case,
        client=client,
    )


@pytest.mark.django_db
def test_queued_source_edit_fails_before_transport_and_retry_never_reuses_changed_result():
    graph = _case_graph('api-case-queued-edit')
    graph.api_request.method = 'GET'
    graph.api_request.url = 'https://api-a.example.test/resource'
    graph.api_request.save(update_fields=['method', 'url'])
    case_url = f'/api/api-testing/test-cases/{graph.case.pk}/'
    execute_url = f'{case_url}execute/'
    fingerprint_a = graph.client.get(case_url).data['execution_fingerprint']

    def edit_sources_then_run(execution_id, variables, *, require_snapshot=False):
        ApiRequest.objects.filter(pk=graph.api_request.pk).update(
            url='https://api-b.example.test/changed',
            headers={'Authorization': 'credential-b'},
        )
        Environment.objects.filter(pk=graph.environment.pk).update(
            variables={'host': 'api-b.example.test', 'credential': 'credential-b'},
        )
        return execute_api_case_execution.run(
            execution_id,
            variables,
            require_snapshot=require_snapshot,
        )

    with patch(
        'apps.api_testing.case_tasks.execute_api_case_execution.delay',
        side_effect=edit_sources_then_run,
    ) as dispatch, patch(
        'apps.api_testing.utils.validate_outbound_url',
        side_effect=lambda url, **kwargs: url,
    ), patch('apps.api_testing.utils.guarded_request', return_value=_response()) as transport:
        submitted = graph.client.post(execute_url, {
            'idempotency_key': 'queued-source-a',
            'execution_fingerprint': fingerprint_a,
        }, format='json')

        assert submitted.status_code == 202, submitted.data
        dispatch.assert_called_once()
        execution = ApiCaseExecution.objects.get(pk=submitted.data['id'])
        assert len(execution.idempotency_payload_hash) == 64
        assert execution.status == 'FAILED'
        assert '配置已变化' in execution.error_message
        transport.assert_not_called()

        ApiRequest.objects.filter(pk=graph.api_request.pk).update(
            url='https://api-a.example.test/resource',
            headers={},
        )
        Environment.objects.filter(pk=graph.environment.pk).update(
            variables={'host': 'api-a.example.test', 'credential': 'credential-a'},
        )
        retry = graph.client.post(execute_url, {
            'idempotency_key': 'queued-source-a',
            'execution_fingerprint': fingerprint_a,
        }, format='json')

    assert retry.status_code == 200, retry.data
    assert retry.data['id'] == submitted.data['id']
    execution.refresh_from_db()
    assert execution.status == 'FAILED'
    assert 'api-b.example.test' not in str(execution.results)
    assert 'credential-b' not in execution.error_message
    transport.assert_not_called()


@pytest.mark.django_db
def test_worker_uses_frozen_inputs_when_request_and_environment_change_after_verification():
    graph = _case_graph('api-case-late-edit')
    graph.api_request.method = 'GET'
    graph.api_request.url = 'https://api-a.example.test/resource'
    graph.api_request.headers = {'X-Credential': '{{credential}}'}
    graph.api_request.save(update_fields=['method', 'url', 'headers'])
    graph.environment.variables = {'credential': 'credential-a'}
    graph.environment.save(update_fields=['variables'])
    case_url = f'/api/api-testing/test-cases/{graph.case.pk}/'
    execute_url = f'{case_url}execute/'
    fingerprint_a = graph.client.get(case_url).data['execution_fingerprint']
    sent = []

    def edit_after_freeze(test_case, environment, executed_by, **kwargs):
        ApiRequest.objects.filter(pk=graph.api_request.pk).update(
            url='https://api-b.example.test/changed',
            headers={'X-Credential': 'credential-b'},
        )
        Environment.objects.filter(pk=graph.environment.pk).update(
            variables={'credential': 'credential-b'},
        )
        return run_api_test_case(
            test_case,
            environment,
            executed_by,
            **kwargs,
        )

    with patch(
        'apps.api_testing.case_tasks.execute_api_case_execution.delay',
        side_effect=lambda execution_id, variables, *, require_snapshot=False: execute_api_case_execution.run(
            execution_id, variables, require_snapshot=require_snapshot,
        ),
    ), patch(
        'apps.api_testing.case_runner.run_api_test_case',
        side_effect=edit_after_freeze,
    ), patch(
        'apps.api_testing.utils.validate_outbound_url',
        side_effect=lambda url, **kwargs: url,
    ), patch(
        'apps.api_testing.utils.guarded_request',
        side_effect=lambda **kwargs: (sent.append(kwargs) or _response()),
    ):
        submitted = graph.client.post(execute_url, {
            'idempotency_key': 'late-source-edit',
            'execution_fingerprint': fingerprint_a,
        }, format='json')

    assert submitted.status_code == 202, submitted.data
    execution = ApiCaseExecution.objects.get(pk=submitted.data['id'])
    assert execution.status == 'COMPLETED', execution.error_message
    assert len(sent) == 1
    assert sent[0]['url'] == 'https://api-a.example.test/resource'
    assert sent[0]['headers']['X-Credential'] == 'credential-a'
    assert all('api-b.example.test' not in str(item) for item in sent)


@pytest.mark.django_db
def test_worker_rejects_legacy_incomplete_hash_before_transport():
    graph = _case_graph('api-case-legacy-incomplete-worker')
    graph.api_request.method = 'GET'
    graph.api_request.url = 'https://api.example.test/legacy'
    graph.api_request.save(update_fields=['method', 'url'])
    legacy_hash = hashlib.sha256(json.dumps(
        {'environment_id': graph.environment.pk, 'variables': {}},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).encode('utf-8')).hexdigest()
    execution = ApiCaseExecution.objects.create(
        project=graph.project,
        test_case=graph.case,
        test_case_name=graph.case.name,
        environment=graph.environment,
        executed_by=graph.user,
        idempotency_key='old-incomplete-run',
        idempotency_payload_hash=legacy_hash,
    )

    with patch('apps.api_testing.utils.guarded_request') as transport:
        outcome = execute_api_case_execution.run(execution.pk)

    execution.refresh_from_db()
    assert outcome['status'] == execution.status == 'FAILED'
    assert '配置' in execution.error_message
    transport.assert_not_called()


@pytest.mark.django_db
def test_fingerprint_and_queue_preserve_dynamic_templates_without_resolving_them():
    graph = _case_graph('api-case-template-freeze')
    graph.api_request.url = 'https://api.example.test/${random_uuid()}'
    graph.environment.variables = {'token': '${random_uuid()}', 'secret': '{{generated}}'}
    graph.api_request.save(update_fields=['url'])
    graph.environment.save(update_fields=['variables'])
    case_url = f'/api/api-testing/test-cases/{graph.case.pk}/'

    with patch('apps.api_testing.utils.VariableResolver.resolve') as resolve, patch(
        'apps.api_testing.case_tasks.execute_api_case_execution.delay',
    ) as dispatch:
        detail = graph.client.get(case_url)
        queued = graph.client.post(f'{case_url}execute/', {
            'idempotency_key': 'raw-dynamic-inputs',
            'execution_fingerprint': detail.data['execution_fingerprint'],
        }, format='json')

    assert detail.status_code == 200
    assert queued.status_code == 202, queued.data
    dispatch.assert_called_once()
    resolve.assert_not_called()


@pytest.mark.django_db
def test_case_list_fingerprinting_reuses_prefetched_steps_without_collection_n_plus_one():
    user = get_user_model().objects.create_user(username='api-case-fingerprint-query-count')
    project = ApiProject.objects.create(
        name='query project', project_type='HTTP', status='IN_PROGRESS', owner=user,
    )
    collection = ApiCollection.objects.create(name='query collection', project=project)
    api_request = ApiRequest.objects.create(
        collection=collection,
        name='query request',
        method='GET',
        url='https://api.example.test/resource',
        created_by=user,
    )
    for index in range(20):
        case = ApiTestCase.objects.create(project=project, name=f'query case {index}', created_by=user)
        ApiTestCaseStep.objects.create(test_case=case, request=api_request, name='step')

    client = APIClient()
    client.force_authenticate(user=user)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(f'/api/api-testing/test-cases/?project={project.pk}')

    assert response.status_code == 200, response.data
    assert response.data['count'] == 20
    collection_queries = [query['sql'] for query in queries if 'api_collections' in query['sql'].lower()]
    assert len(collection_queries) <= 1, collection_queries
    assert len(queries) <= 10, [query['sql'] for query in queries]
