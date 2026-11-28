import hashlib
import re
import secrets

import redis
from django.conf import settings
from django.contrib.auth import logout
from django.core.cache import cache
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import generics, permissions, status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.exceptions import AuthenticationFailed, ValidationError
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError

# JWT 相关导入
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from .captcha import create_captcha
from .models import User
from .redis_client import get_redis
from .serializers import (
    LoginSerializer,
    UserCreateSerializer,
    UserDirectorySerializer,
    UserSelfUpdateSerializer,
    UserSerializer,
    user_session_data,
)
from .sms import send_register_verify_code, validate_verify_code

REFRESH_COOKIE_NAME = 'testhub_refresh'


def _positive_setting(name, default, maximum):
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, 1), maximum)


def _rate_limit_allows(key, limit, window):
    if cache.add(key, 1, timeout=window):
        return True
    try:
        attempts = cache.incr(key)
    except ValueError:
        if cache.add(key, 1, timeout=window):
            return True
        attempts = cache.incr(key)
    return attempts <= limit


_EXCHANGE_GETDEL_LUA = """
local value = redis.call('GET', KEYS[1])
if value then redis.call('DEL', KEYS[1]) end
return value
"""


def _consume_redis_key(redis_client, key):
    getdel = getattr(redis_client, 'getdel', None)
    if callable(getdel):
        try:
            return getdel(key)
        except redis.ResponseError as exc:
            if 'unknown command' not in str(exc).lower():
                raise
    return redis_client.eval(_EXCHANGE_GETDEL_LUA, 1, key)


def _set_refresh_cookie(response, refresh_token):
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=7 * 24 * 60 * 60,
        httponly=True,
        secure=settings.APP_USE_HTTPS,
        samesite='Lax',
        path='/api/auth/',
    )


def _clear_refresh_cookie(response):
    response.delete_cookie(REFRESH_COOKIE_NAME, path='/api/auth/')


class CookieTokenRefreshView(TokenRefreshView):
    """Refresh JWT from an HttpOnly cookie without exposing it to JavaScript."""

    def post(self, request, *args, **kwargs):
        data = request.data.copy()
        if not data.get('refresh'):
            data['refresh'] = request.COOKIES.get(REFRESH_COOKIE_NAME, '')
        if not data.get('refresh'):
            response = Response({'detail': 'refresh token invalid'}, status=status.HTTP_401_UNAUTHORIZED)
            _clear_refresh_cookie(response)
            return response
        serializer = self.get_serializer(data=data)
        try:
            serializer.is_valid(raise_exception=True)
        except (User.DoesNotExist, TokenError, AuthenticationFailed, ValidationError):
            response = Response({'detail': 'refresh token invalid'}, status=status.HTTP_401_UNAUTHORIZED)
            _clear_refresh_cookie(response)
            return response
        payload = dict(serializer.validated_data)
        refresh = payload.pop('refresh', None)
        response = Response(payload, status=status.HTTP_200_OK)
        if refresh:
            _set_refresh_cookie(response, refresh)
        return response



@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_current_user(request):
    return Response(user_session_data(request.user))

@method_decorator(csrf_exempt, name='dispatch')
class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all().order_by('username')
    serializer_class = UserCreateSerializer
    permission_classes = [permissions.AllowAny]
    
    def create(self, request, *args, **kwargs):
        phone = request.data.get('phone')
        if phone not in (None, ''):
            if not isinstance(phone, str) or len(phone) > 11:
                return Response({'error': '手机号格式不正确'}, status=status.HTTP_400_BAD_REQUEST)
            phone = phone.strip()
            if phone:
                if not re.fullmatch(r'1[3-9]\d{9}', phone):
                    return Response({'error': '手机号格式不正确'}, status=status.HTTP_400_BAD_REQUEST)
                verify_code = request.data.get('verify_code')
                verify_code_token = request.data.get('verify_code_token')
                if (
                    not isinstance(verify_code, str)
                    or not verify_code
                    or len(verify_code) > 6
                    or not isinstance(verify_code_token, str)
                    or not verify_code_token
                    or len(verify_code_token) > 128
                ):
                    return Response({'error': '请输入短信验证码'}, status=status.HTTP_400_BAD_REQUEST)
                valid, error = validate_verify_code(
                    phone, verify_code_token.strip(), verify_code.strip()
                )
                if not valid:
                    return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response({
            'user': user_session_data(user),
        }, status=status.HTTP_201_CREATED)

@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def login_view(request):
    username = str(request.data.get('username') or '')[:150]
    ip = request.META.get('REMOTE_ADDR', 'unknown')
    throttle_key = f'auth:login-fail:{ip}:{username.lower()}'
    attempts = cache.get(throttle_key, 0)
    if attempts >= 10:
        return Response({'error': '登录尝试过于频繁，请稍后再试'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    serializer = LoginSerializer(data=request.data)
    if not serializer.is_valid():
        cache.set(throttle_key, attempts + 1, timeout=300)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    cache.delete(throttle_key)
    user = serializer.validated_data['user']

    # JWT authentication is the sole browser API session; do not create a Django session cookie.
    refresh = RefreshToken.for_user(user)
    access_token = str(refresh.access_token)
    refresh_token = str(refresh)

    response = Response({
        'user': user_session_data(user),
        'access': access_token,
        'message': '登录成功'
    })
    _set_refresh_cookie(response, refresh_token)
    return response

@api_view(['POST'])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def logout_view(request):
    """用户退出登录，将 refresh token 加入黑名单并清理 HttpOnly cookie"""
    refresh_token = request.data.get('refresh') or request.COOKIES.get(REFRESH_COOKIE_NAME)
    if refresh_token:
        try:
            from rest_framework_simplejwt.tokens import RefreshToken as JWTRefreshToken
            JWTRefreshToken(refresh_token).blacklist()
        except Exception:
            pass

    logout(request)
    response = Response({'message': '退出成功'})
    _clear_refresh_cookie(response)
    return response

@api_view(['GET'])
def profile_view(request):
    if not request.user.is_authenticated:
        return Response({'error': '未登录'}, status=status.HTTP_401_UNAUTHORIZED)
    
    return Response(user_session_data(request.user))

class UserListView(generics.ListCreateAPIView):
    queryset = User.objects.all().order_by('username')
    permission_classes = [permissions.IsAuthenticated]

    def get_permissions(self):
        if self.request.method == 'POST':
            return [permissions.IsAdminUser()]
        return super().get_permissions()

    def get_serializer_class(self):
        return UserCreateSerializer if self.request.method == 'POST' else UserDirectorySerializer

    def get_queryset(self):
        if self.request.user.is_staff or self.request.user.is_superuser:
            return self.queryset.filter(is_active=True)
        return self.queryset.filter(pk=self.request.user.pk, is_active=True)


class UserDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = User.objects.all().order_by('username')
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if self.request.user.is_staff or self.request.user.is_superuser:
            return self.queryset
        return self.queryset.filter(pk=self.request.user.pk)

    def get_permissions(self):
        if self.request.method == 'DELETE':
            return [permissions.IsAdminUser()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH') and not (
            self.request.user.is_staff or self.request.user.is_superuser
        ):
            return UserSelfUpdateSerializer
        if self.request.user.is_staff or self.request.user.is_superuser:
            return UserSerializer
        return UserDirectorySerializer


# ========== 图形验证码 & 短信验证码 ==========

@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def get_captcha(request):
    """获取图形验证码"""
    try:
        result = create_captcha()
    except (redis.RedisError, OSError):
        return Response(
            {'success': False, 'error': '验证码服务暂时不可用'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response(result)


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def send_register_code(request):
    """Send a captcha-verified SMS code without exposing account registration state."""
    phone = request.data.get('phone')
    captcha_token = request.data.get('captcha_token')
    captcha_code = request.data.get('captcha_code')
    mode = request.data.get('mode', 'register')

    if not isinstance(phone, str) or not phone.strip():
        return Response({'success': False, 'error': '请输入手机号'}, status=status.HTTP_400_BAD_REQUEST)
    phone = phone.strip()
    if len(phone) > 11 or not re.fullmatch(r'1[3-9]\d{9}', phone):
        return Response({'success': False, 'error': '手机号格式不正确'}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(mode, str) or len(mode) > 16 or mode not in {'register', 'login'}:
        return Response({'success': False, 'error': '无效的验证码用途'}, status=status.HTTP_400_BAD_REQUEST)
    if (
        not isinstance(captcha_token, str)
        or not captcha_token
        or len(captcha_token) > 128
        or not isinstance(captcha_code, str)
        or not captcha_code
        or len(captcha_code) > 16
    ):
        return Response({'success': False, 'error': '请输入图形验证码'}, status=status.HTTP_400_BAD_REQUEST)

    # SMS issuance must not distinguish registered from unregistered phone numbers.
    result = send_register_verify_code(phone, captcha_token, captcha_code)
    return Response(
        result,
        status=status.HTTP_200_OK if result.get('success') else status.HTTP_400_BAD_REQUEST,
    )


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def sms_login_view(request):
    """短信验证码登录：校验短信码 → 返回 JWT（未注册提示用户先注册）"""
    phone = request.data.get('phone')
    verify_code = request.data.get('verify_code')
    verify_code_token = request.data.get('verify_code_token')

    if not isinstance(phone, str) or len(phone) > 11:
        return Response({'error': '手机号格式不正确'}, status=status.HTTP_400_BAD_REQUEST)
    phone = phone.strip()
    if not phone:
        return Response({'error': '请输入手机号'}, status=status.HTTP_400_BAD_REQUEST)
    if not re.fullmatch(r'1[3-9]\d{9}', phone):
        return Response({'error': '手机号格式不正确'}, status=status.HTTP_400_BAD_REQUEST)

    if (
        not isinstance(verify_code, str)
        or not verify_code
        or len(verify_code) > 6
        or not isinstance(verify_code_token, str)
        or not verify_code_token
        or len(verify_code_token) > 128
    ):
        return Response({'error': '请输入短信验证码'}, status=status.HTTP_400_BAD_REQUEST)
    verify_code = verify_code.strip()
    verify_code_token = verify_code_token.strip()

    valid, error = validate_verify_code(phone, verify_code_token, verify_code)
    if not valid:
        return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

    # 查找用户
    matching_users = User.objects.filter(phone=phone)
    user_count = matching_users.count()
    if user_count > 1:
        return Response(
            {'error': '该手机号关联多个账号，请联系管理员处理'},
            status=status.HTTP_409_CONFLICT,
        )
    if not user_count:
        return Response({'error': '该手机号未注册，请先注册'}, status=status.HTTP_400_BAD_REQUEST)
    user = matching_users.first()

    if not user.is_active:
        return Response({'error': '用户已被禁用'}, status=status.HTTP_400_BAD_REQUEST)

    # 生成 JWT
    refresh = RefreshToken.for_user(user)
    response = Response({
        'user': user_session_data(user),
        'access': str(refresh.access_token),
        'message': '登录成功',
    })
    _set_refresh_cookie(response, str(refresh))
    return response


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def websocket_ticket_view(request):
    """Issue a one-shot ticket for a browser WebSocket handshake.

    The browser sends its in-memory JWT through this authenticated HTTP call;
    only the short-lived, path-bound ticket is placed in the WebSocket URL.
    """
    channel = str(request.data.get('channel') or '').strip()
    execution_id = str(request.data.get('execution_id') or '').strip()
    if (
        channel not in {'app-automation', 'perf-testing'}
        or not execution_id.isascii()
        or not execution_id.isdigit()
        or len(execution_id) > 19
    ):
        return Response({'error': '无效的 WebSocket 通道或执行记录'},
                        status=status.HTTP_400_BAD_REQUEST)

    if channel == 'app-automation':
        from apps.app_automation.access import scope_executions
        from apps.app_automation.models import AppTestExecution
        allowed = scope_executions(
            AppTestExecution.objects.filter(pk=int(execution_id)), request.user
        ).exists()
    else:
        from apps.perf_testing.access import can_access_project
        from apps.perf_testing.models import PerfExecution
        execution = PerfExecution.objects.select_related('project').filter(
            pk=int(execution_id)
        ).first()
        allowed = bool(execution and can_access_project(request.user, execution.project))

    # Do not disclose whether an inaccessible execution exists.
    if not allowed:
        return Response({'error': '执行记录不存在'}, status=status.HTTP_404_NOT_FOUND)

    from apps.core.websocket_auth import issue_websocket_ticket

    path = f'/ws/{channel}/executions/{execution_id}/'
    ticket, expires_in_seconds = issue_websocket_ticket(
        user=request.user,
        path=path,
    )
    return Response({
        'ticket': ticket,
        'expires_in_seconds': expires_in_seconds,
        'path': path,
    })


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def exchange_token_view(request):
    """Exchange a short-lived, one-time SSO code for a browser JWT."""
    action = request.data.get('action', '')
    if not isinstance(action, str) or len(action) > 16:
        return Response({'error': '无效的 action'}, status=status.HTTP_400_BAD_REQUEST)
    if action == 'create':
        if not request.user.is_authenticated:
            return Response({'error': '未登录'}, status=status.HTTP_401_UNAUTHORIZED)
        limit = _positive_setting('EXCHANGE_TOKEN_CREATE_RATE_LIMIT', 10, 1000)
        window = _positive_setting('EXCHANGE_TOKEN_RATE_WINDOW_SECONDS', 60, 3600)
        try:
            allowed = _rate_limit_allows(
                f'auth:exchange:create:user:{request.user.pk}', limit, window
            )
        except (redis.RedisError, OSError):
            return Response({'error': '服务暂时不可用'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if not allowed:
            return Response({'error': '授权码请求过于频繁'}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        try:
            redis_client = get_redis()
            for _ in range(3):
                code = secrets.token_urlsafe(32)
                if redis_client.set(f'exchange:{code}', request.user.pk, ex=60, nx=True):
                    return Response({'code': code})
        except (redis.RedisError, OSError):
            return Response({'error': '授权码服务暂时不可用'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'error': '授权码服务暂时不可用'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    if action == 'redeem':
        code = request.data.get('code')
        if not isinstance(code, str) or not code.strip() or len(code) > 128:
            return Response({'error': '授权码无效或已过期'}, status=status.HTTP_400_BAD_REQUEST)
        code = code.strip()

        limit = _positive_setting('EXCHANGE_TOKEN_REDEEM_RATE_LIMIT', 30, 1000)
        window = _positive_setting('EXCHANGE_TOKEN_RATE_WINDOW_SECONDS', 300, 3600)
        remote_addr = str(request.META.get('REMOTE_ADDR', 'unknown'))[:128]
        address_key = hashlib.sha256(remote_addr.encode('utf-8')).hexdigest()
        try:
            allowed = _rate_limit_allows(
                f'auth:exchange:redeem:ip:{address_key}', limit, window
            )
        except (redis.RedisError, OSError):
            return Response({'error': '服务暂时不可用'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if not allowed:
            return Response({'error': '授权码兑换请求过于频繁'}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        try:
            user_id = _consume_redis_key(get_redis(), f'exchange:{code}')
        except (redis.RedisError, OSError):
            return Response({'error': '授权码服务暂时不可用'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if user_id is None:
            return Response({'error': '授权码无效或已过期'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if isinstance(user_id, bytes):
                user_id = user_id.decode('ascii')
            user = User.objects.get(pk=int(user_id))
        except (User.DoesNotExist, TypeError, ValueError, UnicodeDecodeError):
            return Response({'error': '授权码无效或已过期'}, status=status.HTTP_400_BAD_REQUEST)

        if not user.is_active:
            return Response({'error': '授权码无效或已过期'}, status=status.HTTP_400_BAD_REQUEST)

        refresh = RefreshToken.for_user(user)
        response = Response({
            'access': str(refresh.access_token),
            'user': user_session_data(user),
        })
        _set_refresh_cookie(response, str(refresh))
        return response

    return Response({'error': '无效的 action'}, status=status.HTTP_400_BAD_REQUEST)
