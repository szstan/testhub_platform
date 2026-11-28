# -*- coding: utf-8 -*-
# -----------------------------
# @Author    : 影子
# @Time      : 2026/1/16 22:16
# @Software  : PyCharm
# @FileName  : random_tools.py
# -----------------------------
"""
随机数据生成工具
"""
import logging
import random
import string
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, List

logger = logging.getLogger(__name__)
MAX_RANDOM_COUNT = 10_000
MAX_RANDOM_LENGTH = 4_096
MAX_RANDOM_TOTAL_OUTPUT = 1_000_000


def validate_generation_budget(count=1, length=None):
    """Raise ValueError when a random generation request exceeds its budget."""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_RANDOM_COUNT:
        raise ValueError(f'数量必须是 1 到 {MAX_RANDOM_COUNT} 之间的整数')
    if length is None:
        return
    if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= MAX_RANDOM_LENGTH:
        raise ValueError(f'长度必须是 1 到 {MAX_RANDOM_LENGTH} 之间的整数')
    if count * length > MAX_RANDOM_TOTAL_OUTPUT:
        raise ValueError('生成的数据量超过上限')


def _integer_output_length(value):
    bits = value.bit_length()
    return max(1, (bits * 30103 + 99999) // 100000)


def _strftime_output_length(date_format):
    if not isinstance(date_format, str):
        raise ValueError('日期格式必须是字符串')

    expansions = {
        '%': 1, 'a': 9, 'A': 30, 'b': 9, 'B': 30, 'c': 64, 'x': 32, 'X': 32,
        'C': 2, 'g': 2, 'h': 9, 'k': 2, 'l': 2, 'P': 8, 'v': 32, '+': 64,
        'Y': 4, 'y': 2, 'm': 2, 'd': 2, 'e': 2, 'H': 2, 'I': 2, 'M': 2,
        'S': 2, 'f': 6, 'z': 5, 'Z': 64, 'j': 3, 'U': 2, 'W': 2, 'w': 1,
        'u': 1, 'V': 2, 'G': 4, 'p': 8, 'D': 8, 'F': 10, 'T': 8, 'R': 5,
        'r': 11, 's': 16, 'n': 1, 't': 1,
    }
    if len(date_format) > MAX_RANDOM_LENGTH:
        return MAX_RANDOM_LENGTH + 1
    output_length = 0
    index = 0
    while index < len(date_format):
        if date_format[index] != '%':
            output_length += 1
            index += 1
            continue

        index += 1
        while index < len(date_format) and date_format[index] in '-_0^#':
            index += 1
        width_start = index
        while index < len(date_format) and date_format[index].isdigit():
            index += 1
        width_text = date_format[width_start:index]
        if len(width_text) > 4:
            return MAX_RANDOM_LENGTH + 1
        width = int(width_text) if width_text else 0
        if index < len(date_format) and date_format[index] == '.':
            index += 1
            precision_start = index
            while index < len(date_format) and date_format[index].isdigit():
                index += 1
            precision_text = date_format[precision_start:index]
            if len(precision_text) > 4:
                return MAX_RANDOM_LENGTH + 1
            width = max(width, int(precision_text) if precision_text else 0)
        if index < len(date_format) and date_format[index] in 'EO':
            index += 1
        if index >= len(date_format):
            output_length += 1
            break

        directive = date_format[index]
        index += 1
        output_length += max(width, expansions.get(directive, 64))
        if output_length > MAX_RANDOM_LENGTH:
            return output_length

    return max(1, output_length)


def _json_output_length(value, limit, depth=0):
    if depth > 32:
        return limit + 1
    if value is None:
        return 4
    if isinstance(value, bool):
        return 4 if value else 5
    if isinstance(value, int):
        return _integer_output_length(value) + (1 if value < 0 else 0)
    if isinstance(value, float):
        return len(repr(value))
    if isinstance(value, str):
        if len(value) > limit:
            return limit + 1
        size = 2
        for char in value:
            codepoint = ord(char)
            if char in '\\"':
                size += 2
            elif char in '\b\f\n\r\t':
                size += 2
            elif codepoint < 0x20:
                size += 6
            elif codepoint < 0x80:
                size += 1
            elif codepoint <= 0xFFFF:
                size += 6
            else:
                size += 12
            if size > limit:
                return limit + 1
        return size
    if isinstance(value, (list, tuple)):
        if len(value) > limit:
            return limit + 1
        size = 2 + max(0, len(value) - 1)
        for item in value:
            size += _json_output_length(item, limit - size, depth + 1)
            if size > limit:
                return limit + 1
        return size
    if isinstance(value, dict):
        if len(value) > limit:
            return limit + 1
        size = 2 + max(0, len(value) - 1)
        for key, item in value.items():
            if not isinstance(key, str):
                return limit + 1
            size += _json_output_length(key, limit - size, depth + 1) + 1
            if size > limit:
                return limit + 1
            size += _json_output_length(item, limit - size, depth + 1)
            if size > limit:
                return limit + 1
        return size
    return limit + 1


class RandomTools:
    """随机工具类"""

    @staticmethod
    def random_int(min_val: int, max_val: int, count: int = 1) -> Dict[str, Any]:
        """生成随机整数"""
        try:
            validate_generation_budget(count)
            if isinstance(min_val, int) and isinstance(max_val, int):
                output_length = max(_integer_output_length(min_val), _integer_output_length(max_val))
                if min_val < 0 or max_val < 0:
                    output_length += 1
                validate_generation_budget(count, output_length)
            if count == 1:
                result = random.randint(min_val, max_val)
                return {'success': True, 'result': result}
            else:
                result = [random.randint(min_val, max_val) for _ in range(count)]
                return {'success': True, 'result': result, 'count': len(result)}
        except Exception as e:
            logger.error(f'随机数生成失败: {str(e)}')
            return {'result': False, 'error': f'随机数生成失败: {str(e)}'}

    @staticmethod
    def random_float(min_val: float, max_val: float, precision: int = 2, count: int = 1) -> Dict[str, Any]:
        """生成随机浮点数"""
        try:
            validate_generation_budget(count)
            if (
                isinstance(precision, bool)
                or not isinstance(precision, int)
                or not -MAX_RANDOM_LENGTH <= precision <= MAX_RANDOM_LENGTH
            ):
                raise ValueError(f'精度必须是 -{MAX_RANDOM_LENGTH} 到 {MAX_RANDOM_LENGTH} 之间的整数')
            validate_generation_budget(count, max(1, abs(precision)))
            if count == 1:
                result = round(random.uniform(min_val, max_val), precision)
                return {'success': True, 'result': result}
            else:
                result = [round(random.uniform(min_val, max_val), precision) for _ in range(count)]
                return {'success': True, 'result': result, 'count': len(result)}
        except Exception as e:
            logger.error(f'随机浮点数生成失败: {str(e)}')
            return {'result': False, 'error': f'随机浮点数生成失败: {str(e)}'}

    @staticmethod
    def random_string(length: int = 10, char_type: str = 'all', count: int = 1) -> Dict[str, Any]:
        """生成随机字符串"""
        try:
            validate_generation_budget(count, length)
            char_sets = {
                'all': string.ascii_letters + string.digits + string.punctuation,
                'letters': string.ascii_letters,
                'lowercase': string.ascii_lowercase,
                'uppercase': string.ascii_uppercase,
                'digits': string.digits,
                'alphanumeric': string.ascii_letters + string.digits,
                'hex': string.hexdigits.lower(),
                'chinese': '的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什认六共权收证改张象完却究支群市音强书山兵类体况引历切目位且速究况达织密界目系林群米',
                'special': string.punctuation
            }

            if char_type not in char_sets:
                return {'result': False, 'error': f'不支持的字符类型: {char_type}'}

            chars = char_sets[char_type]
            if count == 1:
                result = ''.join(random.choice(chars) for _ in range(length))
                return {'success': True, 'result': result, 'length': len(result)}
            else:
                result = [''.join(random.choice(chars) for _ in range(length)) for _ in range(count)]
                return {'success': True, 'result': result, 'count': len(result), 'string_length': length}
        except Exception as e:
            logger.error(f'随机字符串生成失败: {str(e)}')
            return {'result': False, 'error': f'随机字符串生成失败: {str(e)}'}

    @staticmethod
    def random_uuid(version: int = 4, count: int = 1) -> Dict[str, Any]:
        """生成随机UUID"""
        try:
            validate_generation_budget(count, 36)
            if version == 1:
                uuid_gen = uuid.uuid1
            elif version == 4:
                uuid_gen = uuid.uuid4
            else:
                logger.error(f'不支持的UUID版本: {version}')
                return {'result': False, 'error': f'不支持的UUID版本: {version}'}

            if count == 1:
                result = str(uuid_gen())
                return {
                    'success': True,
                    'result': result,
                    'version': version,
                    'format': 'string'
                }
            else:
                result = [str(uuid_gen()) for _ in range(count)]
                return {
                    'success': True,
                    'result': result,
                    'version': version,
                    'count': len(result)
                }
        except Exception as e:
            logger.error(f'UUID生成失败: {str(e)}')
            return {'result': False, 'error': f'UUID生成失败: {str(e)}'}

    @staticmethod
    def random_mac_address(separator: str = ':', count: int = 1) -> Dict[str, Any]:
        """生成随机MAC地址"""
        try:
            validate_generation_budget(count)
            if not isinstance(separator, str):
                raise ValueError('分隔符必须是字符串')
            validate_generation_budget(count, 12 + 5 * len(separator))

            def generate_mac():
                # 生成6个2位16进制数
                parts = [f'{random.randint(0x00, 0xff):02x}' for _ in range(6)]
                # 确保第一个字节的最低位为0（单播地址），次低位为0（本地管理）
                parts[0] = f'{int(parts[0], 16) & 0xfe:02x}'
                return separator.join(parts)

            if count == 1:
                result = generate_mac()
                return {'success': True, 'result': result}
            else:
                result = [generate_mac() for _ in range(count)]
                return {'success': True, 'result': result, 'count': len(result)}
        except Exception as e:
            logger.error(f'MAC地址生成失败: {str(e)}')
            return {'result': False, 'error': f'MAC地址生成失败: {str(e)}'}

    @staticmethod
    def random_ip_address(ip_version: int = 4, count: int = 1) -> Dict[str, Any]:
        """生成随机IP地址"""
        try:
            validate_generation_budget(count)

            def generate_ipv4():
                return '.'.join(str(random.randint(0, 255)) for _ in range(4))

            def generate_ipv6():
                # 生成8个4位16进制数的IPv6地址
                parts = []
                for _ in range(8):
                    # 简化生成，避免::压缩
                    parts.append(f'{random.randint(0, 0xffff):04x}')
                return ':'.join(parts)

            if ip_version == 4:
                validate_generation_budget(count, 15)
                if count == 1:
                    result = generate_ipv4()
                    return {'success': True, 'result': result, 'version': 4}
                else:
                    result = [generate_ipv4() for _ in range(count)]
                    return {'success': True, 'result': result, 'version': 4, 'count': len(result)}
            elif ip_version == 6:
                validate_generation_budget(count, 39)
                if count == 1:
                    result = generate_ipv6()
                    return {'success': True, 'result': result, 'version': 6}
                else:
                    result = [generate_ipv6() for _ in range(count)]
                    return {'success': True, 'result': result, 'version': 6, 'count': len(result)}
            else:
                return {'result': False, 'error': f'不支持的IP版本: {ip_version}'}
        except Exception as e:
            logger.error(f'IP地址生成失败: {str(e)}')
            return {'result': False, 'error': f'IP地址生成失败: {str(e)}'}

    @staticmethod
    def random_date(start_date: str, end_date: str, count: int = 1, date_format: str = '%Y-%m-%d') -> Dict[str, Any]:
        """生成随机日期"""
        try:
            validate_generation_budget(count)
            validate_generation_budget(count, _strftime_output_length(date_format))
            if any(not isinstance(value, str) or len(value) != 10 for value in (start_date, end_date)):
                raise ValueError('日期必须是 YYYY-MM-DD 格式')
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')

            delta = end_dt - start_dt
            random_seconds = random.randint(0, int(delta.total_seconds()))

            if count == 1:
                result = (start_dt + timedelta(seconds=random_seconds)).strftime(date_format)
                return {'success': True, 'result': result, 'format': date_format}
            else:
                result = []
                for _ in range(count):
                    random_seconds = random.randint(0, int(delta.total_seconds()))
                    result.append((start_dt + timedelta(seconds=random_seconds)).strftime(date_format))
                return {'success': True, 'result': result, 'count': len(result), 'format': date_format}
        except Exception as e:
            logger.error(f'随机日期生成失败: {str(e)}')
            return {'result': False, 'error': f'随机日期生成失败: {str(e)}'}

    @staticmethod
    def random_boolean(count: int = 1) -> Dict[str, Any]:
        """生成随机布尔值"""
        try:
            validate_generation_budget(count, 5)
            if count == 1:
                result = random.choice([True, False])
                return {'success': True, 'result': result}
            else:
                result = [random.choice([True, False]) for _ in range(count)]
                return {'success': True, 'result': result, 'count': len(result)}
        except Exception as e:
            logger.error(f'随机布尔值生成失败: {str(e)}')
            return {'result': False, 'error': f'随机布尔值生成失败: {str(e)}'}

    @staticmethod
    def random_color(format: str = 'hex', count: int = 1) -> Dict[str, Any]:
        """生成随机颜色"""
        try:
            validate_generation_budget(count)
            if format == 'hex':
                validate_generation_budget(count, 7)
            elif format == 'rgb':
                validate_generation_budget(count, 24)
            elif format == 'rgba':
                validate_generation_budget(count, 32)
            else:
                raise ValueError('不支持的格式')

            def generate():
                r = random.randint(0, 255)
                g = random.randint(0, 255)
                b = random.randint(0, 255)

                if format == 'hex':
                    return f'#{r:02x}{g:02x}{b:02x}'
                elif format == 'rgb':
                    return f'rgb({r}, {g}, {b})'
                elif format == 'rgba':
                    a = round(random.random(), 2)
                    return f'rgba({r}, {g}, {b}, {a})'
                else:
                    raise ValueError(f'不支持的格式: {format}')

            if count == 1:
                result = generate()
                return {'success': True, 'result': result, 'format': format}
            else:
                result = [generate() for _ in range(count)]
                return {'success': True, 'result': result, 'count': len(result), 'format': format}
        except Exception as e:
            logger.error(f'随机颜色生成失败: {str(e)}')
            return {'result': False, 'error': f'随机颜色生成失败: {str(e)}'}

    @staticmethod
    def random_password(length: int = 12, include_uppercase: bool = True, include_lowercase: bool = True,
                        include_digits: bool = True, include_special: bool = True, count: int = 1) -> Dict[str, Any]:
        """生成随机密码"""
        try:
            validate_generation_budget(count, length)
            chars = ''
            if include_uppercase:
                chars += string.ascii_uppercase
            if include_lowercase:
                chars += string.ascii_lowercase
            if include_digits:
                chars += string.digits
            if include_special:
                chars += '!@#$%^&*()_+-=[]{}|;:,.<>?'

            if not chars:
                return {'result': False, 'error': '至少选择一种字符类型'}

            def generate_password():
                password = ''.join(random.choice(chars) for _ in range(length))
                return password

            if count == 1:
                result = generate_password()
                return {
                    'success': True,
                    'result': result,
                    'length': len(result),
                    'contains': {
                        'uppercase': include_uppercase,
                        'lowercase': include_lowercase,
                        'digits': include_digits,
                        'special': include_special
                    }
                }
            else:
                result = [generate_password() for _ in range(count)]
                return {
                    'success': True,
                    'result': result,
                    'count': len(result),
                    'length': length,
                    'contains': {
                        'uppercase': include_uppercase,
                        'lowercase': include_lowercase,
                        'digits': include_digits,
                        'special': include_special
                    }
                }
        except Exception as e:
            logger.error(f'随机密码生成失败: {str(e)}')
            return {'result': False, 'error': f'随机密码生成失败: {str(e)}'}

    @staticmethod
    def random_sequence(sequence: List, count: int = 1, unique: bool = False) -> Dict[str, Any]:
        """从序列中随机选择"""
        try:
            validate_generation_budget(count, 1)
            if unique:
                if count > len(sequence):
                    logger.error(f'请求数量({count})大于序列长度({len(sequence)})')
                    return {'result': False, 'error': f'请求数量({count})大于序列长度({len(sequence)})'}
                result = random.sample(sequence, count)
            else:
                result = [random.choice(sequence) for _ in range(count)]

            output_length = len(result) + 1 if count > 1 else 0
            if output_length > MAX_RANDOM_TOTAL_OUTPUT:
                raise ValueError('生成的数据量超过上限')
            for item in result:
                available = MAX_RANDOM_TOTAL_OUTPUT - output_length
                item_limit = min(MAX_RANDOM_LENGTH, available)
                item_length = _json_output_length(item, item_limit)
                if item_length > MAX_RANDOM_LENGTH:
                    raise ValueError(f'序列元素长度不能超过 {MAX_RANDOM_LENGTH}')
                if item_length > available:
                    raise ValueError('生成的数据量超过上限')
                output_length += item_length

            return {
                'success': True,
                'result': result[0] if count == 1 else result,
                'count': len(result) if count > 1 else 1
            }
        except Exception as e:
            logger.error(f'随机序列选择失败：{str(e)}')
            return {'result': False, 'error': '随机序列选择失败, 请检查参数!'}
