from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.users.models import CIToken


class Command(BaseCommand):
    help = 'List CI token metadata without exposing token secrets or hashes.'

    def add_arguments(self, parser):
        parser.add_argument('--project-id', type=int)
        parser.add_argument('--all', action='store_true', help='Include revoked tokens')

    def handle(self, *args, **options):
        tokens = CIToken.objects.select_related('project', 'user').order_by('project_id', 'name')
        if options.get('project_id'):
            tokens = tokens.filter(project_id=options['project_id'])
        if not options['all']:
            tokens = tokens.filter(revoked_at__isnull=True)
        for token in tokens:
            expires = token.expires_at.isoformat() if token.expires_at else 'never'
            revoked = token.revoked_at.isoformat() if token.revoked_at else '-'
            last_used = token.last_used_at.isoformat() if token.last_used_at else '-'
            self.stdout.write(
                f'{token.project_id}\t{token.name}\t{token.token_prefix}\t'
                f'{token.user.username}\t{expires}\t{revoked}\t{last_used}'
            )
