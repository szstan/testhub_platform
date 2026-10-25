"""Short-lived authentication tickets for browser WebSocket handshakes.

Browsers cannot attach an Authorization header to the WebSocket constructor.  A
browser therefore exchanges its in-memory API access token for a short-lived,
path-bound ticket over the authenticated HTTP API.  The ticket is intentionally
not a JWT and is consumed once by the ASGI middleware before the consumer runs.
"""

from __future__ import annotations

import logging
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache

TICKET_SALT = 'testhub.websocket'
TICKET_CACHE_PREFIX = 'testhub:websocket-ticket:'
DEFAULT_TICKET_TTL = 60
MIN_TICKET_TTL = 15
MAX_TICKET_TTL = 120
logger = logging.getLogger(__name__)


def ticket_ttl() -> int:
    """Return a bounded TTL so a deployment cannot accidentally issue long-lived tickets."""
    try:
        configured = int(getattr(settings, 'WEBSOCKET_TICKET_TTL', DEFAULT_TICKET_TTL))
    except (TypeError, ValueError):
        configured = DEFAULT_TICKET_TTL
    return max(MIN_TICKET_TTL, min(MAX_TICKET_TTL, configured))


def issue_websocket_ticket(*, user, path: str) -> tuple[str, int]:
    """Create a signed ticket bound to one user and exact WebSocket path."""
    if not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', False):
        raise ValueError('an active authenticated user is required')
    if not isinstance(path, str) or not path.startswith('/ws/'):
        raise ValueError('ticket path must be a WebSocket path')

    nonce = secrets.token_urlsafe(32)
    payload = {
        'v': 1,
        'uid': str(user.pk),
        'path': path,
        'nonce': nonce,
    }
    ttl = ticket_ttl()
    ticket = signing.dumps(payload, salt=TICKET_SALT, compress=True)
    cache.set(
        f'{TICKET_CACHE_PREFIX}{nonce}',
        {'uid': str(user.pk), 'path': path},
        timeout=ttl,
    )
    return ticket, ttl


def consume_websocket_ticket(ticket: str, path: str):
    """Validate and consume a ticket, returning the active user or ``None``.

    The short cache claim prevents two concurrent handshake workers from using
    the same nonce.  ``cache.add`` is atomic on the production Redis backend
    and also keeps the DB-free test backend deterministic.
    """
    if not ticket or not isinstance(path, str):
        return None
    try:
        payload = signing.loads(ticket, salt=TICKET_SALT, max_age=ticket_ttl())
    except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get('v') != 1:
        return None
    if payload.get('path') != path:
        return None

    nonce = payload.get('nonce')
    user_id = payload.get('uid')
    if not isinstance(nonce, str) or not nonce or not isinstance(user_id, str) or not user_id:
        return None

    cache_key = f'{TICKET_CACHE_PREFIX}{nonce}'
    claim_key = f'{cache_key}:claim'
    if not cache.add(claim_key, True, timeout=ticket_ttl()):
        return None
    try:
        cached = cache.get(cache_key)
        if not isinstance(cached, dict):
            return None
        if cached.get('uid') != user_id or cached.get('path') != path:
            return None
        # Delete before the DB lookup so inactive/deleted users cannot leave a
        # reusable ticket behind and every valid ticket remains one-shot.
        cache.delete(cache_key)
    finally:
        cache.delete(claim_key)

    return get_user_model().objects.filter(pk=user_id, is_active=True).first()


class WebSocketTicketAuthMiddleware:
    """Resolve a short-lived ``?ticket=`` into ``scope['user']``.

    Existing authenticated Django sessions continue to work when no ticket is
    supplied.  A supplied but invalid ticket deliberately overrides the
    session user with AnonymousUser so a stale/forged ticket cannot silently
    fall back to a different session identity.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'websocket':
            return await self.inner(scope, receive, send)

        from channels.db import database_sync_to_async
        from django.contrib.auth.models import AnonymousUser
        from urllib.parse import parse_qs

        query_string = scope.get('query_string', b'')
        if isinstance(query_string, bytes):
            query_string = query_string.decode('utf-8', errors='replace')
        ticket = parse_qs(query_string, keep_blank_values=True).get('ticket', [None])[0]
        if ticket is not None:
            try:
                user = await database_sync_to_async(consume_websocket_ticket)(
                    ticket,
                    scope.get('path', ''),
                )
            except Exception:  # noqa: BLE001
                logger.warning('WebSocket ticket validation failed', exc_info=True)
                user = None
            scope = dict(scope)
            scope['user'] = user or AnonymousUser()
        return await self.inner(scope, receive, send)
