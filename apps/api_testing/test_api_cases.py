import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.api_testing.case_runner import run_api_test_case
from apps.api_testing.case_views import request_has_case_step_references
from apps.api_testing.models import (
    ApiCaseExecution,
    ApiCollection,
    ApiProject,
    ApiRequest,
    ApiTestCase,
    ApiTestCaseStep,
    Environment,
    TestExecution as ApiTestExecution,
    TestSuite as ApiTestSuite,
    TestSuiteCase as ApiTestSuiteCase,
    TestSuiteRequest as ApiTestSuiteRequest,
)
from apps.api_testing.utils import execute_api_request, execute_test_suite


def _api_workspace(username='api-case-owner'):
    user = get_user_model().objects.create_user(username=username, password='pass')
    project = ApiProject.objects.create(
        name=f'{username} project', project_type='HTTP', status='IN_PROGRESS', owner=user,
    )
    collection = ApiCollection.objects.create(name='collection', project=project)
    api_request = ApiRequest.objects.create(
        collection=collection, name='get resource', method='GET',
        url='https://api.example.test/resource', created_by=user,
    )
    return user, project, collection, api_request


def _response(status_code=200, body='{}', headers=None):
    return SimpleNamespace(
        status_code=status_code,
        headers=headers or {'content-type': 'application/json'},
        text=body,
    )


@pytest.mark.django_db
def test_case_crud_is_project_scoped_and_step_patch_is_atomic():
    user, project, _, same_request = _api_workspace('api-case-crud')
    other, foreign_project, _, foreign_request = _api_workspace('api-case-foreign')
    client = APIClient()
    client.force_authenticate(user=user)

    created = client.post('/api/api-testing/test-cases/', {
        'project': project.pk,
        'name': 'case',
        'steps': [{'name': 'first', 'request': same_request.pk}],
    }, format='json')
    assert created.status_code == 201, created.data
    case_id = created.data['id']
    assert len(created.data['steps']) == 1

    invalid_patch = client.patch(f'/api/api-testing/test-cases/{case_id}/', {
        'steps': [
            {'name': 'first', 'request': same_request.pk},
            {'name': 'foreign', 'request': foreign_request.pk},
        ],
    }, format='json')
    assert invalid_patch.status_code == 400
    assert ApiTestCaseStep.objects.filter(test_case_id=case_id).count() == 1

    invalid_assertion = client.post('/api/api-testing/test-cases/', {
        'project': project.pk,
        'name': 'invalid assertion',
        'steps': [{
            'name': 'script must be rejected',
            'request': same_request.pk,
            'assertions': [{'type': 'javascript', 'script': 'return true'}],
        }],
    }, format='json')
    assert invalid_assertion.status_code == 400
    assert not ApiTestCase.objects.filter(name='invalid assertion').exists()

    invalid_json = client.generic(
        'POST', '/api/api-testing/test-cases/', data='{"project":', content_type='application/json',
    )
    assert invalid_json.status_code == 400

    filtered = client.get(f'/api/api-testing/test-cases/?project={foreign_project.pk}')
    assert filtered.status_code == 200
    assert filtered.data['count'] == 0

    outsider = APIClient()
    outsider.force_authenticate(user=other)
    assert outsider.get(f'/api/api-testing/test-cases/{case_id}/').status_code == 404


@pytest.mark.django_db
def test_case_duplicate_is_draft_and_copies_steps_and_delete_rejects_suite_reference():
    user, project, _, api_request = _api_workspace('api-case-duplicate')
    case = ApiTestCase.objects.create(project=project, name='source', status='active', created_by=user)
    ApiTestCaseStep.objects.create(test_case=case, request=api_request, name='step', order=0)
    client = APIClient()
    client.force_authenticate(user=user)

    duplicated = client.post(f'/api/api-testing/test-cases/{case.pk}/duplicate/', {}, format='json')
    assert duplicated.status_code == 201, duplicated.data
    copied = ApiTestCase.objects.get(pk=duplicated.data['id'])
    assert copied.status == 'draft'
    assert copied.name == 'source (copy)'
    assert copied.steps.count() == 1

    suite = ApiTestSuite.objects.create(project=project, name='suite', created_by=user)
    ApiTestSuiteCase.objects.create(test_suite=suite, test_case=copied)
    deleted = client.delete(f'/api/api-testing/test-cases/{copied.pk}/')
    assert deleted.status_code == 409
    assert ApiTestCase.objects.filter(pk=copied.pk).exists()
    assert request_has_case_step_references(api_request.pk)


@pytest.mark.parametrize('operation', ['delete', 'duplicate'])
@pytest.mark.django_db
def test_case_mutations_revalidate_project_access_after_lock(operation):
    from apps.api_testing.case_views import ApiTestCaseViewSet

    user, project, _, _ = _api_workspace(f'api-case-access-race-{operation}')
    foreign_owner = get_user_model().objects.create_user(username=f'api-case-private-{operation}')
    private_project = ApiProject.objects.create(
        name='private project', project_type='HTTP', status='IN_PROGRESS', owner=foreign_owner,
    )
    case = ApiTestCase.objects.create(project=project, name='racing case', created_by=user)
    client = APIClient()
    client.force_authenticate(user=user)
    original_get_object = ApiTestCaseViewSet.get_object

    def move_case_after_lookup(view):
        source = original_get_object(view)
        ApiTestCase.objects.filter(pk=source.pk).update(project=private_project)
        return source

    with patch.object(
        ApiTestCaseViewSet, 'get_object', autospec=True, side_effect=move_case_after_lookup,
    ):
        if operation == 'delete':
            response = client.delete(f'/api/api-testing/test-cases/{case.pk}/')
        else:
            response = client.post(
                f'/api/api-testing/test-cases/{case.pk}/duplicate/', {}, format='json',
            )

    assert response.status_code == 404
    assert ApiTestCase.objects.filter(pk=case.pk).exists()
    assert ApiTestCase.objects.count() == 1


@pytest.mark.parametrize('operation', ['create', 'update'])
@pytest.mark.django_db
def test_case_serializer_revalidates_step_project_after_validation(operation):
    from rest_framework.exceptions import ValidationError

    from apps.api_testing.case_serializers import ApiTestCaseSerializer

    user, project, _, api_request = _api_workspace(f'api-case-step-race-{operation}')
    foreign_project = ApiProject.objects.create(
        name='second project', project_type='HTTP', status='IN_PROGRESS', owner=user,
    )
    foreign_collection = ApiCollection.objects.create(name='second collection', project=foreign_project)
    case = None
    if operation == 'update':
        case = ApiTestCase.objects.create(project=project, name='existing case', created_by=user)
        ApiTestCaseStep.objects.create(test_case=case, request=api_request, name='step', order=0)

    request_context = SimpleNamespace(user=user)
    payload = {'steps': [{'name': 'step', 'request': api_request.pk}]}
    if operation == 'create':
        payload.update({'project': project.pk, 'name': 'new case'})
        serializer = ApiTestCaseSerializer(data=payload, context={'request': request_context})
    else:
        serializer = ApiTestCaseSerializer(
            case, data=payload, partial=True, context={'request': request_context},
        )
    assert serializer.is_valid(), serializer.errors

    ApiRequest.objects.filter(pk=api_request.pk).update(collection=foreign_collection)
    with pytest.raises(ValidationError):
        serializer.save()

    if operation == 'create':
        assert not ApiTestCase.objects.filter(name='new case').exists()
    else:
        case.refresh_from_db()
        assert case.project_id == project.pk


@pytest.mark.django_db
def test_case_execute_dispatch_is_idempotent_and_dispatch_failure_is_persisted():
    user, project, _, _ = _api_workspace('api-case-dispatch')
    case = ApiTestCase.objects.create(project=project, name='queued', created_by=user)
    client = APIClient()
    client.force_authenticate(user=user)
    url = f'/api/api-testing/test-cases/{case.pk}/execute/'
    payload = {'variables': {'token': 'runtime-only'}, 'idempotency_key': 'same-run'}

    with patch('apps.api_testing.case_tasks.execute_api_case_execution.delay') as dispatch:
        first = client.post(url, payload, format='json')
        second = client.post(url, payload, format='json')
    assert first.status_code == 202, first.data
    assert second.status_code == 200, second.data
    assert first.data['id'] == second.data['id']
    dispatch.assert_called_once()

    failure_case = ApiTestCase.objects.create(project=project, name='broker failure', created_by=user)
    with patch(
        'apps.api_testing.case_tasks.execute_api_case_execution.delay',
        side_effect=RuntimeError('broker unavailable'),
    ):
        failed = client.post(
            f'/api/api-testing/test-cases/{failure_case.pk}/execute/',
            {},
            format='json',
        )
    assert failed.status_code == 503
    assert failed.data['status'] == 'FAILED'
    assert ApiCaseExecution.objects.get(pk=failed.data['id']).status == 'FAILED'

    history = client.get(f'/api/api-testing/test-cases/{case.pk}/executions/')
    assert history.status_code == 200
    assert history.data['count'] == 1
    execution = client.get(f"/api/api-testing/case-executions/{first.data['id']}/")
    assert execution.status_code == 200


@pytest.mark.parametrize(('target', 'field', 'updated'), [
    ('request', 'url', 'https://api.example.test/v2/${random_uuid()}'),
    ('request', 'headers', {'Authorization': 'new-header-secret'}),
    ('request', 'params', {'page': 2}),
    ('request', 'body', {'type': 'json', 'data': {'password': 'new-body-secret'}}),
    ('request', 'auth', {'type': 'bearer', 'token': 'new-auth-secret'}),
    ('environment', 'variables', {'host': 'new-host', 'dynamic': '${random_uuid()}'}),
])
@pytest.mark.django_db
def test_case_execution_fingerprint_tracks_in_place_request_and_environment_edits(target, field, updated):
    user, project, collection, api_request = _api_workspace(f'api-case-fingerprint-{target}-{field}')
    api_request.method = 'POST'
    api_request.url = 'https://api.example.test/v1/${random_uuid()}'
    api_request.headers = {'Authorization': 'old-header-secret'}
    api_request.params = {'page': 1}
    api_request.body = {'type': 'json', 'data': {'password': 'old-body-secret'}}
    api_request.auth = {'type': 'bearer', 'token': 'old-auth-secret'}
    api_request.save()
    environment = Environment.objects.create(
        name='fingerprint environment', scope='LOCAL', project=project,
        variables={'host': 'old-host', 'private': 'old-env-secret', 'dynamic': '${random_uuid()}'},
        created_by=user,
    )
    case = ApiTestCase.objects.create(
        project=project, name='fingerprint case', environment=environment, created_by=user,
    )
    ApiTestCaseStep.objects.create(test_case=case, request=api_request, name='step')
    client = APIClient()
    client.force_authenticate(user=user)
    url = f'/api/api-testing/test-cases/{case.pk}/'

    first = client.get(url)
    repeated = client.get(url)
    assert first.status_code == repeated.status_code == 200
    original_fingerprint = first.data['execution_fingerprint']
    assert len(original_fingerprint) == 64
    assert repeated.data['execution_fingerprint'] == original_fingerprint
    assert all(secret not in json.dumps(first.data) for secret in (
        'old-header-secret', 'old-body-secret', 'old-auth-secret', 'old-env-secret',
    ))

    model = ApiRequest if target == 'request' else Environment
    instance = api_request if target == 'request' else environment
    model.objects.filter(pk=instance.pk).update(**{field: updated})
    changed = client.get(url)
    assert changed.status_code == 200
    assert changed.data['execution_fingerprint'] != original_fingerprint


@pytest.mark.django_db
def test_case_execution_fingerprint_includes_optional_environment_default_headers_without_resolving_them():
    from apps.api_testing.case_execution_snapshot import api_case_execution_fingerprint

    user, project, _collection, _api_request = _api_workspace('api-case-default-headers')
    environment = Environment.objects.create(
        name='headers', scope='LOCAL', project=project,
        variables={'generated': '${random_uuid()}'}, created_by=user,
    )
    case = ApiTestCase.objects.create(project=project, name='headers case', created_by=user)
    environment.default_headers = {'Authorization': 'first-private-header'}
    first = api_case_execution_fingerprint(case, environment=environment)
    environment.default_headers = {'Authorization': 'second-private-header'}
    second = api_case_execution_fingerprint(case, environment=environment)
    assert first != second
    assert 'first-private-header' not in first
    assert 'second-private-header' not in second


@pytest.mark.django_db
def test_case_execution_fingerprint_guards_stale_context_and_old_idempotency_keys():
    user, project, collection, api_request = _api_workspace('api-case-fingerprint-race')
    api_request.method = 'POST'
    api_request.url = 'https://api.example.test/v1/${random_uuid()}'
    api_request.headers = {'Authorization': 'request-secret'}
    api_request.params = {'page': 1}
    api_request.body = {'type': 'json', 'data': {'password': 'body-secret'}}
    api_request.auth = {'type': 'bearer', 'token': 'auth-secret'}
    api_request.save()
    environment = Environment.objects.create(
        name='QA', scope='LOCAL', project=project,
        variables={'host': 'old-host', 'password': 'environment-secret'}, created_by=user,
    )
    case = ApiTestCase.objects.create(
        project=project, name='login', environment=environment, created_by=user,
    )
    ApiTestCaseStep.objects.create(test_case=case, request=api_request, name='login')
    client = APIClient()
    client.force_authenticate(user=user)
    case_url = f'/api/api-testing/test-cases/{case.pk}/'
    execute_url = f'{case_url}execute/'

    context_a = client.get(case_url)
    fingerprint_a = context_a.data['execution_fingerprint']
    assert all(secret not in json.dumps(context_a.data) for secret in (
        'request-secret', 'body-secret', 'auth-secret', 'environment-secret',
    ))
    payload_a = {'idempotency_key': 'same-run', 'execution_fingerprint': fingerprint_a}
    with patch('apps.api_testing.case_tasks.execute_api_case_execution.delay') as dispatch:
        first = client.post(execute_url, payload_a, format='json')
        retry = client.post(execute_url, payload_a, format='json')
    assert first.status_code == 202, first.data
    assert retry.status_code == 200, retry.data
    assert first.data['id'] == retry.data['id']
    dispatch.assert_called_once()

    ApiRequest.objects.filter(pk=api_request.pk).update(
        url='https://api.example.test/v2/${random_uuid()}',
        headers={'Authorization': 'changed-request-secret'},
        params={'page': 2},
        body={'type': 'json', 'data': {'password': 'changed-body-secret'}},
        auth={'type': 'bearer', 'token': 'changed-auth-secret'},
    )
    Environment.objects.filter(pk=environment.pk).update(
        variables={'host': 'new-host', 'password': 'changed-environment-secret'},
    )
    context_b = client.get(case_url)
    fingerprint_b = context_b.data['execution_fingerprint']
    assert fingerprint_b != fingerprint_a

    with patch('apps.api_testing.case_tasks.execute_api_case_execution.delay') as dispatch:
        stale_context = client.post(execute_url, {
            'idempotency_key': 'stale-context', 'execution_fingerprint': fingerprint_a,
        }, format='json')
        old_key = client.post(execute_url, {
            'idempotency_key': 'same-run', 'execution_fingerprint': fingerprint_b,
        }, format='json')
        fresh = client.post(execute_url, {
            'idempotency_key': 'new-run', 'execution_fingerprint': fingerprint_b,
        }, format='json')
    assert stale_context.status_code == 409
    assert old_key.status_code == 409
    assert fresh.status_code == 202, fresh.data
    assert fresh.data['id'] != first.data['id']
    dispatch.assert_called_once()


@pytest.mark.django_db
def test_legacy_idempotency_records_are_not_reused_without_a_snapshot_digest():
    user, project, _, _ = _api_workspace('api-case-legacy-idempotency')
    environment = Environment.objects.create(
        name='legacy env', scope='LOCAL', project=project, variables={'base': 'old'}, created_by=user,
    )
    case = ApiTestCase.objects.create(
        project=project, name='legacy case', environment=environment, created_by=user,
    )
    legacy_hash = hashlib.sha256(json.dumps(
        {'environment_id': environment.pk, 'variables': {}},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).encode('utf-8')).hexdigest()
    ApiCaseExecution.objects.create(
        project=project, test_case=case, test_case_name=case.name, environment=environment,
        executed_by=user, idempotency_key='legacy-key', idempotency_payload_hash=legacy_hash,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(
        f'/api/api-testing/test-cases/{case.pk}/execute/',
        {'idempotency_key': 'legacy-key'}, format='json',
    )
    assert response.status_code == 409


@pytest.mark.django_db
def test_case_runner_variable_precedence_extraction_and_ephemeral_overrides():
    user, project, collection, _ = _api_workspace('api-case-runner')
    environment = Environment.objects.create(
        name='local', scope='LOCAL', project=project,
        variables={'base': 'environment', 'trace': 'environment'}, created_by=user,
    )
    first_request = ApiRequest.objects.create(
        collection=collection, name='create', method='GET',
        url='https://api.example.test/{{base}}',
        assertions=[{'type': 'status_code', 'expected': 200}], created_by=user,
    )
    second_request = ApiRequest.objects.create(
        collection=collection, name='lookup', method='GET',
        url='https://api.example.test/{{token}}?trace={{trace}}',
        headers={'X-Base': '{{base}}'}, params={}, created_by=user,
    )
    case = ApiTestCase.objects.create(
        project=project, name='two steps', environment=environment,
        variables={'base': 'case', 'trace': 'case'}, created_by=user,
    )
    ApiTestCaseStep.objects.create(
        test_case=case, name='create', request=first_request, order=0,
        extractors=[{
            'name': 'token', 'source': 'json_path', 'expression': '$.token', 'required': True,
        }],
    )
    ApiTestCaseStep.objects.create(
        test_case=case, name='lookup', request=second_request, order=1,
        request_overrides={'params': {'extra': '{{trace}}'}, 'headers': {'X-Step': 'on'}},
        assertions=[],
    )
    calls = []
    responses = [
        _response(body='{"token":"runtime-secret-value"}'),
        _response(body='{"ok":true}'),
    ]

    def send(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), \
         patch('apps.api_testing.utils.guarded_request', side_effect=send):
        result = run_api_test_case(
            case, environment, user,
            variable_overrides={'base': 'call', 'trace': 'run'},
        )

    assert result['success'] is True
    assert [item['status'] for item in result['results']] == ['passed', 'passed']
    assert '{{base}}' not in result['results'][0]['url']
    assert calls[0]['url'] == 'https://api.example.test/call'
    assert calls[1]['url'] == 'https://api.example.test/runtime-secret-value?trace=run'
    assert calls[1]['headers']['X-Base'] == 'call'
    assert calls[1]['headers']['X-Step'] == 'on'
    assert calls[1]['params'] == {'extra': 'run'}
    assert 'runtime-secret-value' not in json.dumps(result)
    first_request.refresh_from_db()
    second_request.refresh_from_db()
    assert first_request.url == 'https://api.example.test/{{base}}'
    assert second_request.headers == {'X-Base': '{{base}}'}
    assert second_request.params == {}


@pytest.mark.django_db
def test_case_runner_assertion_failure_stops_and_unresolved_variables_never_send():
    user, project, collection, _ = _api_workspace('api-case-stop')
    failed_request = ApiRequest.objects.create(
        collection=collection, name='negative', method='GET',
        url='https://api.example.test/negative',
        assertions=[{'type': 'status_code', 'expected': 201}], created_by=user,
    )
    next_request = ApiRequest.objects.create(
        collection=collection, name='next', method='GET',
        url='https://api.example.test/{{missing}}', created_by=user,
    )
    case = ApiTestCase.objects.create(project=project, name='stop', stop_on_failure=True, created_by=user)
    ApiTestCaseStep.objects.create(test_case=case, name='fail', request=failed_request, order=0)
    ApiTestCaseStep.objects.create(test_case=case, name='never send', request=next_request, order=1)
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), \
         patch('apps.api_testing.utils.guarded_request', return_value=_response(status_code=200)) as send:
        result = run_api_test_case(case, None, user)
    assert result['success'] is False
    assert result['failed_count'] == 1
    assert result['skipped_count'] == 1
    send.assert_called_once()


@pytest.mark.django_db
def test_case_runner_blocks_unresolved_variables_before_transport():
    user, project, collection, _ = _api_workspace('api-case-unresolved')
    request = ApiRequest.objects.create(
        collection=collection, name='unresolved', method='GET',
        url='https://api.example.test/{{missing}}', created_by=user,
    )
    case = ApiTestCase.objects.create(project=project, name='unresolved case', created_by=user)
    ApiTestCaseStep.objects.create(test_case=case, name='unresolved', request=request, order=0)
    with patch('apps.api_testing.utils.guarded_request') as send:
        result = run_api_test_case(case, None, user)
    assert result['success'] is False
    assert result['failed_count'] == 1
    send.assert_not_called()


@pytest.mark.parametrize('enabled', [None, False])
@pytest.mark.django_db
def test_case_without_enabled_steps_is_not_reported_as_success(enabled):
    user, project, _collection, api_request = _api_workspace(f'api-case-no-enabled-{enabled}')
    case = ApiTestCase.objects.create(project=project, name='empty case', created_by=user)
    if enabled is not None:
        ApiTestCaseStep.objects.create(
            test_case=case, request=api_request, name='disabled', order=0, enabled=enabled,
        )

    with patch('apps.api_testing.utils.guarded_request') as send:
        result = run_api_test_case(case, None, user)

    assert result['success'] is False
    assert result['error']
    assert result['passed_count'] == 0
    assert send.call_count == 0


@pytest.mark.django_db
def test_single_request_body_modes_and_overrides_use_guarded_shared_transport():
    user, _, _, api_request = _api_workspace('api-case-body-modes')
    api_request.method = 'POST'
    api_request.save(update_fields=['method'])
    cases = [
        ({'type': 'json', 'data': {'value': '{{token}}'}}, 'json', {'value': 'resolved'}),
        ({'type': 'raw', 'data': 'value={{token}}'}, 'data', 'value=resolved'),
        ({'type': 'x-www-form-urlencoded', 'data': [{'key': 'value', 'value': '{{token}}'}]}, 'data', [('value', 'resolved')]),
        ({'type': 'form-data', 'data': [{'key': 'value', 'value': '{{token}}'}]}, 'files', [('value', (None, 'resolved'))]),
    ]
    for body, expected_key, expected_value in cases:
        with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), \
             patch('apps.api_testing.utils.guarded_request', return_value=_response()) as send:
            result = execute_api_request(
                api_request, None, user,
                variable_overrides={'token': 'resolved'},
                request_overrides={'body': body},
                record_history=False,
            )
        assert result['success'] is True
        assert send.call_args.kwargs[expected_key] == expected_value


@pytest.mark.django_db
def test_linked_general_testcase_requires_project_access():
    from apps.projects.models import Project
    from apps.testcases.models import TestCase

    user, project, _, _ = _api_workspace('api-case-linked')
    other = get_user_model().objects.create_user(username='api-case-linked-outsider', password='pass')
    general_project = Project.objects.create(name='foreign general project', owner=other)
    linked = TestCase.objects.create(
        project=general_project,
        title='private test case',
        expected_result='done',
        author=other,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post('/api/api-testing/test-cases/', {
        'project': project.pk,
        'name': 'linked case',
        'linked_testcase': linked.pk,
    }, format='json')
    assert response.status_code == 400
    assert not ApiTestCase.objects.filter(name='linked case').exists()


@pytest.mark.django_db
def test_case_runner_fails_required_extraction_and_skips_remaining_steps():
    user, project, collection, _ = _api_workspace('api-case-extract-missing')
    first_request = ApiRequest.objects.create(
        collection=collection, name='missing data', method='GET',
        url='https://api.example.test/first', created_by=user,
    )
    next_request = ApiRequest.objects.create(
        collection=collection, name='must skip', method='GET',
        url='https://api.example.test/second', created_by=user,
    )
    case = ApiTestCase.objects.create(project=project, name='missing extractor', created_by=user)
    ApiTestCaseStep.objects.create(
        test_case=case, name='missing', request=first_request, order=0,
        extractors=[{'name': 'token', 'source': 'json_path', 'expression': '$.token', 'required': True}],
    )
    ApiTestCaseStep.objects.create(test_case=case, name='skip', request=next_request, order=1)
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), \
         patch('apps.api_testing.utils.guarded_request', return_value=_response()) as send:
        result = run_api_test_case(case, None, user)
    assert result['success'] is False
    assert result['failed_count'] == 1
    assert result['skipped_count'] == 1
    assert '缺少必需提取变量' in result['results'][0]['error']
    send.assert_called_once()


@pytest.mark.django_db
def test_case_task_persists_terminal_counts_and_ignores_duplicate_delivery():
    from apps.api_testing.case_tasks import execute_api_case_execution

    user, project, _, _ = _api_workspace('api-case-task')
    case = ApiTestCase.objects.create(project=project, name='task case', created_by=user)
    execution = ApiCaseExecution.objects.create(
        project=project, test_case=case, test_case_name=case.name, executed_by=user,
    )
    runner_result = {
        'success': True,
        'total_count': 2,
        'passed_count': 1,
        'failed_count': 0,
        'skipped_count': 1,
        'results': [{'name': 'step', 'status': 'passed'}],
    }
    with patch('apps.api_testing.case_runner.run_api_test_case', return_value=runner_result) as runner:
        first = execute_api_case_execution.run(execution.pk, {'runtime-secret': 'not stored'})
        second = execute_api_case_execution.run(execution.pk, {'runtime-secret': 'not stored'})
    execution.refresh_from_db()
    assert first['status'] == 'COMPLETED'
    assert second['duplicate'] is True
    assert runner.call_count == 1
    assert execution.total_steps == 2
    assert execution.passed_steps == 1
    assert execution.skipped_steps == 1
    assert not hasattr(execution, 'variables')


@pytest.mark.django_db
def test_stale_api_case_executions_are_failed_without_replaying_requests():
    from datetime import timedelta

    from django.utils import timezone

    from apps.api_testing.case_tasks import reconcile_stale_api_case_executions

    user, project, _, _ = _api_workspace('api-case-recovery')
    old = timezone.now() - timedelta(minutes=5)
    stale_running = ApiCaseExecution.objects.create(
        project=project, test_case_name='stale running', executed_by=user,
        status='RUNNING', start_time=old, heartbeat_at=old,
    )
    stale_pending = ApiCaseExecution.objects.create(
        project=project, test_case_name='stale pending', executed_by=user, status='PENDING',
    )
    ApiCaseExecution.objects.filter(pk=stale_pending.pk).update(created_at=old)
    active = ApiCaseExecution.objects.create(
        project=project, test_case_name='active', executed_by=user,
        status='RUNNING', start_time=timezone.now(), heartbeat_at=timezone.now(),
    )

    recovered = reconcile_stale_api_case_executions(stale_after_seconds=60)

    assert recovered == 2
    for execution in (stale_running, stale_pending):
        execution.refresh_from_db()
        assert execution.status == 'FAILED'
        assert execution.end_time is not None
        assert '心跳超时' in execution.error_message
    active.refresh_from_db()
    assert active.status == 'RUNNING'


@pytest.mark.django_db
def test_worker_cannot_overwrite_an_execution_reaped_during_its_run():
    from datetime import timedelta

    from django.utils import timezone

    from apps.api_testing.case_tasks import execute_api_case_execution, reconcile_stale_api_case_executions

    user, project, _, _ = _api_workspace('api-case-recovery-fence')
    case = ApiTestCase.objects.create(project=project, name='case', created_by=user)
    execution = ApiCaseExecution.objects.create(
        project=project, test_case=case, test_case_name=case.name, executed_by=user,
    )

    def finish_after_reap(*_args, **_kwargs):
        ApiCaseExecution.objects.filter(pk=execution.pk).update(
            heartbeat_at=timezone.now() - timedelta(minutes=2),
        )
        assert reconcile_stale_api_case_executions(stale_after_seconds=60) == 1
        return {
            'success': True, 'total_count': 1, 'passed_count': 1,
            'failed_count': 0, 'skipped_count': 0, 'results': [],
        }

    with patch('apps.api_testing.case_runner.run_api_test_case', side_effect=finish_after_reap):
        result = execute_api_case_execution.run(execution.pk)

    execution.refresh_from_db()
    assert result['superseded'] is True
    assert execution.status == 'FAILED'
    assert '心跳超时' in execution.error_message


@pytest.mark.django_db
def test_suite_case_actions_add_and_reorder_full_mixed_membership():
    user, project, _, api_request = _api_workspace('api-case-suite-actions')
    suite = ApiTestSuite.objects.create(project=project, name='suite', created_by=user)
    request_membership = ApiTestSuiteRequest.objects.create(
        test_suite=suite, request=api_request, order=0,
    )
    case = ApiTestCase.objects.create(project=project, name='case', created_by=user)
    client = APIClient()
    client.force_authenticate(user=user)

    added = client.post(
        f'/api/api-testing/test-suites/{suite.pk}/add-cases/',
        {'case_ids': [case.pk]}, format='json',
    )
    assert added.status_code == 200, added.data
    case_membership = ApiTestSuiteCase.objects.get(test_suite=suite, test_case=case)
    reordered = client.post(
        f'/api/api-testing/test-suites/{suite.pk}/reorder/',
        {'items': [{'kind': 'case', 'id': case.pk}, {'kind': 'request', 'id': api_request.pk}]},
        format='json',
    )
    assert reordered.status_code == 200, reordered.data
    case_membership.refresh_from_db()
    request_membership.refresh_from_db()
    assert case_membership.order == 0
    assert request_membership.order == 1

    disabled = client.patch(
        f'/api/api-testing/test-suite-cases/{case_membership.pk}/',
        {'enabled': False}, format='json',
    )
    assert disabled.status_code == 200, disabled.data
    removed = client.delete(f'/api/api-testing/test-suite-cases/{case_membership.pk}/')
    assert removed.status_code == 204


@pytest.mark.django_db
def test_mixed_suite_uses_global_order_and_legacy_suite_status_code_value():
    user, project, _, api_request = _api_workspace('api-case-suite')
    suite = ApiTestSuite.objects.create(project=project, name='mixed', created_by=user)
    case = ApiTestCase.objects.create(project=project, name='case', created_by=user)
    ApiTestCaseStep.objects.create(test_case=case, name='step', request=api_request, order=0)
    ApiTestSuiteCase.objects.create(test_suite=suite, test_case=case, order=1)
    ApiTestSuiteRequest.objects.create(
        test_suite=suite, request=api_request, order=2,
        assertions=[{'type': 'status_code', 'value': 201}],
    )
    response = _response(status_code=200)

    def fake_request(*args, **kwargs):
        return {
            'success': True,
            'url': 'https://api.example.test/resource',
            'status_code': 200,
            'response_time': 1,
            'assertions_results': [],
            '_raw_response': response,
        }

    with patch('apps.api_testing.utils.execute_api_request', side_effect=fake_request), \
         patch('apps.api_testing.case_runner.execute_api_request', side_effect=fake_request):
        result = execute_test_suite(suite, None, user)

    assert result['success'] is False
    assert [item['kind'] for item in result['results']] == ['case', 'request']
    assert result['results'][1]['assertions_results'][0]['expected'] == 201
    execution = ApiTestExecution.objects.get(pk=result['execution_id'])
    assert execution.status == 'FAILED'
    assert execution.total_requests == 2
