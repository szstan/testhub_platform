import base64
import re
import uuid
from contextlib import contextmanager
from threading import Barrier, Event, Thread
from unittest.mock import Mock, patch

import pytest
import redis
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from rest_framework import serializers, status
from rest_framework.test import APIClient

from apps.users.models import UserPhoneWriteLock
from apps.users.registration import (
    DuplicatePhoneError,
    create_user_with_unique_phone,
    phone_write_lock_id,
    serialized_phone_write,
)
from apps.users.serializers import (
    UserCreateSerializer,
    UserSelfUpdateSerializer,
    UserSerializer,
)


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


def test_registration_rechecks_duplicate_phone_after_serializer_validation():
    phone = '13900139000'
    first = UserCreateSerializer(data={
        'username': 'racing-phone-first',
        'email': 'first@example.com',
        'password': 'StrongPass123',
        'password_confirm': 'StrongPass123',
        'phone': phone,
    })
    second = UserCreateSerializer(data={
        'username': 'racing-phone-second',
        'email': 'second@example.com',
        'password': 'StrongPass123',
        'password_confirm': 'StrongPass123',
        'phone': phone,
    })
    first.is_valid(raise_exception=True)
    second.is_valid(raise_exception=True)

    first.save()
    with pytest.raises(serializers.ValidationError):
        second.save()

    assert User.objects.filter(phone=phone).count() == 1


def test_phone_registration_rechecks_duplicate_phone_after_serializer_validation(api_client):
    phone = '13900139000'

    def create_racing_user(**user_data):
        User.objects.create_user(
            username='racing-api-registration-owner',
            password='StrongPass123',
            phone=user_data['phone'],
        )
        return create_user_with_unique_phone(**user_data)

    with patch('apps.users.serializers.create_user_with_unique_phone', side_effect=create_racing_user):
        response = api_client.post(
            '/api/auth/register/',
            {
                'username': 'racing-api-registration-loser',
                'email': 'racing-api@example.com',
                'password': 'StrongPass123',
                'password_confirm': 'StrongPass123',
                'phone': phone,
            },
            format='json',
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert User.objects.filter(phone=phone).count() == 1
    assert not User.objects.filter(username='racing-api-registration-loser').exists()


def test_legacy_registration_route_rechecks_duplicate_phone_after_sms_validation(api_client):
    phone = '13900139001'

    def claim_phone_before_create(*args, **kwargs):
        User.objects.create_user(
            username='racing-legacy-registration-owner',
            password='StrongPass123',
            phone=phone,
        )
        return True, ''

    with patch('apps.users.sms.validate_verify_code', side_effect=claim_phone_before_create):
        response = api_client.post(
            '/api/auth/test-register/',
            {
                'username': 'racing-legacy-registration-loser',
                'email': 'racing-legacy@example.com',
                'password': 'StrongPass123',
                'phone': phone,
                'verify_code': '123456',
                'verify_code_token': 'token',
            },
            format='json',
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert User.objects.filter(phone=phone).count() == 1
    assert not User.objects.filter(username='racing-legacy-registration-loser').exists()


@pytest.mark.parametrize('serializer_class', [UserSerializer, UserSelfUpdateSerializer])
def test_stale_partial_profile_updates_do_not_restore_re_registered_phone(serializer_class):
    old_phone = '13900139000'
    new_phone = '13900139002'
    user = User.objects.create_user(username='stale-profile-owner', phone=old_phone)
    stale_instance = User.objects.get(pk=user.pk)
    serializer = serializer_class(
        stale_instance,
        data={'first_name': f'Updated by {serializer_class.__name__}'},
        partial=True,
    )
    assert serializer.is_valid(), serializer.errors

    current = User.objects.get(pk=user.pk)
    phone_change = UserSerializer(current, data={'phone': new_phone}, partial=True)
    assert phone_change.is_valid(), phone_change.errors
    phone_change.save()
    create_user_with_unique_phone(
        username=f'old-phone-registrant-{serializer_class.__name__}',
        password='StrongPass123',
        phone=old_phone,
    )

    serializer.save()
    persisted = User.objects.get(pk=user.pk)
    assert persisted.phone == new_phone
    assert persisted.first_name == f'Updated by {serializer_class.__name__}'
    assert User.objects.filter(phone=old_phone).count() == 1


@pytest.mark.parametrize('stale_phone', ['13900139000', '13900139006'])
def test_stale_explicit_phone_update_does_not_restore_or_replace_newer_phone(stale_phone):
    old_phone = '13900139000'
    new_phone = '13900139002'
    user = User.objects.create_user(username='stale-phone-owner', phone=old_phone)
    stale = UserSerializer(User.objects.get(pk=user.pk), data={'phone': stale_phone}, partial=True)
    assert stale.is_valid(), stale.errors

    latest = UserSerializer(User.objects.get(pk=user.pk), data={'phone': new_phone}, partial=True)
    assert latest.is_valid(), latest.errors
    latest.save()
    create_user_with_unique_phone(username='released-stale-phone', phone=old_phone)

    with pytest.raises(serializers.ValidationError):
        stale.save()
    user.refresh_from_db()
    assert user.phone == new_phone
    assert User.objects.filter(phone=old_phone).count() == 1


def test_phone_change_and_clear_release_numbers_for_registration():
    old_phone = '13900139003'
    new_phone = '13900139004'
    user = User.objects.create_user(username='phone-change-owner', phone=old_phone)

    change = UserSerializer(user, data={'phone': new_phone}, partial=True)
    assert change.is_valid(), change.errors
    change.save()

    create_user_with_unique_phone(
        username='released-old-phone',
        password='StrongPass123',
        phone=old_phone,
    )
    clear = UserSerializer(change.instance, data={'phone': ''}, partial=True)
    assert clear.is_valid(), clear.errors
    clear.save()
    create_user_with_unique_phone(
        username='released-new-phone',
        password='StrongPass123',
        phone=new_phone,
    )

    user.refresh_from_db()
    assert user.phone == ''
    assert User.objects.filter(phone=old_phone).count() == 1
    assert User.objects.filter(phone=new_phone).count() == 1


def test_registration_hashes_password_before_acquiring_phone_lock():
    from django.contrib.auth.hashers import make_password as real_make_password
    from apps.users import registration

    events = []
    real_serialized_write = registration.serialized_phone_write

    @contextmanager
    def tracked_phone_write(*phones):
        events.append('lock')
        with real_serialized_write(*phones):
            yield

    def tracked_make_password(*args, **kwargs):
        events.append('hash')
        return real_make_password(*args, **kwargs)

    with (
        patch('apps.users.registration.serialized_phone_write', tracked_phone_write),
        patch('apps.users.registration.make_password', tracked_make_password),
    ):
        create_user_with_unique_phone(
            username='hash-before-phone-lock',
            password='StrongPass123',
            phone='13900139005',
        )

    assert events == ['hash', 'lock']


def test_phone_write_stripes_keep_distinct_phone_keys_independent():
    first_phone = '13000000001'
    second_phone = '13000000002'
    first_lock = phone_write_lock_id(first_phone)
    second_lock = phone_write_lock_id(second_phone)
    assert first_lock != second_lock

    with serialized_phone_write(first_phone):
        with serialized_phone_write(second_phone):
            locked_rows = set(
                UserPhoneWriteLock.objects.filter(pk__in=(first_lock, second_lock))
                .values_list('pk', flat=True)
            )
    assert locked_rows == {first_lock, second_lock}


@pytest.mark.django_db(transaction=True)
def test_mysql_phone_write_stripes_do_not_block_distinct_numbers():
    if connection.vendor != 'mysql':
        pytest.skip('SQLite does not implement select_for_update row locks')

    first_phone = '13000000001'
    second_phone = '13000000002'
    assert phone_write_lock_id(first_phone) != phone_write_lock_id(second_phone)

    first_entered = Event()
    second_entered = Event()
    release_first = Event()
    errors = []

    def hold_first_phone_lock():
        close_old_connections()
        try:
            with serialized_phone_write(first_phone):
                first_entered.set()
                if not release_first.wait(timeout=10):
                    errors.append(TimeoutError('first phone lock was not released'))
        except Exception as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    def acquire_second_phone_lock():
        close_old_connections()
        try:
            with serialized_phone_write(second_phone):
                second_entered.set()
        except Exception as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    first_thread = Thread(target=hold_first_phone_lock)
    second_thread = Thread(target=acquire_second_phone_lock)
    first_thread.start()
    try:
        assert first_entered.wait(timeout=5)
        second_thread.start()
        assert second_entered.wait(timeout=3)
    finally:
        release_first.set()
        first_thread.join(timeout=10)
        if second_thread.ident is not None:
            second_thread.join(timeout=10)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert not errors


@pytest.mark.django_db(transaction=True)
def test_mysql_simultaneous_registrations_with_same_phone_allow_only_one():
    if connection.vendor != 'mysql':
        pytest.skip('SQLite does not implement select_for_update row locks')

    phone = '13000000003'
    start = Barrier(2, timeout=10)
    results = []
    errors = []

    def register(username):
        close_old_connections()
        try:
            start.wait()
            create_user_with_unique_phone(username=username, password='StrongPass123', phone=phone)
            results.append('created')
        except DuplicatePhoneError:
            results.append('duplicate')
        except Exception as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [Thread(target=register, args=(f'same-phone-racer-{number}',)) for number in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    assert sorted(results) == ['created', 'duplicate']
    assert User.objects.filter(phone=phone).count() == 1


def test_public_registration_rejects_duplicate_phone_and_allows_legacy_blank_values(api_client, active_user):
    User.objects.create_user(username='legacy-null-phone', password='StrongPass123', phone=None)
    User.objects.create_user(username='legacy-blank-phone', password='StrongPass123', phone='')

    duplicate = api_client.post(
        '/api/auth/register/',
        {
            'username': 'duplicate-phone-user',
            'email': 'duplicate@example.com',
            'password': 'StrongPass123',
            'password_confirm': 'StrongPass123',
            'phone': active_user.phone,
        },
        format='json',
    )
    assert duplicate.status_code == status.HTTP_400_BAD_REQUEST
    assert '该手机号已被注册' in str(duplicate.json())
    assert User.objects.filter(phone=active_user.phone).count() == 1

    blank_phone = api_client.post(
        '/api/auth/register/',
        {
            'username': 'blank-phone-user',
            'email': 'blank@example.com',
            'password': 'StrongPass123',
            'password_confirm': 'StrongPass123',
            'phone': '',
        },
        format='json',
    )
    assert blank_phone.status_code == status.HTTP_201_CREATED, blank_phone.json()
    assert User.objects.filter(phone='').count() == 2


def test_admin_user_update_allows_unchanged_legacy_duplicate_phone():
    phone = '13900139000'
    user = User.objects.create_user(username='legacy-duplicate-admin-one', phone=phone)
    User.objects.create_user(username='legacy-duplicate-admin-two', phone=phone)

    serializer = UserSerializer(user, data={'phone': phone, 'first_name': 'Updated'}, partial=True)
    assert serializer.is_valid(), serializer.errors
    serializer.save()

    assert User.objects.filter(phone=phone).count() == 2
    user.refresh_from_db()
    assert user.first_name == 'Updated'


def test_legacy_duplicate_phone_is_rejected_by_sms_code_and_login(api_client):
    phone = '13900139000'
    User.objects.create_user(username='legacy-phone-one', password='StrongPass123', phone=phone)
    User.objects.create_user(username='legacy-phone-two', password='StrongPass123', phone=phone)

    with patch('apps.users.views.send_register_verify_code') as send_code:
        send_response = api_client.post(
            '/api/auth/send-register-code/',
            {
                'phone': phone,
                'captcha_token': 'captcha-token',
                'captcha_code': 'ABCD',
                'mode': 'login',
            },
            format='json',
        )
    assert send_response.status_code == status.HTTP_409_CONFLICT
    assert '多个账号' in send_response.json()['error']
    send_code.assert_not_called()

    with patch('apps.users.views.validate_verify_code', return_value=(True, '')):
        login_response = api_client.post(
            '/api/auth/sms-login/',
            {'phone': phone, 'verify_code_token': 'token', 'verify_code': '123456'},
            format='json',
        )
    assert login_response.status_code == status.HTTP_409_CONFLICT
    assert '多个账号' in login_response.json()['error']
    assert 'access' not in login_response.json()


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
    assert payload['user']['is_admin'] is False
    assert 'is_staff' not in payload['user']
    assert re.match(r'^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$', payload['access'])
    assert 'testhub_refresh=' in response.cookies['testhub_refresh'].OutputString()

    active_user.is_staff = True
    active_user.save(update_fields=['is_staff'])
    api_client.force_authenticate(user=active_user)
    admin_response = api_client.get('/api/auth/profile/')
    assert admin_response.status_code == status.HTTP_200_OK
    assert admin_response.json()['is_admin'] is True


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
    assert '用户名或密码错误' in str(_response_error(response))


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


def test_refresh_clears_cookie_when_refresh_user_no_longer_exists(api_client, active_user):
    login_response = api_client.post(
        '/api/auth/login/',
        {'username': active_user.username, 'password': 'StrongPass123'},
        format='json',
    )
    api_client.cookies.update(login_response.cookies)
    active_user.delete()

    refresh_response = api_client.post('/api/auth/token/refresh/', {}, format='json')

    assert refresh_response.status_code == status.HTTP_401_UNAUTHORIZED
    assert refresh_response.json()['detail'] == 'refresh token invalid'
    assert refresh_response.cookies['testhub_refresh']['max-age'] == 0


@pytest.mark.parametrize('failure', ['missing', 'malformed', 'expired', 'blacklisted', 'inactive'])
def test_refresh_failures_are_401_and_always_clear_cookie(api_client, active_user, failure):
    from datetime import timedelta
    from django.utils import timezone
    from rest_framework_simplejwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(active_user)
    if failure == 'expired':
        refresh.set_exp(from_time=timezone.now(), lifetime=timedelta(seconds=-1))
    elif failure == 'blacklisted':
        refresh.blacklist()
    elif failure == 'inactive':
        active_user.is_active = False
        active_user.save(update_fields=['is_active'])
    if failure != 'missing':
        api_client.cookies['testhub_refresh'] = 'invalid.jwt' if failure == 'malformed' else str(refresh)

    response = api_client.post('/api/auth/token/refresh/', {}, format='json')

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {'detail': 'refresh token invalid'}
    assert response.cookies['testhub_refresh']['max-age'] == 0
    assert response.cookies['testhub_refresh']['path'] == '/api/auth/'


@pytest.mark.parametrize('authorization', [None, 'Bearer invalid.jwt', 'expired', 'inactive'])
def test_logout_clears_cookie_even_without_valid_access(api_client, active_user, authorization):
    from datetime import timedelta
    from django.utils import timezone
    from rest_framework_simplejwt.exceptions import TokenError
    from rest_framework_simplejwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(active_user)
    raw_refresh = str(refresh)
    api_client.cookies['testhub_refresh'] = raw_refresh
    if authorization in {'expired', 'inactive'}:
        access = refresh.access_token
        if authorization == 'expired':
            access.set_exp(from_time=timezone.now(), lifetime=timedelta(seconds=-1))
        else:
            active_user.is_active = False
            active_user.save(update_fields=['is_active'])
        authorization = f'Bearer {access}'
    if authorization:
        api_client.credentials(HTTP_AUTHORIZATION=authorization)

    response = api_client.post('/api/auth/logout/', {}, format='json')

    assert response.status_code == status.HTTP_200_OK
    assert response.cookies['testhub_refresh']['max-age'] == 0
    with pytest.raises(TokenError):
        RefreshToken(raw_refresh)
    # Logout is idempotent after the browser has already removed the cookie.
    assert api_client.post('/api/auth/logout/', {}, format='json').status_code == status.HTTP_200_OK


def test_logout_with_invalid_cookie_does_not_require_login(api_client):
    api_client.cookies['testhub_refresh'] = 'invalid.jwt'
    response = api_client.post('/api/auth/logout/', {}, format='json')
    assert response.status_code == status.HTTP_200_OK
    assert response.cookies['testhub_refresh']['max-age'] == 0
    assert api_client.get('/api/auth/profile/').status_code == status.HTTP_401_UNAUTHORIZED
