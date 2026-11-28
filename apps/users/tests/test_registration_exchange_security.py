import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock, Thread
from unittest.mock import patch

import pytest
import redis
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import close_old_connections
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient

from apps.users import sms, views

User = get_user_model()


class AtomicRedis:
    """Small Redis test double for the atomic operations used by these flows."""

    def __init__(self):
        self.values = {}
        self.lock = Lock()

    def setex(self, key, ttl, value):
        with self.lock:
            self.values[key] = value
        return True

    def set(self, key, value, *, ex, nx):
        with self.lock:
            if nx and key in self.values:
                return None
            self.values[key] = value
        return True

    def getdel(self, key):
        with self.lock:
            return self.values.pop(key, None)

    def ttl(self, key):
        with self.lock:
            return 60 if key in self.values else -2

    def delete(self, *keys):
        with self.lock:
            return sum(self.values.pop(key, None) is not None for key in keys)

    def eval(self, script, numkeys, *args):
        with self.lock:
            if 'local attempts = redis.call' in script:
                code_key, phone_key, attempts_key = args[:3]
                expected_code, expected_phone, _ttl, max_attempts = args[3:]
                stored_code = self.values.get(code_key)
                if stored_code is None:
                    return -1
                attempts = int(self.values.get(attempts_key, 0)) + 1
                self.values[attempts_key] = attempts
                if attempts > int(max_attempts):
                    self.values.pop(code_key, None)
                    self.values.pop(phone_key, None)
                    self.values.pop(attempts_key, None)
                    return -4
                stored_phone = self.values.get(phone_key)
                if stored_phone is None:
                    return -1
                if str(stored_code) != str(expected_code):
                    return -2
                if str(stored_phone) != str(expected_phone):
                    return -3
                self.values.pop(code_key, None)
                self.values.pop(phone_key, None)
                return 1
            if 'local value = redis.call' in script:
                return self.values.pop(args[0], None)
        raise AssertionError('Unexpected Redis Lua script')


def _register_payload(username, phone, token=None, code=None):
    payload = {
        'username': username,
        'email': f'{username}@example.com',
        'password': 'StrongPass123',
        'password_confirm': 'StrongPass123',
        'phone': phone,
    }
    if token is not None:
        payload['verify_code_token'] = token
    if code is not None:
        payload['verify_code'] = code
    return payload


def _redeem_cache_key(ip):
    digest = hashlib.sha256(ip.encode('utf-8')).hexdigest()
    return f'auth:exchange:redeem:ip:{digest}'


@pytest.mark.django_db
def test_public_registration_requires_sms_proof_and_accepts_phone_bound_proof():
    client = APIClient()
    redis_store = AtomicRedis()
    phone = '13900139000'
    token = 'registration-proof-token'

    with patch('apps.users.sms.get_redis', return_value=redis_store):
        missing = client.post('/api/auth/register/', _register_payload('missing-proof', phone), format='json')
        assert missing.status_code == status.HTTP_400_BAD_REQUEST
        assert not User.objects.filter(username='missing-proof').exists()
        assert redis_store.values == {}

        redis_store.setex(f'sms:verify_code:{token}', 300, '123456')
        redis_store.setex(f'sms:phone:{token}', 300, '13900139001')
        mismatched = client.post(
            '/api/auth/register/',
            _register_payload('mismatched-proof', phone, token, '123456'),
            format='json',
        )
        assert mismatched.status_code == status.HTTP_400_BAD_REQUEST
        assert not User.objects.filter(username='mismatched-proof').exists()

        redis_store.setex(f'sms:verify_code:{token}', 300, '123456')
        redis_store.setex(f'sms:phone:{token}', 300, phone)
        registered = client.post(
            '/api/auth/register/',
            _register_payload('verified-phone-user', phone, token, '123456'),
            format='json',
        )
        assert registered.status_code == status.HTTP_201_CREATED, registered.json()
        assert User.objects.get(username='verified-phone-user').phone == phone

        replay = client.post(
            '/api/auth/register/',
            _register_payload('replayed-proof', phone, token, '123456'),
            format='json',
        )
        assert replay.status_code == status.HTTP_400_BAD_REQUEST
        assert not User.objects.filter(username='replayed-proof').exists()


@pytest.mark.django_db
def test_legacy_registration_requires_and_consumes_sms_proof():
    client = APIClient()
    redis_store = AtomicRedis()
    phone = '13900139002'
    token = 'legacy-registration-proof'

    with patch('apps.users.sms.get_redis', return_value=redis_store):
        missing = client.post(
            '/api/auth/test-register/',
            _register_payload('legacy-missing-proof', phone),
            format='json',
        )
        assert missing.status_code == status.HTTP_400_BAD_REQUEST
        assert not User.objects.filter(username='legacy-missing-proof').exists()

        redis_store.setex(f'sms:verify_code:{token}', 300, '654321')
        redis_store.setex(f'sms:phone:{token}', 300, phone)
        registered = client.post(
            '/api/auth/test-register/',
            _register_payload('legacy-verified-user', phone, token, '654321'),
            format='json',
        )

    assert registered.status_code == status.HTTP_200_OK, registered.json()
    assert registered.json()['success'] is True
    assert User.objects.get(username='legacy-verified-user').phone == phone


@pytest.mark.django_db
def test_sms_code_endpoint_captcha_failure_is_same_for_registered_and_unknown_numbers():
    User.objects.create_user(username='known-sms-number', phone='13900139003')
    client = APIClient()
    request_data = {
        'captcha_token': 'captcha-token',
        'captcha_code': 'ABCD',
        'mode': 'register',
    }

    with patch('apps.users.captcha.validate_captcha', return_value=False) as validate_captcha:
        registered = client.post(
            '/api/auth/send-register-code/',
            {**request_data, 'phone': '13900139003'},
            format='json',
        )
        unknown = client.post(
            '/api/auth/send-register-code/',
            {**request_data, 'phone': '13900139004'},
            format='json',
        )

    assert registered.status_code == unknown.status_code == status.HTTP_400_BAD_REQUEST
    assert registered.json() == unknown.json() == {
        'success': False,
        'error': '图形验证码错误或已过期',
    }
    assert validate_captcha.call_count == 2


def test_sms_proof_concurrent_replay_consumes_only_once():
    redis_store = AtomicRedis()
    phone = '13900139005'
    token = 'one-time-proof-token'
    redis_store.setex(f'sms:verify_code:{token}', 300, '111222')
    redis_store.setex(f'sms:phone:{token}', 300, phone)
    start = Barrier(2, timeout=5)
    results = []

    def validate():
        start.wait()
        results.append(sms.validate_verify_code(phone, token, '111222'))

    with patch('apps.users.sms.get_redis', return_value=redis_store):
        threads = [Thread(target=validate) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(valid for valid, _error in results) == 1


def test_sms_send_cooldown_reservation_is_atomic():
    redis_store = AtomicRedis()
    phone = '13900139006'
    start = Barrier(2, timeout=5)
    results = []

    def reserve():
        start.wait()
        results.append(sms._reserve_rate_limit(phone))

    with patch('apps.users.sms.get_redis', return_value=redis_store):
        threads = [Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(allowed for allowed, _remaining in results) == 1


@pytest.mark.django_db
def test_exchange_create_uses_high_entropy_code_and_redeem_is_one_shot():
    user = User.objects.create_user(username='exchange-owner', password='StrongPass123')
    client = APIClient()
    client.force_authenticate(user=user)
    redis_store = AtomicRedis()
    create_key = f'auth:exchange:create:user:{user.pk}'
    cache.delete(create_key)

    try:
        with patch('apps.users.views.get_redis', return_value=redis_store):
            created = client.post('/api/auth/exchange-token/', {'action': 'create'}, format='json')
            assert created.status_code == status.HTTP_200_OK, created.json()
            code = created.json()['code']
            assert len(code) == 43
            assert redis_store.values[f'exchange:{code}'] == user.pk

            redeemer = APIClient()
            first = redeemer.post(
                '/api/auth/exchange-token/',
                {'action': 'redeem', 'code': code},
                format='json',
                REMOTE_ADDR='198.51.100.21',
            )
            second = redeemer.post(
                '/api/auth/exchange-token/',
                {'action': 'redeem', 'code': code},
                format='json',
                REMOTE_ADDR='198.51.100.21',
            )

        assert first.status_code == status.HTTP_200_OK
        assert second.status_code == status.HTTP_400_BAD_REQUEST
        assert 'access' in first.json()
        assert f'exchange:{code}' not in redis_store.values
    finally:
        cache.delete(create_key)
        cache.delete(_redeem_cache_key('198.51.100.21'))


@pytest.mark.django_db(transaction=True)
def test_concurrent_exchange_redeem_returns_jwt_to_only_one_request():
    user = User.objects.create_user(username='exchange-race-owner', password='StrongPass123')
    redis_store = AtomicRedis()
    code = 'concurrent-one-shot-code'
    redis_store.setex(f'exchange:{code}', 60, user.pk)
    ip = '198.51.100.22'
    cache.delete(_redeem_cache_key(ip))
    start = Barrier(2, timeout=10)

    def redeem():
        close_old_connections()
        try:
            start.wait()
            response = APIClient().post(
                '/api/auth/exchange-token/',
                {'action': 'redeem', 'code': code},
                format='json',
                REMOTE_ADDR=ip,
            )
            return response.status_code
        finally:
            close_old_connections()

    try:
        with patch('apps.users.views.get_redis', return_value=redis_store):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(redeem) for _ in range(2)]
                results = [future.result(timeout=15) for future in futures]
        assert sorted(results) == [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST]
    finally:
        cache.delete(_redeem_cache_key(ip))


@pytest.mark.django_db
@override_settings(EXCHANGE_TOKEN_REDEEM_RATE_LIMIT=2, EXCHANGE_TOKEN_RATE_WINDOW_SECONDS=300)
def test_exchange_redeem_is_rate_limited_by_client_ip():
    client = APIClient()
    redis_store = AtomicRedis()
    ip = '198.51.100.23'
    cache_key = _redeem_cache_key(ip)
    cache.delete(cache_key)

    try:
        with patch('apps.users.views.get_redis', return_value=redis_store):
            responses = [
                client.post(
                    '/api/auth/exchange-token/',
                    {'action': 'redeem', 'code': 'unknown'},
                    format='json',
                    REMOTE_ADDR=ip,
                )
                for _ in range(3)
            ]
        assert [response.status_code for response in responses] == [
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_429_TOO_MANY_REQUESTS,
        ]
    finally:
        cache.delete(cache_key)


@pytest.mark.django_db
@override_settings(EXCHANGE_TOKEN_CREATE_RATE_LIMIT=1, EXCHANGE_TOKEN_RATE_WINDOW_SECONDS=300)
def test_exchange_create_is_rate_limited_per_authenticated_user():
    user = User.objects.create_user(username='exchange-throttle-owner', password='StrongPass123')
    client = APIClient()
    client.force_authenticate(user=user)
    redis_store = AtomicRedis()
    cache_key = f'auth:exchange:create:user:{user.pk}'
    cache.delete(cache_key)

    try:
        with patch('apps.users.views.get_redis', return_value=redis_store):
            first = client.post('/api/auth/exchange-token/', {'action': 'create'}, format='json')
            second = client.post('/api/auth/exchange-token/', {'action': 'create'}, format='json')
        assert first.status_code == status.HTTP_200_OK
        assert second.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    finally:
        cache.delete(cache_key)


def test_exchange_code_parser_rejects_unbounded_values_before_redis_access():
    client = APIClient()
    with patch('apps.users.views.get_redis') as get_redis:
        response = client.post(
            '/api/auth/exchange-token/',
            {'action': 'redeem', 'code': 'x' * 129},
            format='json',
        )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    get_redis.assert_not_called()


def test_exchange_getdel_uses_lua_when_redis_server_is_too_old():
    class OldRedis:
        def getdel(self, key):
            raise redis.ResponseError("unknown command 'GETDEL'")

        def eval(self, script, numkeys, key):
            assert numkeys == 1
            assert 'redis.call(\'DEL\', KEYS[1])' in script
            assert key == 'exchange:legacy-code'
            return b'42'

    assert views._consume_redis_key(OldRedis(), 'exchange:legacy-code') == b'42'
