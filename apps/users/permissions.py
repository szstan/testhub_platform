from rest_framework.permissions import BasePermission

from .models import CIToken


class CIServiceTokenPermission(BasePermission):
    """Keep project-scoped CI credentials limited to CI execution endpoints."""

    allowed_paths = (
        '/api/executions/jobs',
        '/api/executions/import-results',
    )

    def has_permission(self, request, view):
        if not isinstance(request.auth, CIToken):
            return True
        path = request.path.rstrip('/') or '/'
        return any(path == allowed or path.startswith(f'{allowed}/') for allowed in self.allowed_paths)
