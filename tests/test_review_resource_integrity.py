"""Regressions for review-confirmed safety and resource boundaries."""
import asyncio
import json
import socket
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api_testing.contract_runner import execute_contract_cases
from apps.api_testing.contracts import generate_contract_cases
from apps.api_testing.models import ApiCollection, ApiProject, ApiRequest, OpenApiContractSpec, RequestHistory
from apps.api_testing.utils import execute_api_request, execute_assertions
from apps.app_automation import device_pool
from apps.app_automation.constants import DeviceStatus
from apps.app_automation.models import AppDevice
from apps.executions.models import ExecutionAttempt, ExecutionJob, ExecutionJobVariant, ExecutionStepResult
from apps.executions.worker_leases import ExecutionClaimLost, check_claim
from apps.perf_testing.engines import builtin
from apps.perf_testing.engines.base import EngineError
from apps.projects.models import Project
from apps.quality_intelligence.models import QualityFailureCluster
from apps.quality_intelligence.services.clusters import sync_execution_job_clusters
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def review_context():
    user = User.objects.create_user(username='review-resource-owner')
    project = Project.objects.create(name='Review resource boundaries', owner=user)
    client = APIClient()
    client.force_authenticate(user)
    return user, project, client


def _claimed_job(project, user):
    job = ExecutionJob.objects.create(project=project, requested_by=user, source_type='api', status='running')
    attempt = ExecutionAttempt.objects.create(
        job=job, status='running', worker_claim_id='claim',
        worker_claim_expires_at=timezone.now() + timedelta(minutes=5),
    )
    return job, attempt


def test_action_boundary_claim_check_is_one_fresh_small_read(review_context):
    user, project, _client = review_context
    job, attempt = _claimed_job(project, user)
    with CaptureQueriesContext(connection) as queries:
        current = check_claim(job.pk, attempt.pk, 'claim')
    assert current.pk == attempt.pk
    assert len(queries) == 1
    assert 'SELECT' in queries[0]['sql']
    assert 'final_result' not in queries[0]['sql']
    assert 'worker_heartbeat_at' not in queries[0]['sql']

    ExecutionAttempt.objects.filter(pk=attempt.pk).update(worker_claim_expires_at=timezone.now() - timedelta(seconds=1))
    with CaptureQueriesContext(connection) as queries, pytest.raises(ExecutionClaimLost):
        check_claim(job.pk, attempt.pk, 'claim')
    assert len(queries) == 1


def test_action_boundary_claim_check_keeps_variant_currentness(review_context):
    user, project, _client = review_context
    job = ExecutionJob.objects.create(project=project, requested_by=user, source_type='api', status='running')
    variants = [ExecutionJobVariant.objects.create(job=job, variant_key=f'v-{i}', ordinal=i, payload={}) for i in range(2)]
    first = ExecutionAttempt.objects.create(job=job, variant=variants[0], attempt_number=1, status='running', worker_claim_id='first', worker_claim_expires_at=timezone.now() + timedelta(minutes=5))
    ExecutionAttempt.objects.create(job=job, variant=variants[1], attempt_number=2, status='running', worker_claim_id='other', worker_claim_expires_at=timezone.now() + timedelta(minutes=5))
    assert check_claim(job.pk, first.pk, 'first').pk == first.pk
    ExecutionAttempt.objects.create(job=job, variant=variants[0], attempt_number=3, status='queued')
    with pytest.raises(ExecutionClaimLost):
        check_claim(job.pk, first.pk, 'first')


def _device(user, name, **values):
    return AppDevice.objects.create(
        device_id=name, locked_by=user, locked_at=timezone.now() - timedelta(hours=1),
        status=DeviceStatus.LOCKED, max_allocation_time=120, **values,
    )


def test_reaper_locks_only_expired_candidates_and_keeps_renewed_legacy_owner(review_context):
    user, _project, _client = review_context
    live = _device(user, 'live-token', lease_token_hash='token-hash', lease_expires_at=timezone.now() + timedelta(hours=1))
    legacy = _device(user, 'live-legacy', last_heartbeat_at=timezone.now())
    expired = _device(user, 'expired-token', lease_token_hash='expired-hash', lease_expires_at=timezone.now() - timedelta(seconds=1))
    manager = AppDevice.objects
    original = manager.select_for_update
    locked_ids = []

    def locking_query():
        query = original()
        filtered = query.filter
        def capture(**kwargs):
            locked_ids.extend(kwargs['pk__in'])
            return filtered(**kwargs)
        query.filter = capture
        return query

    with patch.object(manager, 'select_for_update', side_effect=locking_query):
        assert device_pool.reap_expired_device_leases() == 1
    assert locked_ids == [expired.pk]
    for device in [live, legacy]:
        device.refresh_from_db()
        assert device.locked_by_id == user.pk
    expired.refresh_from_db()
    assert expired.locked_by_id is None


def test_reaper_rechecks_a_renewal_between_discovery_and_row_lock(review_context):
    user, _project, _client = review_context
    device = _device(user, 'renew-during-reap', lease_token_hash='token', lease_expires_at=timezone.now() - timedelta(seconds=1))
    manager = AppDevice.objects
    original = manager.select_for_update
    def renew_before_lock():
        manager.filter(pk=device.pk).update(lease_expires_at=timezone.now() + timedelta(hours=1))
        return original()
    with patch.object(manager, 'select_for_update', side_effect=renew_before_lock):
        assert device_pool.reap_expired_device_leases() == 0
    device.refresh_from_db()
    assert device.locked_by_id == user.pk
    assert device.lease_token_hash == 'token'


def test_many_jsonpath_assertions_parse_and_redact_the_response_once():
    from apps.api_testing import utils
    parsed = {'first': 1, 'second': 2, 'password': 'fixture-value'}
    response = SimpleNamespace(status_code=200, text=json.dumps(parsed), headers={'content-type': 'application/json'})
    assertions = [
        {'type': 'json_path', 'json_path': '$.first', 'expected': 1},
        {'type': 'json_path', 'json_path': '$.second', 'expected': 2},
        {'type': 'json_path', 'json_path': '$.password', 'expected': 'fixture-value'},
    ]
    with patch.object(utils.json, 'loads', return_value=parsed) as loads, patch.object(utils, 'redact_payload', wraps=utils.redact_payload) as redact:
        result = execute_assertions(response, assertions)
    assert all(row['passed'] for row in result)
    assert result[-1]['actual'] == result[-1]['expected'] == '[REDACTED]'
    assert loads.call_count == 1
    assert sum(call.args[0] is parsed for call in redact.call_args_list) == 1


def test_malformed_json_assertions_share_the_parse_failure():
    from apps.api_testing import utils
    response = SimpleNamespace(status_code=200, text='{malformed', headers={'content-type': 'application/json'})
    with patch.object(utils.json, 'loads', wraps=json.loads) as loads:
        result = execute_assertions(response, [{'type': 'json_path', 'json_path': '$.value', 'expected': 1}] * 4)
    assert all(not row['passed'] and row['error'] for row in result)
    assert len({row['error'] for row in result}) == 1
    assert loads.call_count == 1


def _document():
    return {'openapi': '3.0.0', 'servers': [{'url': 'https://public.example.test'}], 'paths': {
        f'/resource-{i}': {'get': {'responses': {'200': {'description': 'ok'}}}} for i in range(3)
    }}


def test_contract_run_reuses_generated_definitions_for_execution_and_coverage(review_context):
    user, project, client = review_context
    spec = OpenApiContractSpec.objects.create(project=project, name='review-contract', version='1', document=_document(), created_by=user)
    job = ExecutionJob.objects.create(project=project, requested_by=user, source_type='api', source_id='api:request:1')
    with patch('apps.api_testing.contracts_views.generate_contract_cases', wraps=generate_contract_cases) as generate, patch('apps.api_testing.contract_runner.generate_contract_cases', side_effect=AssertionError('execution regenerated definitions')), patch('apps.api_testing.contracts.generate_contract_cases', side_effect=AssertionError('coverage regenerated definitions')), patch('apps.api_testing.contract_runner.guarded_request', return_value=Mock(status_code=200)) as http:
        response = client.post(f'/api/api-testing/openapi-contracts/{spec.pk}/execute/', {'job_id': job.pk, 'kinds': ['positive'], 'limit': 2}, format='json')
    assert response.status_code == 201, response.data
    assert response.data['complete'] is True
    assert len(response.data['results']) == http.call_count == 2
    assert response.data['coverage']['total'] == 3
    assert response.data['coverage']['case_total'] == 6
    assert generate.call_count == 1


@pytest.mark.parametrize('field', ['artifact_page', 'quality_page'])
def test_evidence_excessive_offset_fails_before_counts(review_context, field):
    _user, _project, client = review_context
    with CaptureQueriesContext(connection) as queries:
        response = client.get('/api/executions/evidence-explorer/', {field: 1_000_000, 'page_size': 200})
    assert response.status_code == 400
    assert not any('COUNT(' in row['sql'].upper() for row in queries)


def test_empty_evidence_page_does_not_issue_a_data_scan(review_context):
    _user, _project, client = review_context
    with CaptureQueriesContext(connection) as queries:
        response = client.get('/api/executions/evidence-explorer/', {'artifact_page': 100, 'page_size': 200})
    assert response.status_code == 200
    assert response.data['execution_artifacts'] == []
    assert response.data['counts']['execution_artifacts'] == 0
    assert not any('execution_artifacts' in row['sql'] and ' LIMIT ' in row['sql'].upper() for row in queries)


def _dns_answer(address, port):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, port))]


@override_settings(PERF_ALLOW_PRIVATE_TARGETS=False)
def test_api_execution_cannot_connect_after_a_public_to_private_dns_rebind(review_context, monkeypatch):
    user, _project, _client = review_context
    project = ApiProject.objects.create(name='strict outbound source', owner=user)
    collection = ApiCollection.objects.create(name='strict collection', project=project)
    request = ApiRequest.objects.create(collection=collection, name='rebind', method='GET', url='http://public.example.test/resource', created_by=user)
    calls = []
    def dns(host, port, **kwargs):
        calls.append(host)
        return _dns_answer('93.184.216.34' if len(calls) == 1 else '127.0.0.1', port)
    connect = Mock(side_effect=AssertionError('No socket may open for rebound DNS'))
    monkeypatch.setattr('apps.core.network.socket.getaddrinfo', dns)
    monkeypatch.setattr('urllib3.connection.connection.create_connection', connect)
    result = execute_api_request(request, None, user)
    assert result['success'] is False
    assert len(calls) == 2
    assert RequestHistory.objects.filter(request=request).count() == 0
    connect.assert_not_called()


@override_settings(PERF_ALLOW_PRIVATE_TARGETS=False)
def test_contract_execution_cannot_connect_after_validating_rendered_targets(monkeypatch):
    spec = SimpleNamespace(pk=151, version='1', document=_document())
    calls = []
    def dns(host, port, **kwargs):
        calls.append(host)
        return _dns_answer('93.184.216.34' if len(calls) == 1 else '127.0.0.1', port)
    connect = Mock(side_effect=AssertionError('No socket may open for rebound DNS'))
    monkeypatch.setattr('apps.core.network.socket.getaddrinfo', dns)
    monkeypatch.setattr('urllib3.connection.connection.create_connection', connect)
    result = execute_contract_cases(spec, kinds=['positive'], limit=1)
    assert len(result) == 1 and result[0]['status'] == 'failed'
    assert len(calls) == 2
    connect.assert_not_called()


@pytest.mark.parametrize('runtime', [{'proxy': 'http://proxy.invalid'}, {'use_system_proxy': True}])
def test_strict_perf_mode_rejects_proxy_bypass_before_client_creation(monkeypatch, runtime):
    monkeypatch.setattr(builtin, '_allow_private_targets', lambda: False)
    client = Mock(side_effect=AssertionError('No proxy client may be constructed'))
    monkeypatch.setattr(builtin.httpx, 'AsyncClient', client)
    with pytest.raises(EngineError, match='proxy'):
        asyncio.run(builtin.debug_run({'runtime_config': runtime, 'steps': []}))
    client.assert_not_called()


def test_cluster_projection_keeps_a_mixed_step_failure_open_without_loading_step_json(review_context):
    user, project, _client = review_context
    job = ExecutionJob.objects.create(project=project, requested_by=user, source_type='api', status='failed', final_status='failed')
    attempt = ExecutionAttempt.objects.create(job=job, status='failed', is_final=True)
    job.final_attempt = attempt
    job.save(update_fields=['final_attempt'])
    ExecutionStepResult.objects.bulk_create([
        ExecutionStepResult(attempt=attempt, case_key='mixed.case', step_key=f'step-{i}', status='failed' if i < 4 else 'passed', error_type='assertion', error_message='fixture failure' if i < 4 else '', result={'large': 'x' * 2000})
        for i in range(8)
    ])
    with CaptureQueriesContext(connection) as queries:
        clusters = sync_execution_job_clusters(job)
    assert len(clusters) == 1
    cluster = QualityFailureCluster.objects.get()
    assert cluster.test_key == 'mixed.case'
    assert cluster.resolved_at is None
    assert cluster.occurrence_count == 1
    step_selects = [row['sql'] for row in queries if row['sql'].lstrip().upper().startswith('SELECT') and 'execution_step_results' in row['sql']]
    assert step_selects
    assert all('"execution_step_results"."result"' not in sql for sql in step_selects)
