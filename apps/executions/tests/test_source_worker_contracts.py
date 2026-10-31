from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.executions.adapters import PerformanceExecutorAdapter, UiExecutorAdapter
from apps.executions.models import ExecutionDataset, ExecutionJob, ExecutionSourceBinding
from apps.executions.source_workers import _app_execute, execute_ui_source
from apps.executions.worker_leases import claim_attempt, check_claim
from apps.projects.models import Project
from apps.users.models import User


@pytest.fixture
def worker_job(db):
    user = User.objects.create_user(username='source-worker-contract-owner', password='pass')
    project = Project.objects.create(name='Worker contracts', owner=user)
    return ExecutionJob.objects.create(project=project, source_type='ui', source_id='ui:suite:1', requested_by=user)


def test_ui_worker_applies_browser_variant_and_waits_for_all(worker_job):
    from apps.ui_automation.models import UiProject, TestSuite as UiSuite
    native = UiProject.objects.create(name='Worker native UI project', owner=worker_job.requested_by)
    suite = UiSuite.objects.create(project=native, name='Worker native UI suite')
    worker_job.source_id = f'ui:suite:{suite.pk}'
    worker_job.dataset = ExecutionDataset.objects.create(project=worker_job.project, name='browsers', data=[
        {'engine': 'playwright', 'browser': 'firefox', 'headless': True},
        {'engine': 'selenium', 'browser': 'edge', 'headless': False},
    ])
    worker_job.save(update_fields=['dataset', 'source_id'])
    UiExecutorAdapter(dispatcher=lambda *_: '').submit(worker_job)
    ExecutionSourceBinding.objects.create(
        project=worker_job.project, source_type='ui', source_id=worker_job.source_id,
        source_project_id=str(native.pk), created_by=worker_job.requested_by,
    )
    seen = []
    def execute(source, ref, bound, actor, metadata, cancel_check):
        cancel_check()
        seen.append(metadata)
        return True, {'observed': metadata}
    with patch('apps.executions.source_workers._ui_execute', side_effect=execute):
        for attempt in worker_job.attempts.order_by('attempt_number'):
            result = execute_ui_source.run(worker_job.pk, attempt.pk, variant_id=attempt.variant_id)
            assert result['status'] == 'passed'
    assert [row['browser'] for row in seen] == ['firefox', 'edge']
    assert [row['headless'] for row in seen] == [True, False]
    worker_job.refresh_from_db()
    assert worker_job.final_status == 'passed' and worker_job.final_result['count'] == 2


def test_app_source_carries_generation_zero_and_releases_its_lease(worker_job):
    actor = worker_job.requested_by
    device = SimpleNamespace(pk=3, lease_generation=0)
    execution = SimpleNamespace(pk=9, result='passed', status='success', refresh_from_db=lambda: None)
    source = SimpleNamespace(app_package=SimpleNamespace(package_name='test.package'))
    binding = SimpleNamespace(metadata={'approved': True})
    reference = SimpleNamespace(kind='case')
    with patch('apps.app_automation.device_pool.acquire_device', return_value=(device, 'test-only-lease')) as acquire, patch('apps.app_automation.device_pool.release_device') as release, patch('apps.app_automation.models.AppTestExecution.objects.create', return_value=execution) as create, patch('apps.app_automation.tasks.execute_app_test_task.run') as execute:
        success, result = _app_execute(source, reference, binding, actor)
    assert success and result['execution_id'] == 9
    assert create.call_args.kwargs['device_lease_generation'] == 0
    assert execute.call_args.kwargs['lease_generation'] == 0
    assert execute.call_args.kwargs['release_device_lease'] is False
    assert 'test-only-lease' not in str(execute.call_args)
    release.assert_called_once_with(device.pk, actor, 'test-only-lease')


def test_async_performance_child_owns_heartbeat_and_callback_fence(worker_job):
    from apps.perf_testing.models import PerfExecution, PerfProject, PerfScenario
    from apps.perf_testing.services import executor
    worker_job.source_type, worker_job.source_id = 'performance', 'performance:scenario:1'
    worker_job.save(update_fields=['source_type', 'source_id'])
    receipt = PerformanceExecutorAdapter(dispatcher=lambda *_: 'perf-launcher').submit(worker_job)
    claim_id = claim_attempt(worker_job.pk, receipt.attempt_id, 'perf-launcher')
    project = PerfProject.objects.create(name='performance source', owner=worker_job.requested_by)
    scenario = PerfScenario.objects.create(project=project, name='scenario', created_by=worker_job.requested_by)
    execution = PerfExecution.objects.create(project=project, scenario=scenario, execution_no='lease-contract-1', status='RUNNING', script_ref={
        'unified_execution': {'job_id': worker_job.pk, 'attempt_id': receipt.attempt_id, 'worker_claim_id': claim_id},
    })
    def run_child(execution_id, stdout=None, lease_heartbeat=None):
        assert lease_heartbeat.claim_id == claim_id
        assert lease_heartbeat._thread.is_alive()
        check_claim(worker_job.pk, receipt.attempt_id, claim_id, renew=True)
        execution.status, execution.sla_result, execution.summary = 'COMPLETED', 'PASSED', {'p95_rt': 80}
        execution.save(update_fields=['status', 'sla_result', 'summary'])
        assert executor._publish_unified_completion(execution)
        return execution
    with patch.object(executor, '_run_execution', side_effect=run_child):
        result = executor.run_execution(execution.pk)
    assert result.pk == execution.pk
    worker_job.refresh_from_db()
    assert worker_job.final_status == 'passed'
    assert worker_job.final_result['performance_execution_id'] == execution.pk
    execution.script_ref['unified_execution']['worker_claim_id'] = 'superseded'
    execution.status, execution.sla_result = 'FAILED', 'FAILED'
    assert executor._publish_unified_completion(execution) is False
    worker_job.refresh_from_db()
    assert worker_job.final_status == 'passed'
