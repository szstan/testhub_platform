from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from apps.assistant.models import AssistantMessage, AssistantSession, ChatMessage, DifyConfig
from apps.users.models import User


class _DifyResponse:
    status_code = 200

    @staticmethod
    def json():
        return {
            'answer': 'reply',
            'conversation_id': 'conversation-1',
            'message_id': 'message-1',
        }


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    cache.clear()


@pytest.mark.django_db
@override_settings(DIFY_ALLOWED_HOSTS='dify.example.test')
def test_send_message_rejects_oversized_content_before_storage_or_dify():
    user = User.objects.create_user(username='assistant-long-message')
    session = AssistantSession.objects.create(user=user, session_id='long-message-session')
    DifyConfig.objects.create(api_url='https://dify.example.test/v1', api_key='test-key')

    with patch('apps.assistant.views.guarded_post') as outbound:
        response = _client(user).post(
            '/api/assistant/chat/send_message/',
            {'session_id': session.session_id, 'message': 'x' * 16001},
            format='json',
        )

    assert response.status_code == 400
    assert 'message' in response.data
    assert ChatMessage.objects.count() == 0
    outbound.assert_not_called()


@pytest.mark.django_db
@override_settings(
    ASSISTANT_MESSAGE_RATE='1/minute',
    DIFY_ALLOWED_HOSTS='dify.example.test',
)
def test_send_message_rate_limit_runs_before_dify_and_storage():
    user = User.objects.create_user(username='assistant-chat-throttle')
    session = AssistantSession.objects.create(user=user, session_id='throttled-chat-session')
    DifyConfig.objects.create(api_url='https://dify.example.test/v1', api_key='test-key')
    client = _client(user)
    payload = {'session_id': session.session_id, 'message': 'hello'}

    with patch('apps.assistant.views.guarded_post', return_value=_DifyResponse()) as outbound:
        first = client.post('/api/assistant/chat/send_message/', payload, format='json')
        second = client.post('/api/assistant/chat/send_message/', payload, format='json')

    assert first.status_code == 200
    assert second.status_code == 429
    assert ChatMessage.objects.filter(role='user').count() == 1
    assert ChatMessage.objects.filter(role='assistant').count() == 1
    assert outbound.call_count == 1


@pytest.mark.django_db
@override_settings(ASSISTANT_MESSAGE_RATE='1/minute')
def test_add_message_rate_limit_runs_before_message_storage():
    user = User.objects.create_user(username='assistant-add-message-throttle')
    session = AssistantSession.objects.create(user=user, session_id='throttled-add-session')
    client = _client(user)
    url = f'/api/assistant/sessions/{session.pk}/add_message/'
    payload = {'message_type': 'user', 'content': 'hello'}

    first = client.post(url, payload, format='json')
    second = client.post(url, payload, format='json')

    assert first.status_code == 201
    assert second.status_code == 429
    assert AssistantMessage.objects.filter(session=session).count() == 1


@pytest.mark.django_db
def test_add_message_rejects_oversized_content_before_storage():
    user = User.objects.create_user(username='assistant-add-long-message')
    session = AssistantSession.objects.create(user=user, session_id='long-add-session')

    response = _client(user).post(
        f'/api/assistant/sessions/{session.pk}/add_message/',
        {'message_type': 'user', 'content': 'x' * 16001},
        format='json',
    )

    assert response.status_code == 400
    assert 'content' in response.data
    assert AssistantMessage.objects.filter(session=session).count() == 0
