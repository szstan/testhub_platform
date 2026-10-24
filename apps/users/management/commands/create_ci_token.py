from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.projects.models import Project
from apps.users.models import CIToken, User


class Command(BaseCommand):
    help = 'Create a project-scoped long-lived CI token; the token is printed once.'

    def add_arguments(self, parser):
        parser.add_argument('--project-id', type=int, required=True)
        parser.add_argument('--username', required=True)
        parser.add_argument('--name', required=True)
        parser.add_argument('--expires-days', type=int, default=90)

    def handle(self, *args, **options):
        project = Project.objects.filter(pk=options['project_id']).first()
        if project is None:
            raise CommandError('项目不存在')
        user = User.objects.filter(username=options['username'], is_active=True).first()
        if user is None:
            raise CommandError('用户不存在或已禁用')
        if not (user.is_staff or user.is_superuser or project.owner_id == user.id or project.members.filter(pk=user.pk).exists()):
            raise CommandError('用户不是项目成员')
        expires_days = options['expires_days']
        if expires_days < 1 or expires_days > 3650:
            raise CommandError('expires-days 必须在 1 到 3650 之间')
        raw = f'th_ci_{secrets.token_urlsafe(36)}'
        CIToken.objects.create(
            project=project,
            user=user,
            name=options['name'],
            token_prefix=raw[:16],
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=expires_days),
        )
        self.stdout.write(raw)
