from unittest.mock import patch

from apps.core.management.commands.run_all_scheduled_tasks import Command


def test_unified_scheduler_reaps_expired_device_leases_each_loop_without_device_io():
    command = Command()
    with (
        patch('apps.core.management.commands.run_all_scheduled_tasks.close_old_connections'),
        patch('apps.app_automation.device_pool.reap_expired_device_leases', return_value=2) as reap,
        patch('apps.app_automation.device_pool.acquire_specific_device') as acquire,
        patch.object(command, 'consume_retry_outbox', return_value=0),
        patch.object(command, 'recover_stale_generation_tasks', return_value=0),
        patch.object(command, 'reconcile_execution_claims'),
        patch.object(command, 'dispatch_pending_notifications'),
        patch.object(command, 'reconcile_performance_callbacks', return_value=0),
        patch.object(command, 'schedule_api_tasks', return_value=0),
        patch.object(command, 'schedule_ui_tasks', return_value=0),
        patch.object(command, 'schedule_app_tasks', return_value=0),
        patch.object(command, 'schedule_perf_tasks', return_value=0),
    ):
        command.handle(interval=1, once=True, lease_seconds=60)

    reap.assert_called_once_with()
    acquire.assert_not_called()


def test_reaper_failure_does_not_stop_the_scheduler_maintenance_step(caplog):
    command = Command()
    with patch(
        'apps.app_automation.device_pool.reap_expired_device_leases',
        side_effect=RuntimeError('synthetic reaper failure'),
    ):
        assert command.reap_expired_app_device_leases() == 0
    assert 'synthetic reaper failure' not in caplog.text
