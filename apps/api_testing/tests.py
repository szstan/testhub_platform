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
    TaskNotificationSetting,
    TestExecution as ApiTestExecution,
    TestSuite as ApiTestSuite,
)
from apps.api_testing.serializers import (
    RequestHistorySerializer,
    ScheduledTaskSerializer,
    TestSuiteSerializer as ApiTestSuiteSerializer,
)
from apps.api_testing.utils import execute_api_request, execute_assertions, execute_test_suite
from apps.api_testing.views import TestExecutionViewSet as ApiTestExecutionViewSet
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
    with patch('apps.api_testing.contract_runner.requests.request', return_value=response) as request_mock:
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
def test_openapi_diff_generation_and_coverage():
    user = User.objects.create_user(username='contract-owner', password='pass')
    project = Project.objects.create(name='Contract Project', owner=user)
    baseline = OpenApiContractSpec.objects.create(project=project, name='api', version='1', document=_document())
    current = OpenApiContractSpec.objects.create(project=project, name='api', version='2', document=_document(include_orders=True))

    diff = diff_contracts(current, baseline)
    assert diff['added'] == ['createOrder']
    assert diff['passed'] is True
    cases = generate_contract_cases(current)
    assert any(item['external_ref'] == 'openapi:createOrder:unauthorized' for item in cases)

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
    assert materialized.data['created'][0]['external_ref'] == 'openapi:listUsers:positive'


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
         patch('apps.api_testing.contract_runner.requests.request', return_value=response) as request_mock:
        results = execute_contract_cases(
            spec,
            base_url='https://api.example.test/v1',
            headers={'Authorization': 'test-credential'},
            kinds=['positive'],
        )

    assert results[0]['status'] == 'passed'
    assert request_mock.call_args.kwargs['url'] == 'https://api.example.test/users/test'
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
         patch('apps.api_testing.contract_runner.requests.request') as blocked_request:
        with pytest.raises(ValueError, match='authority'):
            execute_contract_cases(
                malicious_spec,
                base_url='https://api.example.test',
                headers={'Authorization': 'must-not-forward'},
            )
    blocked_request.assert_not_called()


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
