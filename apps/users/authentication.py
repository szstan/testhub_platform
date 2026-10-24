from __future__ import annotations

import hashlib
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import CIToken


class CITokenAuthentication(BaseAuthentication):
    """Authenticate Bearer th_ci tokens without changing JWT behavior."""

    keyword = 'Bearer'
    allowed_paths = (
        '/api/executions/jobs',
        '/api/executions/import-results',
    )

    def authenticate_header(self, request):
        return self.keyword

    def authenticate(self, request):
        header = request.headers.get('Authorization', '')
        prefix = f'{self.keyword} '
        if not header.startswith(prefix):
            return None
        raw = header[len(prefix):].strip()
        if not raw.startswith('th_ci_'):
            return None
        path = request.path.rstrip('/') or '/'
        if not any(path == allowed or path.startswith(f'{allowed}/') for allowed in self.allowed_paths):
            raise AuthenticationFailed('CI token scope does not allow this endpoint')
        token_hash = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        token = CIToken.objects.select_related('user', 'project').filter(
            token_hash=token_hash,
            revoked_at__isnull=True,
            user__is_active=True,
            project__owner__is_active=True,
        ).first()
        if token is None or (token.expires_at and token.expires_at <= timezone.now()):
            raise AuthenticationFailed('CI token is invalid or expired')
        CIToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        return token.user, token
