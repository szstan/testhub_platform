import json
import re

from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

User = get_user_model()
__test__ = False


def _validate_phone(phone):
    """校验手机号格式"""
    if not phone:
        return False, '请输入手机号'
    if not re.match(r'^1[3-9]\d{9}$', phone):
        return False, '手机号格式不正确'
    return True, ''


@csrf_exempt
@require_http_methods(["POST"])
def test_register(request):
    try:
        data = json.loads(request.body)

        # 检查用户名是否已存在
        if User.objects.filter(username=data.get('username')).exists():
            return JsonResponse({
                'success': False,
                'error': '用户名已存在'
            }, status=400)

        # 手机号及短信证明必须有界且绑定为同一个号码。
        phone = data.get('phone')
        if not isinstance(phone, str) or len(phone) > 11:
            return JsonResponse({'success': False, 'error': '手机号格式不正确'}, status=400)
        phone = phone.strip()
        valid, error = _validate_phone(phone)
        if not valid:
            return JsonResponse({'success': False, 'error': error}, status=400)

        verify_code = data.get('verify_code')
        verify_code_token = data.get('verify_code_token')
        if (
            not isinstance(verify_code, str)
            or not verify_code
            or len(verify_code) > 6
            or not isinstance(verify_code_token, str)
            or not verify_code_token
            or len(verify_code_token) > 128
        ):
            return JsonResponse({
                'success': False,
                'error': '请输入短信验证码'
            }, status=400)

        from .sms import validate_verify_code
        valid, error = validate_verify_code(
            phone, verify_code_token.strip(), verify_code.strip()
        )
        if not valid:
            return JsonResponse({'success': False, 'error': error}, status=400)

        # Check phone availability only after phone ownership has been proven.
        if User.objects.filter(phone=phone).exists():
            return JsonResponse({
                'success': False,
                'error': '该手机号已被注册'
            }, status=400)

        # 创建用户
        from .registration import DuplicatePhoneError, create_user_with_unique_phone
        try:
            user = create_user_with_unique_phone(
                username=data.get('username'),
                email=data.get('email', ''),
                password=data.get('password'),
                first_name=data.get('first_name', ''),
                last_name=data.get('last_name', ''),
                phone=phone,
                department=data.get('department', ''),
                position=data.get('position', '')
            )
        except DuplicatePhoneError:
            return JsonResponse({'success': False, 'error': '该手机号已被注册'}, status=400)

        # 生成 JWT token
        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken.for_user(user)

        response = JsonResponse({
            'success': True,
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'phone': user.phone,
                'first_name': user.first_name,
                'last_name': user.last_name
            },
            'access': str(refresh.access_token),
        })
        from .views import _set_refresh_cookie
        _set_refresh_cookie(response, str(refresh))
        return response

    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)