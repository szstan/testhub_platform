from rest_framework import generics, status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from django.conf import settings
from django.contrib.auth import logout
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from .models import User
from .serializers import (
    UserSerializer,
    UserCreateSerializer,
    UserDirectorySerializer,
    UserSelfUpdateSerializer,
    LoginSerializer,
)

# JWT 相关导入
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

REFRESH_COOKIE_NAME = 'testhub_refresh'


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
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        payload = dict(serializer.validated_data)
        refresh = payload.pop('refresh', None)
        response = Response(payload, status=status.HTTP_200_OK)
        if refresh:
            _set_refresh_cookie(response, refresh)
        return response



from .captcha import create_captcha
from .sms import send_register_verify_code, validate_verify_code
from .redis_client import get_redis
import redis
import re
import uuid

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_current_user(request):
    serializer = UserSerializer(request.user)
    return Response(serializer.data)

@method_decorator(csrf_exempt, name='dispatch')
class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all().order_by('username')
    serializer_class = UserCreateSerializer
    permission_classes = [permissions.AllowAny]
    
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response({
            'user': UserSerializer(user).data,
        }, status=status.HTTP_201_CREATED)

@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def login_view(request):
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.validated_data['user']

    # JWT authentication is the sole browser API session; do not create a Django session cookie.
    refresh = RefreshToken.for_user(user)
    access_token = str(refresh.access_token)
    refresh_token = str(refresh)

    response = Response({
        'user': UserSerializer(user).data,
        'access': access_token,
        'message': '登录成功'
    })
    _set_refresh_cookie(response, refresh_token)
    return response

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
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
    
    serializer = UserSerializer(request.user)
    return Response(serializer.data)

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
        return self.queryset.filter(is_active=True)


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
    """发送短信验证码（注册/登录通用）"""
    phone = request.data.get('phone', '').strip()
    captcha_token = request.data.get('captcha_token', '')
    captcha_code = request.data.get('captcha_code', '')
    mode = request.data.get('mode', 'register')  # 'register' 或 'login'

    if not phone:
        return Response({'success': False, 'error': '请输入手机号'}, status=status.HTTP_400_BAD_REQUEST)
    if not captcha_token or not captcha_code:
        return Response({'success': False, 'error': '请输入图形验证码'}, status=status.HTTP_400_BAD_REQUEST)

    # 登录模式：校验手机号是否已注册
    if mode == 'login':
        if not User.objects.filter(phone=phone).exists():
            return Response({'success': False, 'error': '该手机号未注册，请先注册'}, status=status.HTTP_400_BAD_REQUEST)

    # 注册模式：校验手机号未被注册
    if mode == 'register':
        if User.objects.filter(phone=phone).exists():
            return Response({'success': False, 'error': '该手机号已被注册'}, status=status.HTTP_400_BAD_REQUEST)

    result = send_register_verify_code(phone, captcha_token, captcha_code)

    if result.get('success'):
        return Response(result)
    else:
        return Response(result, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def sms_login_view(request):
    """短信验证码登录：校验短信码 → 返回 JWT（未注册提示用户先注册）"""
    phone = request.data.get('phone', '').strip()
    verify_code = request.data.get('verify_code', '').strip()
    verify_code_token = request.data.get('verify_code_token', '').strip()

    # 校验手机号
    if not phone:
        return Response({'error': '请输入手机号'}, status=status.HTTP_400_BAD_REQUEST)
    if not re.match(r'^1[3-9]\d{9}$', phone):
        return Response({'error': '手机号格式不正确'}, status=status.HTTP_400_BAD_REQUEST)

    # 校验短信验证码
    if not verify_code or not verify_code_token:
        return Response({'error': '请输入短信验证码'}, status=status.HTTP_400_BAD_REQUEST)

    valid, error = validate_verify_code(phone, verify_code_token, verify_code)
    if not valid:
        return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

    # 查找用户
    try:
        user = User.objects.get(phone=phone)
    except User.DoesNotExist:
        return Response({'error': '该手机号未注册，请先注册'}, status=status.HTTP_400_BAD_REQUEST)

    if not user.is_active:
        return Response({'error': '用户已被禁用'}, status=status.HTTP_400_BAD_REQUEST)

    # 生成 JWT
    refresh = RefreshToken.for_user(user)
    response = Response({
        'user': UserSerializer(user).data,
        'access': str(refresh.access_token),
        'message': '登录成功',
    })
    _set_refresh_cookie(response, str(refresh))
    return response


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def exchange_token_view(request):
    """
    一次性授权码交换
    action=create:  用当前 JWT 换取一次性 code（需登录），60 秒有效
    action=redeem:  用 code 换取 JWT（无需登录），一次性使用
    """
    action = request.data.get('action', '')
    redis_client = get_redis()

    if action == 'create':
        if not request.user.is_authenticated:
            return Response({'error': '未登录'}, status=status.HTTP_401_UNAUTHORIZED)
        code = str(uuid.uuid4()).replace('-', '')[:8]
        redis_client.setex(f"exchange:{code}", 60, request.user.id)
        return Response({'code': code})

    elif action == 'redeem':
        code = request.data.get('code', '').strip()
        if not code:
            return Response({'error': '缺少授权码'}, status=status.HTTP_400_BAD_REQUEST)

        user_id = redis_client.get(f"exchange:{code}")
        if user_id is None:
            return Response({'error': '授权码无效或已过期'}, status=status.HTTP_400_BAD_REQUEST)

        # 立即删除，保证一次性使用
        redis_client.delete(f"exchange:{code}")

        try:
            user = User.objects.get(id=int(user_id))
        except User.DoesNotExist:
            return Response({'error': '用户不存在'}, status=status.HTTP_400_BAD_REQUEST)

        if not user.is_active:
            return Response({'error': '用户已被禁用'}, status=status.HTTP_400_BAD_REQUEST)

        refresh = RefreshToken.for_user(user)
        response = Response({
            'access': str(refresh.access_token),
            'user': UserSerializer(user).data,
        })
        _set_refresh_cookie(response, str(refresh))
        return response

    else:
        return Response({'error': '无效的 action'}, status=status.HTTP_400_BAD_REQUEST)
