"""Lease loss must stop the next source action without touching a successor claim."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.core.cancellation import execution_cancellation_scope, raise_if_cancelled
from apps.core.scheduler import (
    ScheduleClaimLost, ScheduleLeaseHeartbeat, claim_due_task, renew_claim,
)


@pytest.fixture
def claimed_api_schedule(db):
    from apps.api_testing.models import ScheduledTask
    from apps.users.models import User

    user = User.objects.create_user(username='lease-cancellation-owner')
    task = ScheduledTask.objects.create(
        name='claim-cancellation', task_type='API_REQUEST', trigger_type='INTERVAL',
        interval_seconds=60, created_by=user,
        next_run_time=timezone.now() - timedelta(seconds=1),
    )
    claim = claim_due_task(ScheduledTask, task.pk, due_field='next_run_time', lease_seconds=60)
    return task, claim


@pytest.mark.django_db
@pytest.mark.parametrize('change', ['replacement', 'expired', 'paused'])
def test_schedule_action_boundary_checks_authoritative_claim(claimed_api_schedule, change):
    task, claim = claimed_api_schedule
    heartbeat = ScheduleLeaseHeartbeat(type(task), task.pk, claim.token, lease_seconds=60)
    heartbeat.raise_if_lost()
    updates = {
        'replacement': {'scheduler_lease_token': 'successor'},
        'expired': {'scheduler_lease_expires_at': timezone.now() - timedelta(seconds=1)},
        'paused': {'status': 'PAUSED'},
    }[change]
    type(task).objects.filter(pk=task.pk).update(**updates)
    with pytest.raises(ScheduleClaimLost):
        heartbeat.raise_if_lost()
    # Once lost, even a restored row cannot revive this worker's authority.
    type(task).objects.filter(pk=task.pk).update(
        status='ACTIVE', scheduler_lease_token=claim.token,
        scheduler_lease_expires_at=timezone.now() + timedelta(seconds=60),
    )
    with pytest.raises(ScheduleClaimLost):
        heartbeat.raise_if_lost()


@pytest.mark.django_db
@pytest.mark.django_db
def test_api_scheduler_broker_failure_settles_execution_log_and_releases_claim():
    from apps.api_testing.models import ApiProject, ScheduledTask, TaskExecutionLog
    from apps.core.management.commands.run_all_scheduled_tasks import Command
    from apps.users.models import User

    user = User.objects.create_user(username='api-scheduler-enqueue-failure')
    project = ApiProject.objects.create(
        name='scheduler enqueue failure', project_type='HTTP', status='IN_PROGRESS', owner=user,
    )
    task = ScheduledTask.objects.create(
        name='scheduler broker failure', task_type='API_REQUEST', trigger_type='INTERVAL',
        interval_seconds=60, created_by=user,
        next_run_time=timezone.now() - timedelta(seconds=1),
    )
    command = Command()
    command.lease_seconds = 60

    with patch('apps.api_testing.tasks.execute_scheduled_task.delay', side_effect=RuntimeError('broker offline')):
        assert command.schedule_api_tasks() == 0

    log = TaskExecutionLog.objects.get(task=task)
    task.refresh_from_db()
    assert log.status == 'FAILED'
    assert log.end_time is not None
    assert 'broker offline' in log.error_message
    assert task.failed_runs == 1
    assert task.scheduler_lease_token == ''


def test_schedule_expiry_cannot_be_resurrected_by_heartbeat(claimed_api_schedule):
    task, claim = claimed_api_schedule
    type(task).objects.filter(pk=task.pk).update(
        scheduler_lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    with pytest.raises(ScheduleClaimLost, match='expired'):
        renew_claim(type(task), task.pk, claim.token)
    heartbeat = ScheduleLeaseHeartbeat(type(task), task.pk, claim.token)
    with patch('apps.core.scheduler.Thread') as thread:
        with pytest.raises(ScheduleClaimLost):
            heartbeat.start()
    thread.assert_not_called()


def test_schedule_lost_heartbeat_and_local_deadline_signal_cancellation():
    heartbeat = ScheduleLeaseHeartbeat(object, 3, 'claim')
    heartbeat._stop = Mock()
    heartbeat._stop.wait.side_effect = [False, True]
    with (
        patch('apps.core.scheduler.renew_claim', side_effect=ScheduleClaimLost('superseded')),
        patch('apps.core.scheduler.close_old_connections'),
    ):
        heartbeat._run()
    with pytest.raises(ScheduleClaimLost):
        heartbeat.raise_if_lost()
    heartbeat = ScheduleLeaseHeartbeat(object, 3, 'claim')
    heartbeat._deadline = 0
    with pytest.raises(ScheduleClaimLost):
        heartbeat.raise_if_lost()


def test_cancellation_scopes_are_composed_and_restored():
    check = Mock()
    with execution_cancellation_scope(check) as outer:
        raise_if_cancelled()
        with execution_cancellation_scope() as inner:
            inner.cancel('device lost')
            with pytest.raises(RuntimeError, match='device lost'):
                raise_if_cancelled()
        outer.raise_if_cancelled()
    raise_if_cancelled()
    assert check.call_count == 2


def test_one_second_schedule_lease_renews_with_a_strict_margin():
    heartbeat = ScheduleLeaseHeartbeat(object, 1, 'claim', lease_seconds=1)
    assert 0 < heartbeat.interval_seconds < heartbeat.lease_seconds


@pytest.mark.django_db
@pytest.mark.parametrize('task_type,runner_name', [
    ('API_REQUEST', 'execute_api_request'), ('TEST_SUITE', 'execute_test_suite'),
])
def test_api_worker_cancels_running_log_before_next_action(claimed_api_schedule, task_type, runner_name):
    from apps.api_testing.models import TaskExecutionLog
    from apps.api_testing.tasks import execute_scheduled_task

    task, claim = claimed_api_schedule
    log = TaskExecutionLog.objects.create(task=task)
    snapshot = SimpleNamespace(
        task_type=task_type, test_suite=object(), api_request=object(),
        environment=None, created_by=task.created_by,
    )
    actions = []

    def source(*args, cancel_check):
        cancel_check()
        actions.append('first')
        type(task).objects.filter(pk=task.pk).update(scheduler_lease_token='successor')
        cancel_check()
        actions.append('second')
        return {'success': True}

    with (
        patch('apps.api_testing.tasks._load_task', return_value=snapshot),
        patch('apps.api_testing.tasks._finish_task') as finish,
        patch('apps.core.scheduler.Thread'),
        patch(f'apps.api_testing.utils.{runner_name}', side_effect=source),
    ):
        receipt = execute_scheduled_task.run(task.pk, log.pk, claim.token, 60)
    assert receipt['claim_lost'] is True
    assert actions == ['first']
    finish.assert_not_called()
    log.refresh_from_db()
    task.refresh_from_db()
    assert log.status == 'CANCELLED'
    assert log.end_time is not None
    assert log.duration is not None
    assert task.scheduler_lease_token == 'successor'
    assert task.total_runs == 0


@pytest.mark.parametrize('engine', ['playwright', 'selenium'])
def test_ui_cancellation_stops_before_next_step(engine):
    from apps.ui_automation.test_executor import TestExecutor

    lost = {'value': False}

    def check():
        if lost['value']:
            raise ScheduleClaimLost('UI claim lost')

    executor = TestExecutor(object(), engine=engine, cancel_check=check)
    step = Mock()

    def first_action(*args):
        lost['value'] = True
        return {'status': 'passed'}

    step.side_effect = first_action
    setattr(executor, f'execute_step_{engine}', step)
    case = {'id': 1, 'name': 'case', 'steps': [{}, {}]}
    with pytest.raises(ScheduleClaimLost):
        if engine == 'playwright':
            executor.execute_test_case_playwright_no_db(case)
        else:
            executor.execute_test_case_selenium_no_db(Mock(), case)
    assert step.call_count == 1


@pytest.mark.parametrize('engine', ['playwright', 'selenium'])
def test_ui_step_rejects_cancelled_scope_before_browser_calls(engine):
    from apps.ui_automation.test_executor import TestExecutor

    check = Mock(side_effect=ScheduleClaimLost('lost before action'))
    executor = TestExecutor(object(), engine=engine, cancel_check=check)
    page = Mock()
    executor.current_page = page
    with pytest.raises(ScheduleClaimLost):
        if engine == 'playwright':
            executor.execute_step_playwright({})
        else:
            executor.execute_step_selenium(page, {})
    assert page.mock_calls == []


def test_performance_dispatch_claim_loss_stops_started_child():
    from apps.core.management.commands.run_all_scheduled_tasks import Command

    command = Command()
    command.lease_seconds = 60
    scenario = Mock()
    scenario.has_active_execution.return_value = False
    schedule = SimpleNamespace(pk=7, scenario=scenario, created_by=object(), mark_dispatched=Mock(
        side_effect=ScheduleClaimLost('lost after start'),
    ))
    execution = SimpleNamespace(pk=9)
    with (
        patch('apps.perf_testing.models.PerfScheduledTask.objects') as objects,
        patch.object(command, '_claim', return_value=SimpleNamespace(task=schedule, token='claim')),
        patch.object(command, '_mark_perf_dispatch_failure') as record,
        patch('apps.perf_testing.services.executor.start_execution', return_value=(execution, {})),
        patch('apps.perf_testing.services.executor.stop_execution') as stop,
    ):
        objects.filter.return_value.values_list.return_value = [schedule.pk]
        assert command.schedule_perf_tasks() == 0
    stop.assert_called_once_with(execution, graceful=False)
    record.assert_called_once()


def test_ui_runner_propagates_cancellation_and_closes_reused_browser():
    from apps.ui_automation.test_executor import TestExecutor

    state = {'lost': False}

    def check():
        if state['lost']:
            raise ScheduleClaimLost('lost during UI suite')

    executor = TestExecutor(object(), engine='selenium', cancel_check=check)
    driver = Mock()

    def run_suite():
        executor._selenium_driver = driver
        state['lost'] = True
        executor._check_cancelled()

    with (
        patch('apps.ui_automation.test_executor.connection.close'),
        patch.object(executor, 'create_execution_record'),
        patch.object(executor, 'get_test_cases'),
        patch.object(executor, 'run_with_selenium', side_effect=run_suite),
    ):
        executor.execution = SimpleNamespace(id=1)
        executor.update_execution_result = Mock()
        with pytest.raises(ScheduleClaimLost):
            executor.run()
    driver.quit.assert_called_once()
    executor.update_execution_result.assert_called_once()
