import base64
import re
import uuid
from unittest.mock import Mock, patch

import pytest
import redis
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient


pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def active_user():
    return User.objects.create_user(
        username='auth_user',
        email='auth_user@example.com',
        password='StrongPass123',
        phone='13800138000',
    )


def _response_error(response):
    data = response.json()
    if isinstance(data, dict):
        detail = data.get('detail')
        if isinstance(detail, dict):
            return detail
        return data.get('error') or data.get('detail') or data.get('non_field_errors')
    return data


def test_captcha_returns_token_image_and_stores_code(api_client):
    redis_client = Mock()
    captcha_code = 'ABCD'
    captcha_token = uuid.UUID('11111111-1111-1111-1111-111111111111')

    with (
        patch('apps.users.captcha.generate_captcha_code', return_value=captcha_code),
        patch('apps.users.captcha.uuid.uuid4', return_value=captcha_token),
        patch('apps.users.captcha.get_redis', return_value=redis_client),
    ):
        response = api_client.get('/api/auth/captcha/')

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload['token'] == str(captcha_token)
    assert payload['image'].startswith('data:image/png;base64,')

    image_payload = payload['image'].split(',', 1)[1]
    assert base64.b64decode(image_payload).startswith(b'\x89PNG')
    redis_client.setex.assert_called_once_with(f'captcha:{captcha_token}', 300, captcha_code)


@pytest.mark.parametrize(
    'redis_error',
    [
        redis.ConnectionError('redis unavailable'),
        redis.AuthenticationError('invalid username-password pair or user is disabled'),
        redis.TimeoutError('redis timed out'),
    ],
)
def test_captcha_returns_service_unavailable_when_redis_fails(api_client, redis_error):
    redis_client = Mock()
    redis_client.setex.side_effect = redis_error

    with patch('apps.users.captcha.get_redis', return_value=redis_client):
        response = api_client.get('/api/auth/captcha/')

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json() == {
        'success': False,
        'error': '验证码服务暂时不可用',
    }


def test_send_register_code_rejects_invalid_captcha(api_client):
    with patch('apps.users.captcha.validate_captcha', return_value=False) as validate_captcha:
        response = api_client.post(
            '/api/auth/send-register-code/',
            {
                'phone': '13900139000',
                'captcha_token': 'captcha-token',
                'captcha_code': 'wrong-code',
            },
            format='json',
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {
        'success': False,
        'error': '图形验证码错误或已过期',
    }
    validate_captcha.assert_called_once_with('captcha-token', 'wrong-code')


def test_login_rejects_unknown_user_without_disclosing_account_state(api_client):
    response = api_client.post(
        '/api/auth/login/',
        {'username': 'missing-auth-user', 'password': 'wrong-password'},
        format='json',
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert '用户名或密码错误' in str(_response_error(response))
    assert '不存在' not in str(_response_error(response))


def test_login_returns_jwt_tokens_for_valid_credentials(api_client, active_user):
    response = api_client.post(
        '/api/auth/login/',
        {'username': active_user.username, 'password': 'StrongPass123'},
        format='json',
    )

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload['message'] == '登录成功'
    assert payload['user']['id'] == active_user.id
    assert payload['user']['username'] == active_user.username
    assert re.match(r'^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$', payload['access'])
    assert 'testhub_refresh=' in response.cookies['testhub_refresh'].OutputString()


def test_login_rejects_invalid_password(api_client, active_user):
    response = api_client.post(
        '/api/auth/login/',
        {'username': active_user.username, 'password': 'wrong-password'},
        format='json',
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert '用户名或密码错误' in str(_response_error(response))


@pytest.mark.parametrize(
    'payload',
    [
        {'username': '', 'password': 'StrongPass123'},
        {'username': 'auth_user', 'password': ''},
        {'password': 'StrongPass123'},
        {'username': 'auth_user'},
    ],
)
def test_login_rejects_missing_required_fields(api_client, active_user, payload):
    response = api_client.post('/api/auth/login/', payload, format='json')

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()


def test_login_rejects_inactive_user(api_client, active_user):
    active_user.is_active = False
    active_user.save(update_fields=['is_active'])

    response = api_client.post(
        '/api/auth/login/',
        {'username': active_user.username, 'password': 'StrongPass123'},
        format='json',
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert '用户已被禁用' in str(_response_error(response))


def test_user_detail_is_scoped_to_self_for_regular_users(api_client, active_user):
    other_user = User.objects.create_user(
        username='other_user',
        email='other@example.com',
        password='StrongPass123',
    )
    api_client.force_authenticate(user=active_user)

    response = api_client.get(f'/api/users/{other_user.id}/')

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_regular_users_cannot_create_or_delete_users(api_client, active_user):
    api_client.force_authenticate(user=active_user)

    create_response = api_client.post(
        '/api/users/',
        {'username': 'created_by_user', 'email': 'created@example.com'},
        format='json',
    )
    assert create_response.status_code == status.HTTP_403_FORBIDDEN

    other_user = User.objects.create_user(
        username='delete_target',
        email='delete@example.com',
        password='StrongPass123',
    )
    delete_response = api_client.delete(f'/api/users/{other_user.id}/')
    assert delete_response.status_code == status.HTTP_403_FORBIDDEN
    assert User.objects.filter(pk=other_user.pk).exists()


def test_session_authenticated_api_mutation_requires_csrf(active_user):
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(active_user)

    response = client.patch(
        f'/api/users/{active_user.id}/',
        {'first_name': 'blocked'},
        format='json',
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_admin_can_manage_users(api_client):
    admin = User.objects.create_superuser(
        username='test-admin',
        email='admin@example.com',
        password='StrongPass123',
    )
    target = User.objects.create_user(
        username='admin-target',
        email='target@example.com',
        password='StrongPass123',
    )
    api_client.force_authenticate(user=admin)

    response = api_client.patch(
        f'/api/users/{target.id}/',
        {'is_active': False},
        format='json',
    )

    assert response.status_code == status.HTTP_200_OK
    target.refresh_from_db()
    assert target.is_active is False


def test_refresh_uses_httponly_cookie_without_returning_refresh_token(api_client, active_user):
    login_response = api_client.post(
        '/api/auth/login/',
        {'username': active_user.username, 'password': 'StrongPass123'},
        format='json',
    )
    api_client.cookies.update(login_response.cookies)

    refresh_response = api_client.post('/api/auth/token/refresh/', {}, format='json')

    assert refresh_response.status_code == status.HTTP_200_OK
    assert 'access' in refresh_response.json()
    assert 'refresh' not in refresh_response.json()
    assert refresh_response.cookies['testhub_refresh']['httponly'] is True
