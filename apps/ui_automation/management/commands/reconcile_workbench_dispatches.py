from django.core.management.base import BaseCommand, CommandError

from apps.ui_automation.workbench import (
    reconcile_pending_submissions, reconcile_stale_workbench_executions,
)


class Command(BaseCommand):
    help = 'Reconcile UI workbench dispatches and expired worker leases'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=20)

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        if batch_size < 0:
            raise CommandError('--batch-size must be zero or greater')
        failed = False
        for label, recover in (
            ('dispatches', reconcile_pending_submissions),
            ('workers', reconcile_stale_workbench_executions),
        ):
            try:
                count = recover(limit=batch_size)
                self.stdout.write(f'Reconciled UI workbench {label}: {count}')
            except Exception as exc:  # noqa: BLE001 - still attempt the other recovery
                failed = True
                self.stderr.write(f'UI workbench {label} recovery failed (error_type={type(exc).__name__})')
        if failed:
            raise CommandError('UI workbench reconciliation failed; see recovery error types above')
