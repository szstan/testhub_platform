from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.executions.models import ExecutionArtifact


class Command(BaseCommand):
    help = 'Dry-run or apply cleanup of expired unified execution artifact references.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Delete expired DB references.')
        parser.add_argument('--limit', type=int, default=1000, help='Maximum rows to inspect/delete.')

    def handle(self, *args, **options):
        limit = max(int(options.get('limit') or 1000), 1)
        queryset = ExecutionArtifact.objects.filter(
            retention_until__isnull=False,
            retention_until__lt=timezone.now(),
        ).order_by('retention_until')
        rows = list(queryset[:limit])
        if not rows:
            self.stdout.write(self.style.SUCCESS('No expired execution artifacts found.'))
            return

        if not options.get('apply'):
            self.stdout.write(f'DRY RUN: {len(rows)} expired execution artifact references would be removed.')
            for artifact in rows:
                self.stdout.write(f'  id={artifact.pk} uri={artifact.uri[:160]}')
            self.stdout.write('Re-run with --apply to delete database references; storage objects are not touched.')
            return

        deleted = 0
        for artifact in rows:
            artifact.delete()
            deleted += 1
        self.stdout.write(self.style.SUCCESS(f'Deleted {deleted} expired execution artifact references.'))
