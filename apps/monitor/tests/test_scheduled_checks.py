"""Regression coverage for durable monitor dispatch, recovery, and fencing."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.core.management.commands.run_all_scheduled_tasks import Command
from apps.core.scheduler import ScheduleClaimLost, ScheduleLeaseHeartbeat, claim_due_task, renew_claim
from apps.monitor.models import AlertEvent, MonitorCheckLog, MonitorTarget
from apps.monitor.serializers import MonitorTargetSerializer
from apps.monitor.tasks import execute_monitor_check
from apps.monitor.utils.probes import ProbeResult
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def target():
    user = User.objects.create_user(username='scheduled-monitor-owner')
    return MonitorTarget.objects.create(name='scheduled-health', type='HTTP', created_by=user)


def _claim(target, **kwargs):
    return claim_due_task(
        MonitorTarget, target.pk, due_field='next_check_at',
        status_field='enabled', active_status=True, allow_null_due=True,
        **kwargs,
    )


def test_scheduler_dispatches_only_due_enabled_targets_and_never_probes_in_poller(target):
    future = MonitorTarget.objects.create(
        name='future-health', type='HTTP', next_check_at=timezone.now() + timedelta(hours=1),
    )
    disabled = MonitorTarget.objects.create(name='disabled-health', type='HTTP', enabled=False)
    command = Command()
    command.lease_seconds = 60
    with (
        patch('apps.monitor.utils.scheduler_heartbeat.set_scheduler_beat') as beat,
        patch('apps.monitor.tasks.execute_monitor_check.delay') as dispatch,
        patch('apps.monitor.utils.probes.run_probe') as probe,
    ):
        assert command.schedule_monitor_tasks() == 1
        assert command.schedule_monitor_tasks() == 0
    assert dispatch.call_count == 1
    assert dispatch.call_args.args[0] == target.pk
    assert dispatch.call_args.args[2] == 60
    assert beat.call_count == 2
    probe.assert_not_called()
    for untouched in (future, disabled):
        untouched.refresh_from_db()
        assert untouched.scheduler_lease_token == ''


def test_dispatch_failure_releases_only_its_lease_and_backs_off(target):
    command = Command()
    with (
        patch('apps.monitor.utils.scheduler_heartbeat.set_scheduler_beat'),
        patch('apps.monitor.tasks.execute_monitor_check.delay', side_effect=RuntimeError('broker offline')),
    ):
        assert command.schedule_monitor_tasks() == 0
    target.refresh_from_db()
    assert target.scheduler_lease_token == ''
    assert target.next_check_at > timezone.now()


def test_worker_persists_probe_and_releases_lease(target):
    claim = _claim(target)
    with (
        patch('apps.monitor.tasks.ScheduleLeaseHeartbeat') as heartbeat,
        patch('apps.monitor.utils.probes.run_probe', return_value=ProbeResult(True, 'ok', latency_ms=7)),
    ):
        result = execute_monitor_check.run(target.pk, claim.token)
    target.refresh_from_db()
    assert result['status'] == 'checked'
    assert target.status == 'UP'
    assert target.next_check_at > timezone.now()
    assert target.scheduler_lease_token == ''
    assert MonitorCheckLog.objects.get(target=target).latency_ms == 7
    heartbeat.return_value.start.assert_called_once()
    heartbeat.return_value.stop.assert_called_once()


def test_expired_claim_can_be_reclaimed_but_old_worker_cannot_change_successor(target):
    first = _claim(target)
    MonitorTarget.objects.filter(pk=target.pk).update(
        scheduler_lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    second = _claim(target)
    assert second is not None and second.token != first.token
    with patch('apps.monitor.utils.probes.run_probe') as probe:
        result = execute_monitor_check.run(target.pk, first.token)
    assert result['status'] == 'superseded'
    probe.assert_not_called()
    target.refresh_from_db()
    assert target.scheduler_lease_token == second.token
    assert not MonitorCheckLog.objects.filter(target=target).exists()


def test_response_after_reclaim_cannot_persist_results_or_clear_new_lease(target):
    claim = _claim(target)

    def late_response(*_args):
        MonitorTarget.objects.filter(pk=target.pk).update(
            scheduler_lease_token='successor',
            scheduler_lease_expires_at=timezone.now() + timedelta(seconds=300),
        )
        return ProbeResult(False, 'late failure')

    with (
        patch('apps.monitor.tasks.ScheduleLeaseHeartbeat'),
        patch('apps.monitor.utils.probes.run_probe', side_effect=late_response),
    ):
        result = execute_monitor_check.run(target.pk, claim.token)
    assert result['status'] == 'superseded'
    target.refresh_from_db()
    assert target.status == 'UNKNOWN'
    assert target.scheduler_lease_token == 'successor'
    assert not MonitorCheckLog.objects.filter(target=target).exists()
    assert not AlertEvent.objects.filter(target=target).exists()


def test_duplicate_broker_delivery_does_not_probe_twice(target):
    claim = _claim(target)
    with (
        patch('apps.monitor.tasks.ScheduleLeaseHeartbeat'),
        patch('apps.monitor.utils.probes.run_probe', return_value=ProbeResult(True, 'ok')) as probe,
    ):
        first = execute_monitor_check.run(target.pk, claim.token)
        duplicate = execute_monitor_check.run(target.pk, claim.token)
    assert first['status'] == 'checked'
    assert duplicate['status'] == 'superseded'
    probe.assert_called_once()
    assert MonitorCheckLog.objects.filter(target=target).count() == 1


def test_boolean_enabled_lease_renews_and_disabled_target_loses_authority(target):
    claim = _claim(target, lease_seconds=1)
    renewed = renew_claim(
        MonitorTarget, target.pk, claim.token, lease_seconds=60,
        status_field='enabled', active_status=True,
    )
    assert renewed.expires_at > claim.expires_at
    heartbeat = ScheduleLeaseHeartbeat(
        MonitorTarget, target.pk, claim.token, status_field='enabled', active_status=True,
    )
    heartbeat.raise_if_lost()
    MonitorTarget.objects.filter(pk=target.pk).update(enabled=False)
    with pytest.raises(ScheduleClaimLost):
        heartbeat.raise_if_lost()


def test_scheduler_fields_are_not_client_writable_or_disclosed(target):
    data = MonitorTargetSerializer(target).data
    assert 'scheduler_lease_token' not in data
    assert 'scheduler_lease_expires_at' not in data


def test_unified_poller_calls_monitor_dispatch_each_iteration():
    command = Command()
    methods = (
        'reap_expired_app_device_leases', 'consume_retry_outbox', 'recover_stale_generation_tasks',
        'reconcile_stale_api_case_executions', 'reconcile_execution_claims',
        'dispatch_pending_notifications', 'reconcile_performance_callbacks',
        'schedule_api_tasks', 'schedule_ui_tasks', 'schedule_app_tasks', 'schedule_perf_tasks',
    )
    from contextlib import ExitStack
    with ExitStack() as stack:
        stack.enter_context(patch('apps.core.management.commands.run_all_scheduled_tasks.close_old_connections'))
        for method in methods:
            stack.enter_context(patch.object(command, method, return_value=0))
        monitor = stack.enter_context(patch.object(command, 'schedule_monitor_tasks', return_value=1))
        command.handle(interval=1, once=True, lease_seconds=60)
        monitor.assert_called_once_with()
