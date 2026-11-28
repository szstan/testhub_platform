"""
阿里云短信发送模块
复用 zhiqiong 项目的短信通道配置
包含发送频率限制（同一手机号 60 秒内只能发一次）
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import datetime
from urllib.parse import quote

import requests
from django.conf import settings

from .redis_client import get_redis


def _sign(access_key_secret, string_to_sign):
    """阿里云签名 V1 算法"""
    h = hmac.new(
        access_key_secret.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.sha1
    )
    return base64.b64encode(h.digest()).decode('utf-8')


def _percent_encode(s):
    """URL 编码（阿里云特殊规则）"""
    return quote(s, safe='-_.~')


def _build_signature(params, access_key_secret):
    """构建阿里云请求签名"""
    sorted_keys = sorted(params.keys())
    canonicalized_query = '&'.join([
        f"{_percent_encode(k)}={_percent_encode(str(params[k]))}"
        for k in sorted_keys
    ])
    string_to_sign = 'POST&%2F&' + _percent_encode(canonicalized_query)
    return _sign(access_key_secret + '&', string_to_sign)


def send_sms(phone_number, template_code, template_param, sign_name=None):
    """
    通过阿里云 SMS 发送短信

    Args:
        phone_number: 手机号
        template_code: 短信模板编号
        template_param: 模板参数 dict，如 {'code': '123456'}
        sign_name: 签名名称，默认使用配置中的 SMS_SIGN_NAME
    """
    access_key_id = settings.SMS_ACCESS_KEY_ID
    access_key_secret = settings.SMS_ACCESS_KEY_SECRET
    if sign_name is None:
        sign_name = settings.SMS_SIGN_NAME

    params = {
        'AccessKeyId': access_key_id,
        'Action': 'SendSms',
        'Format': 'JSON',
        'PhoneNumbers': phone_number,
        'SignName': sign_name,
        'SignatureMethod': 'HMAC-SHA1',
        'SignatureNonce': str(uuid.uuid4()),
        'SignatureVersion': '1.0',
        'TemplateCode': template_code,
        'TemplateParam': json.dumps(template_param, ensure_ascii=False),
        'Timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'Version': '2017-05-25',
    }

    signature = _build_signature(params, access_key_secret)
    params['Signature'] = signature

    response = requests.post(
        'https://dysmsapi.aliyuncs.com/',
        data=params,
        timeout=10
    )
    result = response.json()
    return result.get('Code') == 'OK', result


# ========== 验证码相关业务逻辑 ==========

VERIFY_CODE_LENGTH = 6
VERIFY_CODE_TTL = 300        # 验证码 5 分钟有效
VERIFY_MAX_ATTEMPTS = 5
SMS_RATE_LIMIT_TTL = 60      # 同一手机号 60 秒内只能发一次


def _generate_verify_code():
    """Generate an unpredictable six-digit SMS verification code."""
    return ''.join(str(secrets.randbelow(10)) for _ in range(VERIFY_CODE_LENGTH))


def check_rate_limit(phone_number):
    """检查发送频率限制，返回 (allowed: bool, remaining_seconds: int)"""
    redis_client = get_redis()
    key = f"sms:limit:{phone_number}"
    ttl = redis_client.ttl(key)
    if ttl > 0:
        return False, ttl
    return True, 0


def _reserve_rate_limit(phone_number):
    """Atomically reserve the per-phone cooldown before sending an SMS."""
    redis_client = get_redis()
    key = f"sms:limit:{phone_number}"
    if redis_client.set(key, '1', ex=SMS_RATE_LIMIT_TTL, nx=True):
        return True, 0
    return False, max(redis_client.ttl(key), 1)


_VERIFY_CODE_CONSUME_LUA = """
local stored_code = redis.call('GET', KEYS[1])
if not stored_code then return -1 end
local attempts = redis.call('INCR', KEYS[3])
if attempts == 1 then
    redis.call('EXPIRE', KEYS[3], ARGV[3])
end
if attempts > tonumber(ARGV[4]) then
    redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
    return -4
end
local stored_phone = redis.call('GET', KEYS[2])
if not stored_phone then return -1 end
if stored_code ~= ARGV[1] then return -2 end
if stored_phone ~= ARGV[2] then return -3 end
redis.call('DEL', KEYS[1], KEYS[2])
return 1
"""


def send_register_verify_code(phone_number, captcha_token, captcha_code):
    """
    Validate the captcha, apply the phone cooldown, and send a registration/login code.
    """
    from apps.users.captcha import validate_captcha

    if (
        not isinstance(phone_number, str)
        or not re.fullmatch(r'1[3-9]\d{9}', phone_number)
        or not isinstance(captcha_token, str)
        or not captcha_token
        or len(captcha_token) > 128
        or not isinstance(captcha_code, str)
        or not captcha_code
        or len(captcha_code) > 16
    ):
        return {'success': False, 'error': '请求参数无效'}

    if not validate_captcha(captcha_token, captcha_code):
        return {'success': False, 'error': '图形验证码错误或已过期'}

    allowed, remaining = _reserve_rate_limit(phone_number)
    if not allowed:
        return {'success': False, 'error': f'发送过于频繁，请 {remaining} 秒后再试'}

    code = _generate_verify_code()
    token = str(uuid.uuid4())

    redis_client = get_redis()
    redis_client.setex(f"sms:verify_code:{token}", VERIFY_CODE_TTL, code)
    redis_client.setex(f"sms:phone:{token}", VERIFY_CODE_TTL, phone_number)

    success, result = send_sms(
        phone_number=phone_number,
        template_code=settings.SMS_REGISTER_TEMPLATE_CODE,
        template_param={'code': code},
    )

    if not success:
        redis_client.delete(f"sms:verify_code:{token}")
        redis_client.delete(f"sms:limit:{phone_number}")
        redis_client.delete(f"sms:phone:{token}")
        return {'success': False, 'error': f'短信发送失败: {result.get("Message", "未知错误")}'}

    return {
        'success': True,
        'verify_code_token': token,
        'message': '验证码已发送',
    }


def validate_verify_code(phone_number, verify_code_token, verify_code):
    """Validate and atomically consume one phone-bound SMS verification code."""
    if (
        not isinstance(phone_number, str)
        or len(phone_number) > 11
        or not isinstance(verify_code_token, str)
        or not verify_code_token
        or len(verify_code_token) > 128
        or not isinstance(verify_code, str)
        or not verify_code
        or len(verify_code) > VERIFY_CODE_LENGTH
    ):
        return False, '验证码错误或已过期，请重新获取'

    redis_client = get_redis()
    result = redis_client.eval(
        _VERIFY_CODE_CONSUME_LUA,
        3,
        f"sms:verify_code:{verify_code_token}",
        f"sms:phone:{verify_code_token}",
        f"sms:verify_attempts:{verify_code_token}",
        verify_code,
        phone_number,
        VERIFY_CODE_TTL,
        VERIFY_MAX_ATTEMPTS,
    )
    errors = {
        -1: '验证码已过期，请重新获取',
        -2: '验证码错误',
        -3: '手机号与验证码不匹配',
        -4: '验证码错误次数过多，请重新获取',
    }
    return (True, '') if result == 1 else (False, errors.get(result, '验证码错误或已过期，请重新获取'))
