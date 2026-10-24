from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.users.models import CIToken


class Command(BaseCommand):
    help = 'Revoke a project-scoped CI token without exposing its secret.'

    def add_arguments(self, parser):
        parser.add_argument('--project-id', type=int, required=True)
        parser.add_argument('--name', required=True)

    def handle(self, *args, **options):
        token = CIToken.objects.filter(
            project_id=options['project_id'], name=options['name']
        ).first()
        if token is None:
            raise CommandError('CI token 不存在')
        if token.revoked_at is None:
            token.revoked_at = timezone.now()
            token.save(update_fields=['revoked_at'])
        self.stdout.write(self.style.SUCCESS(f'revoked {token.name} ({token.token_prefix})'))
