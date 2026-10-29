from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIClient

from apps.api_testing.contracts import coverage_for_spec, diff_contracts, generate_contract_cases
from apps.api_testing.models import (
    ApiProject,
    OpenApiContractSpec,
    ScheduledTask,
    TaskNotificationSetting,
    TestSuite as ApiTestSuite,
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
