import json
from unittest.mock import Mock, patch

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from apps.api_testing.contract_runner import _case_url, execute_contract_cases
from apps.api_testing.contracts import contract_operations, coverage_for_spec, diff_contracts, generate_contract_cases
from apps.api_testing.models import (
    ApiCollection,
    ApiProject,
    ApiRequest,
    Environment,
    OpenApiContractSpec,
    RequestHistory,
    ScheduledTask,
    TaskExecutionLog,
    TaskNotificationSetting,
    TestExecution as ApiTestExecution,
    TestSuiteRequest as ApiTestSuiteRequest,
    TestSuite as ApiTestSuite,
)
from apps.api_testing.serializers import (
    ApiCollectionSerializer as ApiTestCollectionSerializer,
    ApiRequestSerializer as ApiTestRequestSerializer,
    RequestHistorySerializer,
    ScheduledTaskSerializer,
    TestSuiteSerializer as ApiTestSuiteSerializer,
)
from apps.api_testing.utils import execute_api_request, execute_assertions, execute_test_suite
from apps.api_testing.views import (
    TestExecutionViewSet as ApiTestExecutionViewSet,
    TestSuiteViewSet as ApiTestSuiteViewSet,
)
from apps.core.models import UnifiedNotificationConfig
from apps.executions.models import ExecutionAttempt, ExecutionJob, ExecutionStepResult
from apps.executions.state_machine import AttemptStatus, StepStatus
from apps.projects.models import Project
from apps.users.models import User


def _document(include_orders=False):
    paths = {
        '/users': {
            'get': {
                'operationId': 'listUsers',
                'responses': {'200': {'description': 'ok'}},
            },
        },
    }
    if include_orders:
        paths['/orders'] = {
            'post': {
                'operationId': 'createOrder',
                'security': [{'bearerAuth': []}],
                'responses': {'201': {'description': 'created'}},
            },
        }
    return {'openapi': '3.0.0', 'paths': paths}



def test_contract_operations_reject_duplicate_identities():
    duplicate_operation_ids = {'paths': {
        '/users': {'get': {'operationId': 'sharedOperation'}},
        '/orders': {'post': {'operationId': 'sharedOperation'}},
    }}
    fallback_collision = {'paths': {
        '/users': {'get': {}},
        '/orders': {'post': {'operationId': 'GET /users'}},
    }}
    for document in (duplicate_operation_ids, fallback_collision):
        with pytest.raises(ValueError, match='identity must be unique'):
            contract_operations(document)


def test_contract_runner_negative_cases_require_invalid_input_and_rejection():
    spec = OpenApiContractSpec(pk=9004, version='1', document={'paths': {}})
    invalid_body_case = {
        'contract_key': 'create-item-negative',
        'operation_key': 'createItem',
        'case_kind': 'negative',
        'method': 'POST',
        'path': '/items',
        'parameters': [],
        'request_body': {
            'content': {'application/json': {
                'schema': {
                    'type': 'object',
                    'properties': {'name': {'type': 'string'}},
                    'required': ['name'],
                },
            }},
        },
        'responses': {'201': {'description': 'created'}},
    }
    with patch('apps.api_testing.contract_runner.validate_outbound_url', side_effect=lambda url, **kwargs: url), \
         patch('apps.api_testing.contract_runner.guarded_request', return_value=Mock(status_code=201)) as request_mock:
        results = execute_contract_cases(
            spec,
            base_url='https://api.example.test',
            kinds=['negative'],
            case_definitions=[invalid_body_case],
        )
    assert request_mock.call_args.kwargs['json'] == {}
    assert results[0]['status'] == 'failed'

    unconstructable_case = {**invalid_body_case, 'contract_key': 'get-item-negative', 'method': 'GET', 'request_body': {}}
    with patch('apps.api_testing.contract_runner.validate_outbound_url', side_effect=lambda url, **kwargs: url), \
         patch('apps.api_testing.contract_runner.guarded_request') as request_mock:
        skipped = execute_contract_cases(
            spec,
            base_url='https://api.example.test',
            kinds=['negative'],
            case_definitions=[unconstructable_case],
        )
    assert skipped[0]['status'] == 'skipped'
    request_mock.assert_not_called()


@pytest.mark.django_db
def test_openapi_contract_execute_runs_bounded_cases_and_writes_steps():
    user = User.objects.create_user(username='contract-runner-owner', password='pass')
    project = Project.objects.create(name='Contract Runner Project', owner=user)
    spec = OpenApiContractSpec.objects.create(
        project=project,
        name='runner-api',
        version='1',
        document={**_document(), 'servers': [{'url': 'https://api.example.test'}]},
        document_hash='hash',
        created_by=user,
    )
    job = ExecutionJob.objects.create(project=project, source_type='api', source_id='api:suite:1', requested_by=user)
    response = Mock(status_code=200)
    client = APIClient()
    client.force_authenticate(user=user)

    def verify_attempt_started_before_request(**_kwargs):
        job.refresh_from_db()
        assert job.status == 'running'
        assert job.attempts.filter(attempt_number=1, status='running').exists()
        return response

    with patch(
        'apps.api_testing.contract_runner.guarded_request',
        side_effect=verify_attempt_started_before_request,
    ) as request_mock:
        result = client.post(
            f'/api/api-testing/openapi-contracts/{spec.pk}/execute/',
            {'job_id': job.pk, 'kinds': ['positive'], 'limit': 1},
            format='json',
        )
    assert result.status_code == 201, result.data
    assert result.data['status'] == 'passed'
    assert result.data['complete'] is True
    assert result.data['missing'] == 0
    assert result.data['results'][0]['case_kind'] == 'positive'
    assert request_mock.called
    assert ExecutionStepResult.objects.filter(attempt__job=job, step_key='contract:positive').exists()

    owner = User.objects.create_user(username='notification-owner', password='pass')
    foreign = User.objects.create_user(username='notification-foreign', password='pass')
    project = ApiProject.objects.create(
        name='Notification project', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    suite = ApiTestSuite.objects.create(project=project, name='suite', created_by=owner)
    task = ScheduledTask.objects.create(
        name='task', task_type='TEST_SUITE', trigger_type='INTERVAL', interval_seconds=60,
        test_suite=suite, created_by=owner,
    )
    setting = TaskNotificationSetting.objects.create(task=task)
    foreign_config = UnifiedNotificationConfig.objects.create(
        name='foreign', config_type='webhook_feishu', created_by=foreign,
        webhook_bots={'feishu': {'name': 'foreign-bot', 'webhook_url': 'https://secret.invalid/hook'}},
    )
    own_config = UnifiedNotificationConfig.objects.create(
        name='own', config_type='webhook_feishu', created_by=owner,
        webhook_bots={'feishu': {'name': 'own-bot', 'webhook_url': 'https://secret.invalid/hook', 'secret': 'signing-secret'}},
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    url = f'/api/api-testing/task-notification-settings/{setting.id}/update-settings/'
    denied = client.post(url, {'notification_config': foreign_config.id}, format='json')
    assert denied.status_code == 400
    setting.refresh_from_db()
    assert setting.notification_config_id is None

    allowed = client.post(
        url,
        {
            'notification_config': own_config.id,
            'custom_webhook_bots': {
                'feishu': {
                    'name': 'custom-bot',
                    'webhook_url': 'https://secret.invalid/custom',
                    'secret': 'custom-secret',
                },
            },
        },
        format='json',
    )
    assert allowed.status_code == 200, allowed.data
    assert 'custom_webhook_bots' not in allowed.data
    displayed = allowed.data['webhook_bots_display']
    assert displayed and displayed[0]['configured'] is True
    assert all('webhook_url' not in bot and 'secret' not in bot for bot in displayed)


@pytest.mark.django_db
def test_openapi_contract_execute_rejects_jobs_with_existing_attempts_before_requests():
    user = User.objects.create_user(username='contract-existing-attempt-owner', password='pass')
    project = Project.objects.create(name='Contract Existing Attempt Project', owner=user)
    spec = OpenApiContractSpec.objects.create(
        project=project,
        name='runner-api',
        version='1',
        document={**_document(), 'servers': [{'url': 'https://api.example.test'}]},
        document_hash='hash',
        created_by=user,
    )
    job = ExecutionJob.objects.create(project=project, source_type='api', requested_by=user)
    ExecutionAttempt.objects.create(job=job, attempt_number=1)
    client = APIClient()
    client.force_authenticate(user=user)

    with patch('apps.api_testing.contract_runner.guarded_request') as request_mock:
        response = client.post(
            f'/api/api-testing/openapi-contracts/{spec.pk}/execute/',
            {'job_id': job.pk, 'kinds': ['positive']},
            format='json',
        )

    assert response.status_code == 409
    request_mock.assert_not_called()


@pytest.mark.django_db
def test_openapi_diff_generation_and_coverage():
    user = User.objects.create_user(username='contract-owner', password='pass')
    project = Project.objects.create(name='Contract Project', owner=user)
    baseline = OpenApiContractSpec.objects.create(project=project, name='api', version='1', document=_document())
    current = OpenApiContractSpec.objects.create(project=project, name='api', version='2', document=_document(include_orders=True))

    diff = diff_contracts(current, baseline)
    assert diff['added'] == ['createOrder']
    assert diff['passed'] is True
    cases = generate_contract_cases(current)
    assert any(item['legacy_external_ref'] == 'openapi:createOrder:unauthorized' for item in cases)

    job = ExecutionJob.objects.create(project=project)
    attempt = ExecutionAttempt.objects.create(job=job, status=AttemptStatus.PASSED, attempt_number=1, is_final=True)
    job.final_attempt = attempt
    job.save(update_fields=['final_attempt'])
    ExecutionStepResult.objects.create(
        attempt=attempt,
        case_key='listUsers',
        step_key='test',
        status=StepStatus.PASSED,
    )
    result = coverage_for_spec(current)
    assert result['total'] == 2
    assert result['covered'] == 1
    assert result['uncovered'] == ['createOrder']


@pytest.mark.django_db
def test_openapi_contract_api_is_project_scoped():
    user = User.objects.create_user(username='contract-api-owner', password='pass')
    project = Project.objects.create(name='Contract API Project', owner=user)
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post('/api/api-testing/openapi-contracts/', {
        'project': project.id,
        'name': 'public-api',
        'version': '1',
        'document': _document(),
    }, format='json')
    assert response.status_code == 201, response.data
    spec_id = response.data['id']
    coverage = client.get(f'/api/api-testing/openapi-contracts/{spec_id}/coverage/')
    assert coverage.status_code == 200
    assert coverage.data['total'] == 1
    materialized = client.post(f'/api/api-testing/openapi-contracts/{spec_id}/materialize-cases/', {'kinds': ['positive']}, format='json')
    assert materialized.status_code == 200
    assert materialized.data['total'] == 1
    assert materialized.data['created'][0]['external_ref'].startswith(f'openapi-spec:{spec_id}:')

    other_spec_response = client.post('/api/api-testing/openapi-contracts/', {
        'project': project.id,
        'name': 'same-shape-api',
        'version': '1',
        'document': _document(),
    }, format='json')
    assert other_spec_response.status_code == 201, other_spec_response.data
    other_spec_id = other_spec_response.data['id']
    other_materialized = client.post(
        f'/api/api-testing/openapi-contracts/{other_spec_id}/materialize-cases/',
        {'kinds': ['positive']},
        format='json',
    )
    assert other_materialized.status_code == 200, other_materialized.data
    assert other_materialized.data['created'][0]['external_ref'] != materialized.data['created'][0]['external_ref']

    updated_spec = client.patch(
        f'/api/api-testing/openapi-contracts/{spec_id}/',
        {'version': '2'},
        format='json',
    )
    assert updated_spec.status_code == 200, updated_spec.data
    rematerialized = client.post(
        f'/api/api-testing/openapi-contracts/{spec_id}/materialize-cases/',
        {'kinds': ['positive']},
        format='json',
    )
    assert rematerialized.status_code == 200, rematerialized.data
    assert rematerialized.data['created'] == []
    assert rematerialized.data['updated'][0]['external_ref'] == materialized.data['created'][0]['external_ref']


@pytest.mark.django_db
def test_openapi_contract_record_results_normalizes_case_kinds_and_coverage():
    user = User.objects.create_user(username='contract-run-owner', password='pass')
    project = Project.objects.create(name='Contract Run Project', owner=user)
    client = APIClient()
    client.force_authenticate(user=user)
    spec_response = client.post('/api/api-testing/openapi-contracts/', {
        'project': project.id,
        'name': 'secured-api',
        'version': '2',
        'document': _document(include_orders=True),
    }, format='json')
    assert spec_response.status_code == 201, spec_response.data
    spec_id = spec_response.data['id']
    job = ExecutionJob.objects.create(
        project=project,
        source_type='api',
        source_id='api:suite:99',
        requested_by=user,
    )

    results = [
        {'operation_key': 'createOrder', 'case_kind': 'positive', 'status': 'passed'},
        {'operation_key': 'createOrder', 'case_kind': 'negative', 'status': 'failed', 'error_message': 'schema rejected'},
        {'operation_key': 'createOrder', 'case_kind': 'security', 'status': 'passed'},
    ]
    response = client.post(
        f'/api/api-testing/openapi-contracts/{spec_id}/record-results/',
        {'job_id': job.id, 'results': results},
        format='json',
    )
    assert response.status_code == 201, response.data
    assert response.data['created_results'] == 3
    assert response.data['status'] == 'failed'
    assert response.data['missing'] == 2
    assert response.data['complete'] is False
    assert response.data['coverage']['by_kind']['positive']['covered'] == 1
    assert response.data['coverage']['by_kind']['negative']['covered'] == 0
    assert response.data['coverage']['by_kind']['security']['covered'] == 1
    assert ExecutionStepResult.objects.filter(attempt__job=job, step_key='contract:positive').exists()
    assert ExecutionStepResult.objects.filter(attempt__job=job, case_key__startswith='openapi:').count() == 3


@pytest.mark.django_db
def test_openapi_contract_record_results_rejects_foreign_job():
    owner = User.objects.create_user(username='contract-foreign-owner', password='pass')
    foreign = User.objects.create_user(username='contract-foreign-user', password='pass')
    project = Project.objects.create(name='Contract Owner Project', owner=owner)
    foreign_project = Project.objects.create(name='Contract Foreign Project', owner=foreign)
    spec = OpenApiContractSpec.objects.create(
        project=project,
        name='api',
        version='1',
        document=_document(),
        document_hash='hash',
        created_by=owner,
    )
    foreign_job = ExecutionJob.objects.create(
        project=foreign_project,
        source_type='api',
        source_id='api:suite:100',
        requested_by=foreign,
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.post(
        f'/api/api-testing/openapi-contracts/{spec.pk}/record-results/',
        {
            'job_id': foreign_job.id,
            'results': [{'operation_key': 'listUsers', 'case_kind': 'positive', 'status': 'passed'}],
        },
        format='json',
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_manual_api_scheduled_broker_failure_marks_created_log_failed():
    owner = User.objects.create_user(username='manual-scheduled-broker-failure', password='pass')
    task = ScheduledTask.objects.create(
        name='manual broker failure', task_type='API_REQUEST', trigger_type='INTERVAL',
        interval_seconds=60, created_by=owner,
    )
    client = APIClient()
    client.force_authenticate(user=owner)

    with patch('apps.api_testing.tasks.execute_scheduled_task.delay', side_effect=RuntimeError('broker offline')):
        response = client.post(f'/api/api-testing/scheduled-tasks/{task.pk}/run_now/', {}, format='json')

    assert response.status_code == 500
    log = TaskExecutionLog.objects.get(task=task)
    assert log.status == 'FAILED'
    assert log.end_time is not None
    assert 'broker offline' in log.error_message


@pytest.mark.django_db
def test_contract_runner_rejects_authority_override_and_keeps_final_url_on_origin():
    spec = OpenApiContractSpec(
        pk=9001,
        version='1',
        document={'paths': {
            '/users/{user_id}': {'get': {
                'operationId': 'getUser',
                'parameters': [{'in': 'path', 'name': 'user_id', 'required': True}],
                'responses': {'200': {'description': 'ok'}},
            }},
        }},
    )
    response = Mock(status_code=200)
    with patch('apps.api_testing.contract_runner.validate_outbound_url', side_effect=lambda url, **kwargs: url), \
         patch('apps.api_testing.contract_runner.guarded_request', return_value=response) as request_mock:
        results = execute_contract_cases(
            spec,
            base_url='https://api.example.test/v1',
            headers={'Authorization': 'test-credential'},
            kinds=['positive'],
        )

    assert results[0]['status'] == 'passed'
    assert request_mock.call_args.kwargs['url'] == 'https://api.example.test/v1/users/test'
    assert request_mock.call_args.kwargs['headers']['Authorization'] == 'test-credential'

    def rewrite_validated_target(url, **kwargs):
        if url.endswith('/users'):
            return 'https://attacker.example/users'
        return url

    with patch('apps.api_testing.contract_runner.validate_outbound_url', side_effect=rewrite_validated_target):
        with pytest.raises(ValueError, match='origin'):
            _case_url('https://api.example.test', {'path': '/users'}, allow_private=False)

    for path in ('//127.0.0.1/admin', 'http://127.0.0.1/admin', '/users/\x00admin'):
        with pytest.raises(ValueError):
            _case_url('https://api.example.test', {'path': path}, allow_private=False)

    malicious_spec = OpenApiContractSpec(
        pk=9002,
        version='1',
        document={'paths': {'//127.0.0.1/admin': {'get': {'responses': {'200': {}}}}}},
    )
    with pytest.raises(ValueError, match='authority'):
        contract_operations(malicious_spec.document)
    with patch('apps.api_testing.contract_runner.validate_outbound_url', side_effect=lambda url, **kwargs: url), \
         patch('apps.api_testing.contract_runner.guarded_request') as blocked_request:
        with pytest.raises(ValueError, match='authority'):
            execute_contract_cases(
                malicious_spec,
                base_url='https://api.example.test',
                headers={'Authorization': 'must-not-forward'},
            )
    blocked_request.assert_not_called()


@pytest.mark.django_db
def test_contract_runner_validates_case_count_timeout_and_total_budget(monkeypatch):
    from apps.api_testing import contract_runner

    spec = OpenApiContractSpec(
        pk=9003,
        version='1',
        document=_document(include_orders=True),
    )
    with (
        patch('apps.api_testing.contract_runner.validate_outbound_url', side_effect=lambda url, **kwargs: url),
        patch('apps.api_testing.contract_runner.guarded_request') as request_mock,
    ):
        for invalid_limit in (0, 11, 'not-a-number', 1.5, True):
            with pytest.raises(ValueError, match='limit'):
                execute_contract_cases(spec, base_url='https://api.example.test', limit=invalid_limit)
        for invalid_timeout in (0, 5.1, 'bad', float('nan'), True):
            with pytest.raises(ValueError, match='timeout'):
                execute_contract_cases(spec, base_url='https://api.example.test', timeout=invalid_timeout)
        request_mock.assert_not_called()

        monkeypatch.setattr(contract_runner, 'CONTRACT_RUN_BUDGET_SECONDS', 0)
        results = execute_contract_cases(
            spec,
            base_url='https://api.example.test/v1',
            kinds=['positive'],
            limit=2,
        )

    assert [row['status'] for row in results] == ['blocked', 'blocked']
    request_mock.assert_not_called()


@pytest.mark.django_db
def test_contract_record_results_keeps_member_access_and_blocks_partial_or_skipped_passes():
    owner = User.objects.create_user(username='contract-completeness-owner', password='pass')
    member = User.objects.create_user(username='contract-completeness-member', password='pass')
    project = Project.objects.create(name='Contract Completeness Project', owner=owner)
    project.members.add(member)
    spec = OpenApiContractSpec.objects.create(
        project=project,
        name='api',
        version='1',
        document=_document(include_orders=True),
        document_hash='hash',
        created_by=owner,
    )
    cases = generate_contract_cases(spec)
    client = APIClient()
    client.force_authenticate(user=member)

    skipped_job = ExecutionJob.objects.create(project=project, source_type='api', source_id='contract-skipped')
    skipped_response = client.post(
        f'/api/api-testing/openapi-contracts/{spec.pk}/record-results/',
        {
            'job_id': skipped_job.pk,
            'results': [
                {'contract_key': case['contract_key'], 'status': 'skipped'}
                for case in cases
            ],
        },
        format='json',
    )
    assert skipped_response.status_code == 201, skipped_response.data
    assert skipped_response.data['status'] == 'blocked'
    assert skipped_response.data['missing'] == 0
    assert skipped_response.data['skipped'] == len(cases)
    assert skipped_response.data['complete'] is False

    partial_job = ExecutionJob.objects.create(project=project, source_type='api', source_id='contract-partial')
    partial_response = client.post(
        f'/api/api-testing/openapi-contracts/{spec.pk}/record-results/',
        {
            'job_id': partial_job.pk,
            'results': [{
                'contract_key': cases[0]['contract_key'],
                'status': 'passed',
            }],
        },
        format='json',
    )
    assert partial_response.status_code == 201, partial_response.data
    assert partial_response.data['status'] == 'blocked'
    assert partial_response.data['missing'] == len(cases) - 1
    assert partial_response.data['skipped'] == 0
    assert partial_response.data['complete'] is False


@pytest.mark.django_db
def test_environment_owner_validation_and_runtime_allow_local_project_members():
    owner = User.objects.create_user(username='environment-project-owner', password='pass')
    member = User.objects.create_user(username='environment-project-member', password='pass')
    other = User.objects.create_user(username='environment-foreign-owner', password='pass')
    project = ApiProject.objects.create(
        name='Environment Project', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    project.members.add(member)
    collection = ApiCollection.objects.create(name='collection', project=project)
    api_request = ApiRequest.objects.create(
        collection=collection,
        name='request',
        method='GET',
        url='https://api.example.test/items',
        created_by=owner,
    )
    foreign_global = Environment.objects.create(
        name='foreign global', scope='GLOBAL', variables={'token': 'foreign-token'}, created_by=other,
    )
    shared_local = Environment.objects.create(
        name='shared local', scope='LOCAL', project=project,
        variables={'token': 'project-token'}, created_by=other,
    )
    request_context = Mock(user=member)

    foreign_suite = ApiTestSuiteSerializer(
        data={'name': 'suite', 'project': project.pk, 'environment': foreign_global.pk},
        context={'request': request_context},
    )
    assert not foreign_suite.is_valid()
    assert 'environment' in foreign_suite.errors
    shared_suite = ApiTestSuiteSerializer(
        data={'name': 'suite', 'project': project.pk, 'environment': shared_local.pk},
        context={'request': request_context},
    )
    assert shared_suite.is_valid(), shared_suite.errors

    task_data = {
        'name': 'scheduled request',
        'task_type': 'API_REQUEST',
        'trigger_type': 'INTERVAL',
        'interval_seconds': 60,
        'api_request': api_request.pk,
        'environment': foreign_global.pk,
    }
    foreign_task = ScheduledTaskSerializer(data=task_data, context={'request': request_context})
    assert not foreign_task.is_valid()
    assert 'environment' in foreign_task.errors
    task_data['environment'] = shared_local.pk
    shared_task = ScheduledTaskSerializer(data=task_data, context={'request': request_context})
    assert shared_task.is_valid(), shared_task.errors

    suite = ApiTestSuite.objects.create(
        project=project, name='runtime suite', environment=foreign_global, created_by=owner,
    )
    with patch('requests.request') as http_request:
        denied_request = execute_api_request(api_request, foreign_global, member)
        denied_suite = execute_test_suite(suite, foreign_global, member)
    assert denied_request['success'] is False
    assert denied_suite['success'] is False
    assert ApiTestExecution.objects.filter(test_suite=suite).count() == 0
    http_request.assert_not_called()

    response = Mock(
        status_code=200,
        headers={'content-type': 'application/json'},
        text='{}',
    )
    suite.environment = shared_local
    suite.save(update_fields=['environment'])
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), \
         patch('requests.request', return_value=response) as allowed_request:
        allowed = execute_api_request(api_request, shared_local, member)
    assert allowed['success'] is True
    allowed_request.assert_called_once()


@pytest.mark.django_db
def test_jsonpath_assertion_secrets_are_redacted_in_history_and_serialization():
    user = User.objects.create_user(username='assertion-redaction-user', password='pass')
    project = ApiProject.objects.create(
        name='Assertion Redaction Project', project_type='HTTP', status='IN_PROGRESS', owner=user,
    )
    collection = ApiCollection.objects.create(name='collection', project=project)
    secret = 'response-token-must-not-persist'
    api_request = ApiRequest.objects.create(
        collection=collection,
        name='token response',
        method='GET',
        url='https://api.example.test/token',
        assertions=[{'type': 'json_path', 'json_path': '$.access_token', 'expected': 'some-other-value'}],
        created_by=user,
    )
    response = Mock(
        status_code=200,
        headers={'content-type': 'application/json'},
        text=json.dumps({'access_token': secret}),
    )
    with patch('apps.api_testing.utils.validate_outbound_url', side_effect=lambda url, **kwargs: url), \
         patch('requests.request', return_value=response):
        result = execute_api_request(api_request, None, user)

    assert result['success'] is True
    history = RequestHistory.objects.get(pk=result['history_id'])
    persisted = json.dumps(history.assertions_results)
    serialized = json.dumps(RequestHistorySerializer(history).data)
    assert secret not in persisted
    assert secret not in serialized
    assert secret not in history.response_data['body']
    assert history.response_data['json']['access_token'] == '[REDACTED]'
    assert history.assertions_results[0]['actual'] == '[REDACTED]'
    assert history.assertions_results[0]['passed'] is False


@pytest.mark.django_db
def test_generated_fallback_and_summary_reports_escape_writable_html(tmp_path):
    user = User.objects.create_user(username='report-xss-user', password='pass')
    project = ApiProject.objects.create(
        name='<img src=x onerror=alert(2)>', project_type='HTTP', status='IN_PROGRESS', owner=user,
    )
    suite = ApiTestSuite.objects.create(
        project=project,
        name='</title><script>alert("report-xss")</script>',
        created_by=user,
    )
    execution = ApiTestExecution.objects.create(
        test_suite=suite,
        status='COMPLETED',
        executed_by=user,
        results=[],
    )
    client = APIClient()
    client.force_authenticate(user=user)
    with override_settings(MEDIA_ROOT=str(tmp_path)), \
         patch.object(ApiTestExecutionViewSet, '_check_java_environment', return_value=False):
        response = client.post(
            f'/api/api-testing/test-executions/{execution.pk}/generate-allure-report/',
            {},
            format='json',
        )

    assert response.status_code == 200, response.data
    report_dir = tmp_path / 'api-testing' / 'allure-reports' / f'execution_{execution.pk}'
    summary = (report_dir / 'summary.html').read_text(encoding='utf-8')
    fallback = (report_dir / 'index.html').read_text(encoding='utf-8')
    assert '<script>' not in summary and '<img src=x' not in summary
    assert '<script>' not in fallback
    assert '&lt;script&gt;' in summary and '&lt;img src=x' in summary
    assert '&lt;/title&gt;' in fallback


def test_assertion_jsonpath_secret_is_redacted_before_returning_result():
    response = Mock(
        status_code=200,
        headers={'content-type': 'application/json'},
        text='{"access_token":"assertion-only-secret"}',
    )
    results = execute_assertions(response, [{
        'type': 'json_path',
        'json_path': '$.access_token',
        'expected': 'different-value',
    }])
    assert results[0]['passed'] is False
    assert results[0]['actual'] == '[REDACTED]'
    assert 'assertion-only-secret' not in json.dumps(results)


@pytest.mark.django_db
def test_api_request_endpoint_persists_only_redacted_response_evidence():
    user = User.objects.create_user(username='api-execute-redaction-user', password='pass')
    project = ApiProject.objects.create(
        name='API Execute Redaction Project', project_type='HTTP', status='IN_PROGRESS', owner=user,
    )
    collection = ApiCollection.objects.create(name='collection', project=project)
    secret = 'endpoint-response-secret'
    api_request = ApiRequest.objects.create(
        collection=collection,
        name='token response',
        method='GET',
        url='https://api.example.test/token',
        assertions=[{'type': 'json_path', 'json_path': '$.access_token', 'expected': 'other'}],
        created_by=user,
    )
    response = Mock(
        status_code=200,
        headers={'content-type': 'application/json'},
        text=json.dumps({'access_token': secret}),
    )
    client = APIClient()
    client.force_authenticate(user=user)
    with patch('apps.api_testing.views.validate_outbound_url', side_effect=lambda url, **kwargs: url), \
         patch('apps.api_testing.views.requests.request', return_value=response):
        result = client.post(
            f'/api/api-testing/requests/{api_request.pk}/execute/',
            {},
            format='json',
        )

    assert result.status_code == 200, result.data
    assert secret not in json.dumps(result.data)
    history = RequestHistory.objects.get(pk=result.data['id'])
    assert secret not in history.response_data['body']
    assert history.assertions_results[0]['actual'] == '[REDACTED]'


@pytest.mark.django_db
def test_api_suite_cannot_move_with_foreign_requests_or_environment():
    owner = User.objects.create_user(username='suite-move-owner', password='pass')
    target_owner = User.objects.create_user(username='suite-target-owner', password='pass')
    target_member = User.objects.create_user(username='suite-target-member', password='pass')
    source_member = User.objects.create_user(username='suite-source-member', password='pass')
    source = ApiProject.objects.create(
        name='Suite Source', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    source.members.add(source_member)
    target = ApiProject.objects.create(
        name='Suite Target', project_type='HTTP', status='IN_PROGRESS', owner=target_owner,
    )
    target.members.add(owner, target_member)
    collection = ApiCollection.objects.create(name='source collection', project=source)
    api_request = ApiRequest.objects.create(
        collection=collection,
        name='private source request',
        method='GET',
        url='https://source.example.test/private',
        headers={'Authorization': 'source-secret'},
        created_by=owner,
    )
    suite = ApiTestSuite.objects.create(project=source, name='source suite', created_by=owner)
    ApiTestSuiteRequest.objects.create(test_suite=suite, request=api_request)
    source_environment = Environment.objects.create(
        name='source local', scope='LOCAL', project=source, created_by=owner,
    )

    client = APIClient()
    suite_url = f'/api/api-testing/test-suites/{suite.pk}/'
    empty_suite = ApiTestSuite.objects.create(project=source, name='empty suite', created_by=owner)
    client.force_authenticate(user=source_member)
    denied_empty_move = client.patch(
        f'/api/api-testing/test-suites/{empty_suite.pk}/',
        {'project': target.pk},
        format='json',
    )
    assert denied_empty_move.status_code == 400, denied_empty_move.data
    assert 'project' in denied_empty_move.data
    empty_suite.refresh_from_db()
    assert empty_suite.project_id == source.pk

    client.force_authenticate(user=owner)
    patched = client.patch(suite_url, {'project': target.pk}, format='json')
    assert patched.status_code == 400, patched.data
    assert 'project' in patched.data
    replaced = client.put(
        suite_url,
        {'name': suite.name, 'project': target.pk},
        format='json',
    )
    assert replaced.status_code == 400, replaced.data
    assert 'project' in replaced.data
    suite.refresh_from_db()
    assert suite.project_id == source.pk

    environment_suite = ApiTestSuite.objects.create(
        project=source,
        name='environment suite',
        environment=source_environment,
        created_by=owner,
    )
    environment_move = client.patch(
        f'/api/api-testing/test-suites/{environment_suite.pk}/',
        {'project': target.pk},
        format='json',
    )
    assert environment_move.status_code == 400, environment_move.data
    assert 'environment' in environment_move.data
    environment_suite.refresh_from_db()
    assert environment_suite.project_id == source.pk

    client.force_authenticate(user=target_member)
    hidden_suite = client.get(suite_url)
    assert hidden_suite.status_code == 404
    with patch('apps.api_testing.views.guarded_request') as guarded_request:
        denied_execution = client.post(f'{suite_url}execute/', {}, format='json')
    assert denied_execution.status_code == 404
    guarded_request.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_api_suite_move_rechecks_requests_added_after_serializer_validation():
    owner = User.objects.create_user(username='suite-interleave-owner', password='pass')
    target_owner = User.objects.create_user(username='suite-interleave-target', password='pass')
    source = ApiProject.objects.create(
        name='Suite Interleave Source', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    target = ApiProject.objects.create(
        name='Suite Interleave Target', project_type='HTTP', status='IN_PROGRESS', owner=target_owner,
    )
    target.members.add(owner)
    collection = ApiCollection.objects.create(name='source collection', project=source)
    old_request = ApiRequest.objects.create(
        collection=collection,
        name='old-project request',
        method='GET',
        url='https://source.example.test/interleave',
        created_by=owner,
    )
    suite = ApiTestSuite.objects.create(project=source, name='suite', created_by=owner)
    client = APIClient()
    client.force_authenticate(user=owner)
    original_is_valid = ApiTestSuiteSerializer.is_valid
    interleaved = False

    def add_request_after_validation(serializer, *args, **kwargs):
        nonlocal interleaved
        valid = original_is_valid(serializer, *args, **kwargs)
        if (
            valid
            and not interleaved
            and serializer.instance
            and serializer.instance.pk == suite.pk
            and serializer.initial_data.get('project') == target.pk
        ):
            interleaved = True
            add_response = client.post(
                f'/api/api-testing/test-suites/{suite.pk}/add-requests/',
                {'request_ids': [old_request.pk]},
                format='json',
            )
            assert add_response.status_code == 200, add_response.data
        return valid

    # The add endpoint commits between serializer validation and perform_update.
    # SQLite ignores select_for_update(), so this checks the re-read invariant, not lock blocking.
    with patch.object(ApiTestSuiteSerializer, 'is_valid', new=add_request_after_validation):
        response = client.patch(
            f'/api/api-testing/test-suites/{suite.pk}/',
            {'project': target.pk},
            format='json',
        )

    assert interleaved
    assert response.status_code == 400, response.data
    assert 'project' in response.data
    suite.refresh_from_db()
    assert suite.project_id == source.pk
    assert ApiTestSuiteRequest.objects.filter(test_suite=suite, request=old_request).exists()


@pytest.mark.django_db(transaction=True)
def test_api_suite_add_rechecks_project_after_stale_suite_lookup():
    owner = User.objects.create_user(username='suite-stale-add-owner', password='pass')
    target_owner = User.objects.create_user(username='suite-stale-add-target', password='pass')
    source = ApiProject.objects.create(
        name='Suite Stale Add Source', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    target = ApiProject.objects.create(
        name='Suite Stale Add Target', project_type='HTTP', status='IN_PROGRESS', owner=target_owner,
    )
    target.members.add(owner)
    source_collection = ApiCollection.objects.create(name='source collection', project=source)
    target_collection = ApiCollection.objects.create(name='target collection', project=target)
    old_request = ApiRequest.objects.create(
        collection=source_collection, name='old request', method='GET',
        url='https://source.example.test/items', created_by=owner,
    )
    target_request = ApiRequest.objects.create(
        collection=target_collection, name='target request', method='GET',
        url='https://target.example.test/items', created_by=target_owner,
    )
    suite = ApiTestSuite.objects.create(project=source, name='suite', created_by=owner)
    client = APIClient()
    client.force_authenticate(user=owner)
    original_get_object = ApiTestSuiteViewSet.get_object
    moved = False

    def move_after_lookup(view):
        nonlocal moved
        suite_instance = original_get_object(view)
        if view.action == 'add_requests' and not moved:
            moved = True
            ApiTestSuite.objects.filter(pk=suite_instance.pk).update(project=target)
        return suite_instance

    # Simulate a concurrent move after get_object() but before the locking read.
    # SQLite cannot verify select_for_update() blocking; MySQL integration is needed for that.
    with patch.object(ApiTestSuiteViewSet, 'get_object', new=move_after_lookup):
        stale_add = client.post(
            f'/api/api-testing/test-suites/{suite.pk}/add-requests/',
            {'request_ids': [old_request.pk]},
            format='json',
        )
        current_add = client.post(
            f'/api/api-testing/test-suites/{suite.pk}/add-requests/',
            {'request_ids': [target_request.pk]},
            format='json',
        )

    assert moved
    assert stale_add.status_code == 400, stale_add.data
    assert not ApiTestSuiteRequest.objects.filter(test_suite=suite, request=old_request).exists()
    assert current_add.status_code == 200, current_add.data
    suite.refresh_from_db()
    assert suite.project_id == target.pk
    assert ApiTestSuiteRequest.objects.filter(test_suite=suite, request=target_request).exists()


@pytest.mark.django_db(transaction=True)
def test_api_suite_update_rejects_stale_source_instance():
    owner = User.objects.create_user(username='suite-stale-update-owner', password='pass')
    target_owner = User.objects.create_user(username='suite-stale-update-target', password='pass')
    source = ApiProject.objects.create(
        name='Suite Stale Update Source', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    target = ApiProject.objects.create(
        name='Suite Stale Update Target', project_type='HTTP', status='IN_PROGRESS', owner=target_owner,
    )
    target.members.add(owner)
    suite = ApiTestSuite.objects.create(project=source, name='original name', created_by=owner)
    client = APIClient()
    client.force_authenticate(user=owner)
    original_is_valid = ApiTestSuiteSerializer.is_valid
    moved = False

    def move_after_validation(serializer, *args, **kwargs):
        nonlocal moved
        valid = original_is_valid(serializer, *args, **kwargs)
        if valid and not moved and serializer.instance and serializer.instance.pk == suite.pk:
            moved = True
            ApiTestSuite.objects.filter(pk=suite.pk).update(project=target)
        return valid

    # A stale model instance must not overwrite a project changed after validation.
    with patch.object(ApiTestSuiteSerializer, 'is_valid', new=move_after_validation):
        response = client.patch(
            f'/api/api-testing/test-suites/{suite.pk}/',
            {'name': 'stale update'},
            format='json',
        )

    assert moved
    assert response.status_code == 400, response.data
    assert 'project' in response.data
    suite.refresh_from_db()
    assert suite.project_id == target.pk
    assert suite.name == 'original name'


@pytest.mark.parametrize('admin_flag', ['is_staff', 'is_superuser'])
@pytest.mark.django_db
def test_api_suite_creation_preserves_admin_project_access(admin_flag):
    admin = User.objects.create_user(username=f'suite-{admin_flag}', password='pass')
    setattr(admin, admin_flag, True)
    admin.save(update_fields=[admin_flag])
    owner = User.objects.create_user(username=f'suite-{admin_flag}-owner', password='pass')
    project = ApiProject.objects.create(
        name=f'Suite Admin Project {admin_flag}', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    client = APIClient()
    client.force_authenticate(user=admin)

    response = client.post(
        '/api/api-testing/test-suites/',
        {'name': 'admin-created suite', 'project': project.pk},
        format='json',
    )

    assert response.status_code == 201, response.data
    assert ApiTestSuite.objects.filter(pk=response.data['id'], project=project, created_by=admin).exists()


@pytest.mark.django_db(transaction=True)
def test_api_suite_move_rejects_execution_created_after_validation():
    owner = User.objects.create_user(username='suite-history-owner', password='pass')
    target_owner = User.objects.create_user(username='suite-history-target', password='pass')
    target_member = User.objects.create_user(username='suite-history-target-member', password='pass')
    source = ApiProject.objects.create(
        name='Suite History Source', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    target = ApiProject.objects.create(
        name='Suite History Target', project_type='HTTP', status='IN_PROGRESS', owner=target_owner,
    )
    target.members.add(owner, target_member)
    suite = ApiTestSuite.objects.create(project=source, name='suite with history', created_by=owner)
    client = APIClient()
    client.force_authenticate(user=owner)
    original_is_valid = ApiTestSuiteSerializer.is_valid
    execution_id = None

    def execute_after_validation(serializer, *args, **kwargs):
        nonlocal execution_id
        valid = original_is_valid(serializer, *args, **kwargs)
        if (
            valid
            and execution_id is None
            and serializer.instance
            and serializer.instance.pk == suite.pk
            and serializer.initial_data.get('project') == target.pk
        ):
            execution_response = client.post(
                f'/api/api-testing/test-suites/{suite.pk}/execute/', {}, format='json',
            )
            assert execution_response.status_code == 200, execution_response.data
            execution_id = execution_response.data['id']
        return valid

    with patch.object(ApiTestSuiteSerializer, 'is_valid', new=execute_after_validation):
        move_response = client.patch(
            f'/api/api-testing/test-suites/{suite.pk}/',
            {'project': target.pk},
            format='json',
        )

    assert execution_id is not None
    assert move_response.status_code == 400, move_response.data
    assert 'project' in move_response.data
    suite.refresh_from_db()
    assert suite.project_id == source.pk
    assert ApiTestExecution.objects.filter(pk=execution_id, test_suite=suite).exists()

    client.force_authenticate(user=owner)
    assert client.get(f'/api/api-testing/test-executions/{execution_id}/').status_code == 200
    client.force_authenticate(user=target_member)
    assert client.get(f'/api/api-testing/test-executions/{execution_id}/').status_code == 404

    empty_suite = ApiTestSuite.objects.create(project=source, name='empty suite', created_by=owner)
    client.force_authenticate(user=owner)
    empty_move = client.patch(
        f'/api/api-testing/test-suites/{empty_suite.pk}/',
        {'project': target.pk},
        format='json',
    )
    assert empty_move.status_code == 200, empty_move.data
    empty_suite.refresh_from_db()
    assert empty_suite.project_id == target.pk


@pytest.mark.django_db(transaction=True)
def test_api_request_move_rechecks_suite_links_after_validation():
    owner = User.objects.create_user(username='suite-linked-request-owner', password='pass')
    target_owner = User.objects.create_user(username='suite-linked-request-target', password='pass')
    source = ApiProject.objects.create(
        name='Suite Linked Request Source', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    target = ApiProject.objects.create(
        name='Suite Linked Request Target', project_type='HTTP', status='IN_PROGRESS', owner=target_owner,
    )
    target.members.add(owner)
    source_collection = ApiCollection.objects.create(name='source', project=source)
    target_collection = ApiCollection.objects.create(name='target', project=target)
    api_request = ApiRequest.objects.create(
        collection=source_collection, name='request', method='GET',
        url='https://source.example.test/items', created_by=owner,
    )
    suite = ApiTestSuite.objects.create(project=source, name='suite', created_by=owner)
    client = APIClient()
    client.force_authenticate(user=owner)
    original_is_valid = ApiTestRequestSerializer.is_valid
    interleaved = False

    def add_after_request_validation(serializer, *args, **kwargs):
        nonlocal interleaved
        valid = original_is_valid(serializer, *args, **kwargs)
        if valid and not interleaved and serializer.instance and serializer.instance.pk == api_request.pk:
            interleaved = True
            add_response = client.post(
                f'/api/api-testing/test-suites/{suite.pk}/add-requests/',
                {'request_ids': [api_request.pk]},
                format='json',
            )
            assert add_response.status_code == 200, add_response.data
        return valid

    with patch.object(ApiTestRequestSerializer, 'is_valid', new=add_after_request_validation):
        response = client.patch(
            f'/api/api-testing/requests/{api_request.pk}/',
            {'collection': target_collection.pk},
            format='json',
        )

    assert interleaved
    assert response.status_code == 400, response.data
    assert 'collection' in response.data
    api_request.refresh_from_db()
    assert api_request.collection_id == source_collection.pk
    assert ApiTestSuiteRequest.objects.filter(test_suite=suite, request=api_request).exists()


@pytest.mark.django_db(transaction=True)
def test_api_collection_move_rechecks_suite_links_after_validation():
    owner = User.objects.create_user(username='suite-linked-collection-owner', password='pass')
    target_owner = User.objects.create_user(username='suite-linked-collection-target', password='pass')
    source = ApiProject.objects.create(
        name='Suite Linked Collection Source', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    target = ApiProject.objects.create(
        name='Suite Linked Collection Target', project_type='HTTP', status='IN_PROGRESS', owner=target_owner,
    )
    target.members.add(owner)
    collection = ApiCollection.objects.create(name='source', project=source)
    api_request = ApiRequest.objects.create(
        collection=collection, name='request', method='GET',
        url='https://source.example.test/items', created_by=owner,
    )
    suite = ApiTestSuite.objects.create(project=source, name='suite', created_by=owner)
    client = APIClient()
    client.force_authenticate(user=owner)
    original_is_valid = ApiTestCollectionSerializer.is_valid
    interleaved = False

    def add_after_collection_validation(serializer, *args, **kwargs):
        nonlocal interleaved
        valid = original_is_valid(serializer, *args, **kwargs)
        if valid and not interleaved and serializer.instance and serializer.instance.pk == collection.pk:
            interleaved = True
            add_response = client.post(
                f'/api/api-testing/test-suites/{suite.pk}/add-requests/',
                {'request_ids': [api_request.pk]},
                format='json',
            )
            assert add_response.status_code == 200, add_response.data
        return valid

    with patch.object(ApiTestCollectionSerializer, 'is_valid', new=add_after_collection_validation):
        response = client.patch(
            f'/api/api-testing/collections/{collection.pk}/',
            {'project': target.pk},
            format='json',
        )

    assert interleaved
    assert response.status_code == 400, response.data
    assert 'project' in response.data
    collection.refresh_from_db()
    assert collection.project_id == source.pk
    assert ApiTestSuiteRequest.objects.filter(test_suite=suite, request=api_request).exists()


def test_execute_api_request_runs_cancel_check_before_network():
    cancel_check = Mock(side_effect=RuntimeError('lease lost'))
    with patch('requests.request') as http_request:
        with pytest.raises(RuntimeError, match='lease lost'):
            execute_api_request(Mock(), None, Mock(), cancel_check=cancel_check)

    cancel_check.assert_called_once_with()
    http_request.assert_not_called()


@pytest.mark.django_db
def test_execute_test_suite_runs_cancel_check_before_creating_execution():
    user = User.objects.create_user(username='cancel-check-user', password='pass')
    project = ApiProject.objects.create(
        name='Cancel Check Project', project_type='HTTP', status='IN_PROGRESS', owner=user,
    )
    suite = ApiTestSuite.objects.create(project=project, name='suite', created_by=user)
    cancel_check = Mock(side_effect=RuntimeError('lease lost'))

    with pytest.raises(RuntimeError, match='lease lost'):
        execute_test_suite(suite, None, user, cancel_check=cancel_check)

    cancel_check.assert_called_once_with()
    assert ApiTestExecution.objects.filter(test_suite=suite).count() == 0
