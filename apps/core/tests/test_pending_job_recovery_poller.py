from contextlib import ExitStack
from io import StringIO
from unittest.mock import patch

import pytest

from apps.core.management.commands.run_all_scheduled_tasks import Command


def test_unified_poller_recovers_ui_and_document_and_judge_work_before_scheduling():
    command = Command(stdout=StringIO())
    other_methods = (
        'reap_expired_app_device_leases', 'consume_retry_outbox', 'recover_stale_generation_tasks',
        'reconcile_stale_api_case_executions', 'reconcile_execution_claims',
        'dispatch_pending_notifications', 'reconcile_performance_callbacks',
        'schedule_api_tasks', 'schedule_ui_tasks', 'schedule_app_tasks',
        'schedule_perf_tasks', 'schedule_monitor_tasks',
    )
    order = []
    with ExitStack() as stack:
        stack.enter_context(patch('apps.core.management.commands.run_all_scheduled_tasks.close_old_connections'))
        for method in other_methods:
            stack.enter_context(patch.object(command, method, return_value=0))
        ui = stack.enter_context(patch.object(command, 'reconcile_pending_ui_submissions', side_effect=lambda: order.append('ui') or 2))
        ui_workers = stack.enter_context(patch.object(command, 'reconcile_stale_ui_executions', side_effect=lambda: order.append('ui_workers') or 4))
        analyses = stack.enter_context(patch.object(command, 'reconcile_analysis_tasks', side_effect=lambda: order.append('analyses') or 5))
        judge = stack.enter_context(patch.object(command, 'reconcile_stale_judge_batches', side_effect=lambda: order.append('judge') or 3))
        api = stack.enter_context(patch.object(command, 'schedule_api_tasks', side_effect=lambda: order.append('schedule') or 0))
        command.handle(interval=1, once=True, lease_seconds=60)
    ui.assert_called_once_with()
    ui_workers.assert_called_once_with()
    analyses.assert_called_once_with()
    judge.assert_called_once_with()
    api.assert_called_once_with()
    assert order == ['ui', 'ui_workers', 'analyses', 'judge', 'schedule']
    assert 'UI submissions recovered: 2' in command.stdout._out.getvalue()
    assert 'UI workers recovered: 4' in command.stdout._out.getvalue()
    assert 'document analyses recovered: 5' in command.stdout._out.getvalue()
    assert 'judge batches recovered: 3' in command.stdout._out.getvalue()


@pytest.mark.parametrize('helper,target,limit', [
    ('reconcile_pending_ui_submissions', 'apps.ui_automation.workbench.reconcile_pending_submissions', 20),
    ('reconcile_stale_ui_executions', 'apps.ui_automation.workbench.reconcile_stale_workbench_executions', 20),
    ('reconcile_analysis_tasks', 'apps.requirement_analysis.tasks.reconcile_analysis_tasks', 10),
    ('reconcile_stale_judge_batches', 'apps.llm_judge.tasks.reconcile_stale_batches', 10),
])
def test_recovery_helpers_use_a_bounded_batch_and_tolerate_unavailable_broker(helper, target, limit):
    command = Command()
    with patch(target, return_value=4, create=True) as recover:
        assert getattr(command, helper)() == 4
    recover.assert_called_once_with(limit=limit)
    with patch(target, side_effect=RuntimeError('broker unavailable'), create=True):
        assert getattr(command, helper)() == 0
