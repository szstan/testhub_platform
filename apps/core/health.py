"""Dependency-aware health and readiness endpoints for deployment monitoring."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from asgiref.sync import async_to_sync
from celery import current_app
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from channels.layers import get_channel_layer


_ASSET_RE = re.compile(r"(?:src|href)=[\"']([^\"']+)[\"']")


def health(request):
    """Liveness probe: the Django process is accepting HTTP requests."""
    return JsonResponse({'status': 'ok'})


def _check_database():
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        return 'ok'
    except Exception:  # noqa: BLE001 - readiness must return a bounded response
        return 'error'


def _check_cache():
    try:
        cache_key = f'testhub:readiness:{uuid.uuid4().hex}'
        cache.set(cache_key, 'ok', timeout=5)
        result = 'ok' if cache.get(cache_key) == 'ok' else 'error'
        cache.delete(cache_key)
        return result
    except Exception:  # noqa: BLE001
        return 'error'


def _check_worker():
    """Check the Celery broker without publishing a business task."""
    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        return 'ok'
    try:
        broker = current_app.connection_for_read()
        broker.ensure_connection(max_retries=0)
        broker.close()
        return 'ok'
    except Exception:  # noqa: BLE001
        return 'error'


def _check_channel():
    """Perform a bounded channel-layer send to prove the configured backend works."""
    try:
        layer = get_channel_layer()
        if layer is None:
            return 'error'
        channel = async_to_sync(layer.new_channel)()
        async_to_sync(layer.send)(channel, {'type': 'health.ping'})
        return 'ok'
    except Exception:  # noqa: BLE001
        return 'error'


def _check_migrations():
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        return 'error' if pending else 'ok'
    except Exception:  # noqa: BLE001
        return 'error'


def _check_frontend_assets():
    asset_root = Path(
        getattr(settings, 'FRONTEND_ASSET_DIR', '')
        or Path(settings.BASE_DIR) / 'frontend' / 'dist'
    ).expanduser().resolve()
    index = asset_root / 'index.html'
    if not index.is_file() or not index.stat().st_size:
        return 'error'
    try:
        references = [match.group(1) for match in _ASSET_RE.finditer(index.read_text(encoding='utf-8'))]
    except (OSError, UnicodeError):
        return 'error'
    assets = [reference for reference in references if reference.startswith('assets/')]
    if not assets:
        return 'error'
    for reference in assets:
        candidate = (asset_root / reference).resolve()
        if asset_root not in candidate.parents or not candidate.is_file():
            return 'error'
    return 'ok'


def _required_checks():
    testing = bool(getattr(settings, 'TESTING', False))
    return {
        'worker': bool(getattr(settings, 'READINESS_REQUIRE_WORKER', not testing)),
        'channel': bool(getattr(settings, 'READINESS_REQUIRE_CHANNEL', not testing)),
        'migrations': bool(getattr(settings, 'READINESS_REQUIRE_MIGRATIONS', not testing)),
        'frontend_assets': bool(getattr(settings, 'READINESS_REQUIRE_FRONTEND_ASSETS', not testing)),
    }


@require_GET
def readiness(request):
    """Readiness probe for DB/cache and configured worker/runtime dependencies."""
    checks = {
        'database': _check_database(),
        'cache': _check_cache(),
    }
    required = _required_checks()
    checkers = {
        'worker': _check_worker,
        'channel': _check_channel,
        'migrations': _check_migrations,
        'frontend_assets': _check_frontend_assets,
    }
    for name, checker in checkers.items():
        checks[name] = checker() if required[name] else 'skipped'

    ready = checks['database'] == 'ok' and checks['cache'] == 'ok'
    ready = ready and all(checks[name] == 'ok' for name, enabled in required.items() if enabled)
    return JsonResponse(
        {
            'status': 'ok' if ready else 'unready',
            'checks': checks,
            'required': required,
        },
        status=200 if ready else 503,
    )
