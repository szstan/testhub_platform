"""Small, dependency-aware health endpoints for deployment and monitoring."""

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def health(request):
    """Liveness probe: the Django process is accepting HTTP requests."""
    return JsonResponse({'status': 'ok'})


@require_GET
def readiness(request):
    """Readiness probe for the database and shared cache dependencies."""
    checks = {}

    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        checks['database'] = 'ok'
    except Exception:
        checks['database'] = 'error'

    try:
        cache_key = 'testhub:readiness'
        cache.set(cache_key, 'ok', timeout=5)
        checks['cache'] = 'ok' if cache.get(cache_key) == 'ok' else 'error'
        cache.delete(cache_key)
    except Exception:
        checks['cache'] = 'error'

    ready = all(value == 'ok' for value in checks.values())
    return JsonResponse(
        {'status': 'ok' if ready else 'unready', 'checks': checks},
        status=200 if ready else 503,
    )
