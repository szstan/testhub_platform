"""Recovery commands keep queues independent and honor per-queue limits."""
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management.base import CommandError

from apps.ui_automation.management.commands import reconcile_workbench_dispatches as recovery


def test_workbench_command_recovers_dispatches_and_worker_leases_with_one_bound():
    output = StringIO()
    command = recovery.Command(stdout=output)
    with patch.object(recovery, 'reconcile_pending_submissions', return_value=2) as pending, patch.object(
        recovery, 'reconcile_stale_workbench_executions', return_value=3,
    ) as workers:
        command.handle(batch_size=7)
    pending.assert_called_once_with(limit=7)
    workers.assert_called_once_with(limit=7)
    assert 'dispatches: 2' in output.getvalue()
    assert 'workers: 3' in output.getvalue()


def test_workbench_command_does_not_let_broker_outage_skip_worker_recovery():
    error = StringIO()
    command = recovery.Command(stdout=StringIO(), stderr=error)
    with patch.object(recovery, 'reconcile_pending_submissions', side_effect=RuntimeError('outage')), patch.object(
        recovery, 'reconcile_stale_workbench_executions', return_value=3,
    ) as workers:
        with pytest.raises(CommandError, match='reconciliation failed'):
            command.handle(batch_size=7)
    workers.assert_called_once_with(limit=7)
    assert 'error_type=RuntimeError' in error.getvalue()


def test_workbench_command_rejects_negative_batch_without_recovery():
    command = recovery.Command(stdout=StringIO())
    with patch.object(recovery, 'reconcile_pending_submissions') as pending, patch.object(
        recovery, 'reconcile_stale_workbench_executions',
    ) as workers:
        with pytest.raises(CommandError, match='zero or greater'):
            command.handle(batch_size=-1)
    pending.assert_not_called()
    workers.assert_not_called()
