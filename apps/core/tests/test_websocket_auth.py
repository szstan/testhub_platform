import asyncio
from urllib.parse import urlencode

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.app_automation.models import AppProject, AppTestCase, AppTestExecution
from apps.core.websocket_auth import (
    WebSocketTicketAuthMiddleware,
    consume_websocket_ticket,
    issue_websocket_ticket,
)

pytestmark = pytest.mark.django_db(transaction=True)
User = get_user_model()


def test_ticket_is_path_bound_and_single_use():
    user = User.objects.create_user(username='ws-ticket-owner', password='pass')
    path = '/ws/app-automation/executions/17/'
    ticket, ttl = issue_websocket_ticket(user=user, path=path)

    assert 15 <= ttl <= 120
    assert ticket.count('.') != 2  # Django signed value, not a JWT access token
    assert consume_websocket_ticket(ticket, path + 'wrong') is None
    assert consume_websocket_ticket(ticket, path) == user
    assert consume_websocket_ticket(ticket, path) is None


def test_ticket_middleware_resolves_user_and_rejects_reuse():
    user = User.objects.create_user(username='ws-middleware-owner', password='pass')
    path = '/ws/perf-testing/executions/23/'
    ticket, _ = issue_websocket_ticket(user=user, path=path)
    captured = []

    async def inner(scope, receive, send):
        captured.append(scope)

    middleware = WebSocketTicketAuthMiddleware(inner)
    scope = {
        'type': 'websocket',
        'path': path,
        'query_string': urlencode({'ticket': ticket}).encode(),
        'user': AnonymousUser(),
    }
    asyncio.run(middleware(scope, None, None))

    assert captured[0]['user'] == user

    captured.clear()
    asyncio.run(middleware(scope, None, None))
    assert isinstance(captured[0]['user'], AnonymousUser)


def test_ticket_middleware_invalid_ticket_overrides_session_user():
    user = User.objects.create_user(username='ws-session-user', password='pass')
    path = '/ws/app-automation/executions/24/'
    captured = []

    async def inner(scope, receive, send):
        captured.append(scope)

    middleware = WebSocketTicketAuthMiddleware(inner)
    scope = {
        'type': 'websocket',
        'path': path,
        'query_string': b'ticket=',
        'user': user,
    }
    asyncio.run(middleware(scope, None, None))

    assert isinstance(captured[0]['user'], AnonymousUser)


def test_ticket_middleware_preserves_session_without_ticket():
    user = User.objects.create_user(username='ws-session-preserved', password='pass')
    captured = []

    async def inner(scope, receive, send):
        captured.append(scope)

    middleware = WebSocketTicketAuthMiddleware(inner)
    scope = {'type': 'websocket', 'path': '/ws/perf-testing/executions/25/', 'user': user}
    asyncio.run(middleware(scope, None, None))

    assert captured[0]['user'] == user


def test_websocket_ticket_endpoint_requires_execution_access():
    owner = User.objects.create_user(username='ws-endpoint-owner', password='pass')
    outsider = User.objects.create_user(username='ws-endpoint-outsider', password='pass')
    project = AppProject.objects.create(name='ws-project', owner=owner)
    case = AppTestCase.objects.create(project=project, name='ws-case', created_by=owner)
    execution = AppTestExecution.objects.create(test_case=case, user=owner)
    client = APIClient()

    client.force_authenticate(user=outsider)
    denied = client.post(
        '/api/auth/websocket-ticket/',
        {'channel': 'app-automation', 'execution_id': execution.id},
        format='json',
    )
    assert denied.status_code == 404

    client.force_authenticate(user=owner)
    response = client.post(
        '/api/auth/websocket-ticket/',
        {'channel': 'app-automation', 'execution_id': execution.id},
        format='json',
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == f'/ws/app-automation/executions/{execution.id}/'
    assert payload['expires_in_seconds'] <= 120
    assert payload['ticket']
    assert 'access' not in payload

    cache.clear()
