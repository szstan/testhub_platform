"""
统一变量解析器
支持在接口测试和UI自动化测试中使用动态函数表达式
语法：${function_name(args)}
"""
import re
import sys
import json
from datetime import datetime, timedelta

# 设置标准输出编码为 UTF-8，避免 Windows 系统上的编码问题
if sys.platform == 'win32':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')

# 导入数据工厂的工具类
from apps.data_factory.tools.random_tools import (
    MAX_RANDOM_COUNT,
    MAX_RANDOM_LENGTH,
    MAX_RANDOM_TOTAL_OUTPUT,
    RandomTools,
    _strftime_output_length,
    validate_generation_budget,
)
from apps.data_factory.tools.test_data_tools import TestDataTools
from apps.data_factory.tools.string_tools import StringTools
from apps.data_factory.tools.encoding_tools import EncodingTools
from apps.data_factory.tools.encryption_tools import EncryptionTools
from apps.data_factory.tools.crontab_tools import CrontabTools
from apps.data_factory.tools.image_tools import ImageTools


_COUNT_ARGUMENTS = {
    'random_int': 2, 'random_float': 3, 'random_string': 2,
    'random_digits': 1, 'random_letters': 1, 'random_chinese': 1,
    'random_uuid': 1, 'random_guid': 1, 'random_mac': 1, 'random_mac_address': 1,
    'random_ip': 1, 'random_ip_address': 1, 'random_date': 2,
    'random_boolean': 0, 'random_color': 1, 'random_password': 1, 'random_sequence': 1,
    'random_phone': 0, 'random_email': 0, 'random_id_card': 0,
    'random_name': 1, 'random_company': 0, 'random_address': 0,
    'generate_chinese_name': 1, 'generate_chinese_phone': 0,
    'generate_chinese_email': 0, 'generate_chinese_address': 1,
    'generate_id_card': 0, 'generate_company_name': 0, 'generate_bank_card': 0,
    'generate_hk_id_card': 0, 'generate_business_license': 0,
    'generate_user_profile': 0, 'generate_coordinates': 0,
    'get_next_runs': 1,
}
_LENGTH_ARGUMENTS = {
    'random_string': (0, 8), 'random_digits': (0, 6), 'random_letters': (0, 8),
    'random_chinese': (0, 2), 'random_password': (0, 12),
}

_STRING_COUNT_FUNCTIONS = {
    'random_string', 'random_digits', 'random_letters', 'random_chinese',
    'random_uuid', 'random_guid', 'random_mac', 'random_mac_address',
    'random_ip', 'random_ip_address', 'random_date', 'random_color',
    'random_password', 'random_sequence', 'random_phone', 'random_email',
    'random_id_card', 'random_name', 'random_company', 'random_address',
    'generate_chinese_name', 'generate_chinese_phone', 'generate_chinese_email',
    'generate_chinese_address', 'generate_id_card', 'generate_company_name',
    'generate_bank_card', 'generate_hk_id_card', 'generate_business_license',
    'get_next_runs',
}
_STRUCTURED_COUNT_FUNCTIONS = {'generate_user_profile', 'generate_coordinates'}


class ResolutionBudget:
    """Cumulative generation limits for one complete variable-resolution pass."""

    def __init__(self, max_count=None, max_output=None):
        self.max_count = MAX_RANDOM_COUNT if max_count is None else max_count
        self.max_output = MAX_RANDOM_TOTAL_OUTPUT if max_output is None else max_output
        self.count = 0
        self.output = 0

    def ensure_expression_capacity(self, expression_count):
        if self.count + expression_count > self.max_count:
            raise ValueError(f'动态函数数量超过累计上限 {self.max_count}')

    def reserve(self, count, output_size):
        if self.count + count > self.max_count:
            raise ValueError(f'动态函数数量超过累计上限 {self.max_count}')
        if self.output + output_size > self.max_output:
            raise ValueError(f'动态函数渲染结果超过累计上限 {self.max_output}')
        self.count += count
        self.output += output_size

    def reconcile_output(self, reserved, actual):
        actual_total = self.output - reserved + actual
        if actual_total > self.max_output:
            raise ValueError(f'动态函数渲染结果超过累计上限 {self.max_output}')
        self.output = actual_total


def _value_text_length(value):
    if isinstance(value, str):
        return len(value)
    if isinstance(value, bool):
        return 5 if value else 4
    if isinstance(value, int):
        bits = value.bit_length()
        return max(1, (bits * 30103 + 99999) // 100000) + (value < 0)
    if isinstance(value, float):
        return len(str(value))
    if value is None:
        return 4
    if isinstance(value, (list, tuple)):
        size = 2
        for item in value:
            size += _value_text_length(item) + 2
            if size > MAX_RANDOM_TOTAL_OUTPUT:
                return MAX_RANDOM_TOTAL_OUTPUT + 1
        return size
    if isinstance(value, dict):
        size = 2
        for key, item in value.items():
            size += _value_text_length(key) + _value_text_length(item) + 4
            if size > MAX_RANDOM_TOTAL_OUTPUT:
                return MAX_RANDOM_TOTAL_OUTPUT + 1
        return size
    return MAX_RANDOM_LENGTH


def _count_output_estimate(func_name, args, count):
    item_length = {
        'random_boolean': 5,
        'random_uuid': 36,
        'random_guid': 36,
        'random_phone': 32,
        'generate_chinese_phone': 32,
        'random_email': 320,
        'generate_chinese_email': 320,
        'random_id_card': 32,
        'generate_id_card': 32,
        'random_name': 64,
        'generate_chinese_name': 64,
        'random_company': 256,
        'generate_company_name': 256,
        'random_address': 512,
        'generate_chinese_address': 512,
        'generate_bank_card': 32,
        'generate_hk_id_card': 16,
        'generate_business_license': 32,
        'generate_user_profile': 2048,
        'generate_coordinates': 256,
        'get_next_runs': 21,
    }.get(func_name)

    if func_name == 'random_string':
        item_length = args[0] if args else 8
    elif func_name == 'random_digits':
        item_length = args[0] if args else 6
    elif func_name == 'random_letters':
        item_length = args[0] if args else 8
    elif func_name == 'random_chinese':
        item_length = args[0] if args else 2
    elif func_name == 'random_password':
        item_length = args[0] if args else 12
    elif func_name == 'random_int':
        bounds = [args[0] if args else 0, args[1] if len(args) > 1 else 100]
        item_length = max(_value_text_length(value) for value in bounds)
    elif func_name == 'random_float':
        bounds = [args[0] if args else 0.0, args[1] if len(args) > 1 else 1.0]
        precision = args[2] if len(args) > 2 else 2
        item_length = max(_value_text_length(value) for value in bounds) + abs(precision) + 2
    elif func_name in {'random_mac', 'random_mac_address'}:
        separator = args[0] if args and isinstance(args[0], str) else ':'
        item_length = 12 + 5 * len(separator)
    elif func_name in {'random_ip', 'random_ip_address'}:
        item_length = 39 if args and args[0] == 6 else 15
    elif func_name == 'random_color':
        item_length = {'hex': 7, 'rgb': 24, 'rgba': 32}.get(args[0] if args else 'hex', 32)
    elif func_name == 'random_date':
        date_format = args[3] if len(args) > 3 else '%Y-%m-%d'
        item_length = _strftime_output_length(date_format)
    elif func_name == 'random_sequence':
        item_length = MAX_RANDOM_LENGTH

    if item_length is None:
        item_length = MAX_RANDOM_LENGTH
    if isinstance(item_length, bool) or not isinstance(item_length, int) or item_length < 0:
        item_length = MAX_RANDOM_LENGTH + 1

    if count == 1:
        return item_length
    if func_name in _STRUCTURED_COUNT_FUNCTIONS:
        return count * (item_length + 2) + 2
    if func_name in _STRING_COUNT_FUNCTIONS:
        return count * (2 * item_length + 4) + 2
    return count * item_length + 2 * (count - 1) + 2


def _expression_output_estimate(func_name, args):
    def text_length(index, default=''):
        return _value_text_length(args[index] if len(args) > index else default)

    if func_name in {'timestamp', 'timestamp_sec', 'datetime', 'date', 'time', 'date_offset'}:
        return 64
    if func_name == 'random_boolean':
        return 5
    if func_name == 'remove_whitespace':
        return text_length(0)
    if func_name == 'case_convert':
        return 3 * text_length(0)
    if func_name == 'replace_string':
        text = args[0] if args and isinstance(args[0], str) else ''
        old = args[1] if len(args) > 1 and isinstance(args[1], str) else ''
        new = args[2] if len(args) > 2 and isinstance(args[2], str) else ''
        replacements = len(text) + 1 if not old else text.count(old)
        return len(text) + max(0, replacements * (len(new) - len(old)))
    if func_name in {'word_count', 'regex_test', 'color_convert', 'validate_expression'}:
        return 4096
    if func_name in {'md5', 'md5_hash'}:
        return 32
    if func_name in {'sha1', 'sha1_hash'}:
        return 40
    if func_name in {'sha256', 'sha256_hash'}:
        return 64
    if func_name == 'sha512_hash':
        return 128
    if func_name in {'base64', 'base64_encode'}:
        # Text codecs such as unicode_escape use ten bytes per code point;
        # reserve their Base64 expansion before invoking the encoding tool.
        return 16 * text_length(0) + 64
    if func_name in {'base64_decode', 'url_decode'}:
        return text_length(0) + 64
    if func_name in {'url_encode', 'unicode_convert', 'ascii_convert'}:
        return 12 * text_length(0) + 64
    if func_name == 'base_convert':
        return 6 * text_length(0) + 64
    if func_name in {'aes_encrypt', 'aes_decrypt', 'hash_comparison'}:
        return 2 * text_length(0) + 256
    if func_name == 'generate_expression':
        return sum(text_length(index) for index in range(min(5, len(args)))) + 8
    if func_name == 'parse_expression':
        return text_length(0) + 256
    if func_name in {'generate_barcode', 'generate_qrcode', 'decode_qrcode', 'image_to_base64', 'base64_to_image'}:
        return 12 * max((text_length(index) for index in range(len(args))), default=0) + 4096
    return MAX_RANDOM_LENGTH


class VariableResolver:
    """统一变量解析器 - 使用数据工厂工具"""
    
    def __init__(self, on_value=None, budget=None):
        self.on_value = on_value
        self.budget = budget if budget is not None else ResolutionBudget()
        self.functions = {
            # 随机工具
            'random_int': self._call_random_tool,
            'random_float': self._call_random_tool,
            'random_digits': self._call_random_tool,
            'random_string': self._call_random_tool,
            'random_letters': self._call_random_tool,
            'random_chinese': self._call_random_tool,
            'random_uuid': self._call_random_tool,
            'random_guid': self._call_random_tool,
            'random_mac': self._call_random_tool,
            'random_mac_address': self._call_random_tool,
            'random_ip': self._call_random_tool,
            'random_ip_address': self._call_random_tool,
            'random_boolean': self._call_random_tool,
            'random_color': self._call_random_tool,
            'random_password': self._call_random_tool,
            'random_sequence': self._call_random_tool,
            'random_date': self._call_random_tool,
            
            # 测试数据工具
            'random_phone': self._call_test_data_tool,
            'random_email': self._call_test_data_tool,
            'random_id_card': self._call_test_data_tool,
            'random_name': self._call_test_data_tool,
            'random_company': self._call_test_data_tool,
            'random_address': self._call_test_data_tool,
            'generate_chinese_name': self._call_test_data_tool,
            'generate_chinese_phone': self._call_test_data_tool,
            'generate_chinese_email': self._call_test_data_tool,
            'generate_chinese_address': self._call_test_data_tool,
            'generate_id_card': self._call_test_data_tool,
            'generate_company_name': self._call_test_data_tool,
            'generate_bank_card': self._call_test_data_tool,
            'generate_hk_id_card': self._call_test_data_tool,
            'generate_business_license': self._call_test_data_tool,
            'generate_user_profile': self._call_test_data_tool,
            'generate_coordinates': self._call_test_data_tool,
            
            # 字符工具
            'remove_whitespace': self._call_string_tool,
            'replace_string': self._call_string_tool,
            'word_count': self._call_string_tool,
            'regex_test': self._call_string_tool,
            'case_convert': self._call_string_tool,
            
            # 编码工具
            'timestamp_convert': self._call_encoding_tool,
            'base64_encode': self._call_encoding_tool,
            'base64_decode': self._call_encoding_tool,
            'url_encode': self._call_encoding_tool,
            'url_decode': self._call_encoding_tool,
            'unicode_convert': self._call_encoding_tool,
            'ascii_convert': self._call_encoding_tool,
            'color_convert': self._call_encoding_tool,
            'base_convert': self._call_encoding_tool,
            'generate_barcode': self._call_encoding_tool,
            'generate_qrcode': self._call_encoding_tool,
            'decode_qrcode':self._call_encoding_tool,
            'image_to_base64': self._call_encoding_tool,
            'base64_to_image': self._call_encoding_tool,
            
            # 加密工具
            'base64': self._call_encryption_tool,
            'md5': self._call_encryption_tool,
            'sha1': self._call_encryption_tool,
            'sha256': self._call_encryption_tool,
            'md5_hash': self._call_encryption_tool,
            'sha1_hash': self._call_encryption_tool,
            'sha256_hash': self._call_encryption_tool,
            'sha512_hash': self._call_encryption_tool,
            'hash_comparison': self._call_encryption_tool,
            'aes_encrypt': self._call_encryption_tool,
            'aes_decrypt': self._call_encryption_tool,
            
            # Crontab工具
            'generate_expression': self._call_crontab_tool,
            'parse_expression': self._call_crontab_tool,
            'get_next_runs': self._call_crontab_tool,
            'validate_expression': self._call_crontab_tool,
            
            # 时间日期函数
            'timestamp': self._timestamp,
            'timestamp_sec': self._timestamp_sec,
            'datetime': self._datetime,
            'date': self._date,
            'time': self._time,
            'date_offset': self._date_offset,
        }
    
    def _validate_function_budget(self, func_name, args):
        count_index = _COUNT_ARGUMENTS.get(func_name)
        count = args[count_index] if count_index is not None and len(args) > count_index else 1
        length = None
        if func_name in _LENGTH_ARGUMENTS:
            index, default = _LENGTH_ARGUMENTS[func_name]
            length = args[index] if len(args) > index else default
        elif func_name == 'random_float':
            precision = args[2] if len(args) > 2 else 2
            length = (
                max(1, abs(precision))
                if isinstance(precision, int) and not isinstance(precision, bool)
                else precision
            )
        elif func_name == 'random_date':
            date_format = args[3] if len(args) > 3 else '%Y-%m-%d'
            length = _strftime_output_length(date_format)
        elif func_name == 'random_color':
            color_format = args[0] if args else 'hex'
            length = {'hex': 7, 'rgb': 24, 'rgba': 32}.get(color_format, 32)
        elif func_name in {'random_ip', 'random_ip_address'}:
            length = 39 if args and args[0] == 6 else 15
        elif func_name in {'random_uuid', 'random_guid'}:
            length = 36
        elif func_name in {'random_boolean'}:
            length = 5
        validate_generation_budget(count=count, length=length)
        return count

    def resolve(self, text):
        """解析文本中的动态函数占位符
        
        Args:
            text: 包含动态函数的文本，如 "Hello ${random_string(8)}"
            
        Returns:
            解析后的文本
        """
        if not isinstance(text, str):
            return text

        pattern = r'\$\{([^}]+)\}'
        expression_count = sum(1 for _ in re.finditer(pattern, text))
        if expression_count:
            validate_generation_budget(count=expression_count, length=None)
            self.budget.ensure_expression_capacity(expression_count)
        
        def replace_func(match):
            expression = match.group(1)
            try:
                return self._evaluate_expression(expression)
            except Exception as e:
                if isinstance(e, UnicodeEncodeError):
                    return match.group(0)
                try:
                    print(f"[WARNING] Variable resolution failed: ${{{expression}}} - {str(e)}")
                except UnicodeEncodeError:
                    print(f"[WARNING] Variable resolution failed: ${{{expression}}}")
                return match.group(0)
        
        return re.sub(pattern, replace_func, text)
    
    def _evaluate_expression(self, expression):
        """评估单个表达式
        
        Args:
            expression: 函数表达式，如 "random_int(100, 200)" 或 "timestamp()"
            
        Returns:
            函数执行结果
        """
        match = re.match(r'(\w+)\((.*)\)', expression.strip())
        if not match:
            func_name = expression.strip()
            args = []
        else:
            func_name = match.group(1)
            args = self._parse_args(match.group(2))

        count = self._validate_function_budget(func_name, args)
        if func_name not in self.functions:
            raise ValueError(f"未知函数: {func_name}")

        output_estimate = (
            _count_output_estimate(func_name, args, count)
            if func_name in _COUNT_ARGUMENTS
            else _expression_output_estimate(func_name, args)
        )
        self.budget.reserve(count, output_estimate)
        result = self.functions[func_name](func_name, args)
        rendered = str(result)
        self.budget.reconcile_output(output_estimate, len(rendered))
        if self.on_value is not None:
            self.on_value(result)
            if not isinstance(result, str):
                self.on_value(rendered)
        return rendered
    
    def _parse_args(self, args_str):
        """解析函数参数
        
        Args:
            args_str: 参数字符串，如 "100, 200" 或 "YYYY-MM-DD"
            
        Returns:
            参数列表
        """
        if not args_str.strip():
            return []
        
        args = []
        current_arg = []
        in_brackets = False
        in_quotes = False
        quote_char = None
        
        for char in args_str:
            if char == '[' and not in_quotes:
                in_brackets = True
                current_arg.append(char)
            elif char == ']' and not in_quotes:
                in_brackets = False
                current_arg.append(char)
            elif char == '"' and not in_quotes:
                in_quotes = True
                quote_char = '"'
                current_arg.append(char)
            elif char == "'" and not in_quotes:
                in_quotes = True
                quote_char = "'"
                current_arg.append(char)
            elif char == quote_char and in_quotes:
                in_quotes = False
                quote_char = None
                current_arg.append(char)
            elif char == ',' and not in_brackets and not in_quotes:
                # 分隔参数
                arg = ''.join(current_arg).strip()
                args.append(self._parse_single_arg(arg))
                current_arg = []
            else:
                current_arg.append(char)
        
        # 添加最后一个参数
        if current_arg:
            arg = ''.join(current_arg).strip()
            args.append(self._parse_single_arg(arg))
        
        return args
    
    def _parse_single_arg(self, arg):
        """解析单个参数
        
        Args:
            arg: 单个参数字符串
            
        Returns:
            解析后的参数值
        """
        if not arg:
            return None
        
        # 尝试转换为布尔值
        if arg.lower() == 'true':
            return True
        elif arg.lower() == 'false':
            return False
        # 尝试转换为数字
        try:
            if '.' in arg:
                return float(arg)
            else:
                return int(arg)
        except ValueError:
            # 尝试解析JSON
            if arg.startswith('[') and arg.endswith(']'):
                try:
                    return json.loads(arg)
                except:
                    pass
            # 移除引号
            return arg.strip('\'"')
    
    def _call_random_tool(self, func_name, args):
        """调用随机工具"""
        tool_mapping = {
            'random_int': RandomTools.random_int,
            'random_float': RandomTools.random_float,
            'random_string': RandomTools.random_string,
            'random_uuid': RandomTools.random_uuid,
            'random_guid': RandomTools.random_uuid,
            'random_mac': RandomTools.random_mac_address,
            'random_mac_address': RandomTools.random_mac_address,
            'random_ip': RandomTools.random_ip_address,
            'random_ip_address': RandomTools.random_ip_address,
            'random_date': RandomTools.random_date,
            'random_boolean': RandomTools.random_boolean,
            'random_color': RandomTools.random_color,
            'random_password': RandomTools.random_password,
            'random_sequence': RandomTools.random_sequence,
        }
        
        # 特殊处理 random_digits 和 random_letters
        if func_name == 'random_digits':
            result = RandomTools.random_string(length=args[0] if args else 6, char_type='digits', count=args[1] if len(args) > 1 else 1)
        elif func_name == 'random_letters':
            result = RandomTools.random_string(length=args[0] if args else 8, char_type='letters', count=args[1] if len(args) > 1 else 1)
        elif func_name == 'random_chinese':
            result = RandomTools.random_string(length=args[0] if args else 2, char_type='chinese', count=args[1] if len(args) > 1 else 1)
        elif func_name in tool_mapping:
            # 根据函数名设置默认参数
            kwargs = {}
            if func_name == 'random_int':
                kwargs = {'min_val': args[0] if len(args) > 0 else 0, 'max_val': args[1] if len(args) > 1 else 100, 'count': args[2] if len(args) > 2 else 1}
            elif func_name == 'random_float':
                kwargs = {'min_val': args[0] if len(args) > 0 else 0.0, 'max_val': args[1] if len(args) > 1 else 1.0, 'precision': args[2] if len(args) > 2 else 2, 'count': args[3] if len(args) > 3 else 1}
            elif func_name == 'random_string':
                kwargs = {'length': args[0] if len(args) > 0 else 8, 'char_type': args[1] if len(args) > 1 else 'all', 'count': args[2] if len(args) > 2 else 1}
            elif func_name in ['random_uuid', 'random_guid']:
                kwargs = {'version': args[0] if len(args) > 0 else 4, 'count': args[1] if len(args) > 1 else 1}
            elif func_name in ['random_mac', 'random_mac_address']:
                kwargs = {'separator': args[0] if len(args) > 0 else ':', 'count': args[1] if len(args) > 1 else 1}
            elif func_name in ['random_ip', 'random_ip_address']:
                kwargs = {'ip_version': args[0] if len(args) > 0 else 4, 'count': args[1] if len(args) > 1 else 1}
            elif func_name == 'random_date':
                kwargs = {'start_date': args[0] if len(args) > 0 else '2024-01-01', 'end_date': args[1] if len(args) > 1 else '2024-12-31', 'count': args[2] if len(args) > 2 else 1, 'date_format': args[3] if len(args) > 3 else '%Y-%m-%d'}
            elif func_name == 'random_boolean':
                kwargs = {'count': args[0] if len(args) > 0 else 1}
            elif func_name == 'random_color':
                kwargs = {'format': args[0] if len(args) > 0 else 'hex', 'count': args[1] if len(args) > 1 else 1}
            elif func_name == 'random_password':
                kwargs = {'length': args[0] if len(args) > 0 else 12, 'include_uppercase': True, 'include_lowercase': True, 'include_digits': True, 'include_special': True, 'count': args[1] if len(args) > 1 else 1}
            elif func_name == 'random_sequence':
                # 处理序列参数，支持字符串和列表格式
                sequence_arg = args[0] if len(args) > 0 else []
                if isinstance(sequence_arg, str):
                    # 尝试解析字符串为列表
                    if sequence_arg.startswith('[') and sequence_arg.endswith(']'):
                        # JSON格式: ["a","b","c"]
                        try:
                            sequence_arg = json.loads(sequence_arg)
                        except:
                            # 如果JSON解析失败，尝试手动解析
                            sequence_arg = [item.strip().strip('"\'') for item in sequence_arg[1:-1].split(',')]
                    else:
                        # 逗号分隔格式: a,b,c
                        sequence_arg = [item.strip().strip('"\'') for item in sequence_arg.split(',')]
                kwargs = {'sequence': sequence_arg, 'count': args[1] if len(args) > 1 else 1, 'unique': args[2] if len(args) > 2 else False}
            
            result = tool_mapping[func_name](**kwargs)
        
        # 提取 result 字段
        if isinstance(result, dict) and 'result' in result:
            return result['result']
        return result
    
    def _call_test_data_tool(self, func_name, args):
        """调用测试数据工具"""
        tool_mapping = {
            'random_phone': TestDataTools.generate_chinese_phone,
            'random_email': TestDataTools.generate_chinese_email,
            'random_id_card': TestDataTools.generate_id_card,
            'random_name': TestDataTools.generate_chinese_name,
            'random_company': TestDataTools.generate_company_name,
            'random_address': TestDataTools.generate_chinese_address,
            'generate_chinese_name': TestDataTools.generate_chinese_name,
            'generate_chinese_phone': TestDataTools.generate_chinese_phone,
            'generate_chinese_email': TestDataTools.generate_chinese_email,
            'generate_chinese_address': TestDataTools.generate_chinese_address,
            'generate_id_card': TestDataTools.generate_id_card,
            'generate_company_name': TestDataTools.generate_company_name,
            'generate_bank_card': TestDataTools.generate_bank_card,
            'generate_hk_id_card': TestDataTools.generate_hk_id_card,
            'generate_business_license': TestDataTools.generate_business_license,
            'generate_user_profile': TestDataTools.generate_user_profile,
            'generate_coordinates': TestDataTools.generate_coordinates,
        }
        
        # 设置默认参数
        kwargs = {}
        if func_name in ['generate_chinese_name', 'random_name']:
            kwargs = {'gender': args[0] if len(args) > 0 else 'random', 'count': args[1] if len(args) > 1 else 1}
        elif func_name == 'generate_chinese_address':
            kwargs = {'full_address': args[0] if len(args) > 0 else True, 'count': args[1] if len(args) > 1 else 1}
        else:
            kwargs = {'count': args[0] if len(args) > 0 else 1}
        
        if func_name in tool_mapping:
            result = tool_mapping[func_name](**kwargs)
        
        # 提取 result 字段
        if isinstance(result, dict) and 'result' in result:
            return result['result']
        return result
    
    def _call_string_tool(self, func_name, args):
        """调用字符工具"""
        tool_mapping = {
            'remove_whitespace': StringTools.remove_whitespace,
            'replace_string': StringTools.replace_string,
            'word_count': StringTools.word_count,
            'regex_test': StringTools.regex_test,
            'case_convert': StringTools.case_convert,
        }
        
        # 设置默认参数
        kwargs = {}
        if func_name == 'remove_whitespace':
            kwargs = {'text': args[0] if len(args) > 0 else ''}
        elif func_name == 'replace_string':
            kwargs = {'text': args[0] if len(args) > 0 else '', 'old_str': args[1] if len(args) > 1 else '', 'new_str': args[2] if len(args) > 2 else '', 'is_regex': False}
        elif func_name == 'word_count':
            kwargs = {'text': args[0] if len(args) > 0 else ''}
        elif func_name == 'regex_test':
            kwargs = {'pattern': args[0] if len(args) > 0 else '', 'text': args[1] if len(args) > 1 else '', 'flags': args[2] if len(args) > 2 else ''}
        elif func_name == 'case_convert':
            kwargs = {'text': args[0] if len(args) > 0 else '', 'convert_type': args[1] if len(args) > 1 else 'upper'}
        
        if func_name in tool_mapping:
            result = tool_mapping[func_name](**kwargs)
        
        # 提取 result 字段
        if isinstance(result, dict) and 'result' in result:
            return result['result']
        return result
    
    def _call_encoding_tool(self, func_name, args):
        """调用编码工具"""
        tool_mapping = {
            'timestamp_convert': EncodingTools.timestamp_convert,
            'base64_encode': EncodingTools.base64_encode,
            'base64_decode': EncodingTools.base64_decode,
            'url_encode': EncodingTools.url_encode,
            'url_decode': EncodingTools.url_decode,
            'unicode_convert': EncodingTools.unicode_convert,
            'ascii_convert': EncodingTools.ascii_convert,
            'color_convert': EncodingTools.color_convert,
            'base_convert': EncodingTools.base_convert,
            'generate_barcode': EncodingTools.generate_barcode,
            'generate_qrcode': EncodingTools.generate_qrcode,
            'decode_qrcode': EncodingTools.decode_qrcode,
            'image_to_base64': ImageTools.image_to_base64,
            'base64_to_image': ImageTools.base64_to_image,
        }
        
        # 设置默认参数
        kwargs = {}
        if func_name == 'timestamp_convert':
            kwargs = {'timestamp': args[0] if len(args) > 0 else '', 'convert_type': args[1] if len(args) > 1 else 'to_datetime'}
        elif func_name == 'base64_encode':
            kwargs = {'text': args[0] if len(args) > 0 else '', 'encoding': args[1] if len(args) > 1 else 'utf-8'}
        elif func_name == 'base64_decode':
            kwargs = {'text': args[0] if len(args) > 0 else '', 'encoding': args[1] if len(args) > 1 else 'utf-8'}
        elif func_name == 'url_encode':
            kwargs = {'data': args[0] if len(args) > 0 else '', 'encoding': args[1] if len(args) > 1 else 'utf-8'}
        elif func_name == 'url_decode':
            kwargs = {'data': args[0] if len(args) > 0 else '', 'encoding': args[1] if len(args) > 1 else 'utf-8'}
        elif func_name == 'unicode_convert':
            kwargs = {'text': args[0] if len(args) > 0 else '', 'convert_type': args[1] if len(args) > 1 else 'to_unicode'}
        elif func_name == 'ascii_convert':
            kwargs = {'text': args[0] if len(args) > 0 else '', 'convert_type': args[1] if len(args) > 1 else 'to_ascii'}
        elif func_name == 'color_convert':
            kwargs = {'color': args[0] if len(args) > 0 else '', 'from_type': args[1] if len(args) > 1 else 'hex', 'to_type': args[2] if len(args) > 2 else 'rgb'}
        elif func_name == 'base_convert':
            kwargs = {'number': args[0] if len(args) > 0 else 0, 'from_base': args[1] if len(args) > 1 else 10, 'to_base': args[2] if len(args) > 2 else 16}
        elif func_name == 'generate_barcode':
            kwargs = {'data': args[0] if len(args) > 0 else '', 'barcode_type': args[1] if len(args) > 1 else 'code128'}
        elif func_name == 'generate_qrcode':
            kwargs = {'data': args[0] if len(args) > 0 else ''}
        elif func_name == 'decode_qrcode':
            kwargs = {'image_data': args[0] if len(args) > 0 else '', 'image_format': args[1] if len(args) > 1 else 'png'}
        elif func_name == 'image_to_base64':
            kwargs = {'image_data': args[0] if len(args) > 0 else '', 'image_format': args[1] if len(args) > 1 else 'png', 'include_prefix': True}
        elif func_name == 'base64_to_image':
            kwargs = {'base64_str': args[0] if len(args) > 0 else ''}
        
        if func_name in tool_mapping:
            result = tool_mapping[func_name](**kwargs)
            
            # 提取 result 字段
            if isinstance(result, dict) and 'result' in result:
                return result['result']
            return result
        
        return None
    
    def _call_encryption_tool(self, func_name, args):
        """调用加密工具"""
        tool_mapping = {
            'base64': EncryptionTools.base64_encode,
            'md5': EncryptionTools.md5_hash,
            'sha1': EncryptionTools.sha1_hash,
            'sha256': EncryptionTools.sha256_hash,
            'md5_hash': EncryptionTools.md5_hash,
            'sha1_hash': EncryptionTools.sha1_hash,
            'sha256_hash': EncryptionTools.sha256_hash,
            'sha512_hash': EncryptionTools.sha512_hash,
            'hash_comparison': EncryptionTools.hash_comparison,
            'aes_encrypt': EncryptionTools.aes_encrypt,
            'aes_decrypt': EncryptionTools.aes_decrypt,
        }
        
        # 设置默认参数
        kwargs = {}
        if func_name in ['base64', 'md5', 'sha1', 'sha256', 'md5_hash', 'sha1_hash', 'sha256_hash', 'sha512_hash']:
            kwargs = {'text': args[0] if len(args) > 0 else ''}
        elif func_name == 'hash_comparison':
            kwargs = {'text': args[0] if len(args) > 0 else '', 'hash_value': args[1] if len(args) > 1 else '', 'algorithm': args[2] if len(args) > 2 else 'md5'}
        elif func_name == 'aes_encrypt':
            kwargs = {'text': args[0] if len(args) > 0 else '', 'password': args[1] if len(args) > 1 else '', 'mode': args[2] if len(args) > 2 else 'CBC'}
        elif func_name == 'aes_decrypt':
            kwargs = {'encrypted_text': args[0] if len(args) > 0 else '', 'password': args[1] if len(args) > 1 else '', 'mode': args[2] if len(args) > 2 else 'CBC'}
        
        if func_name in tool_mapping:
            result = tool_mapping[func_name](**kwargs)
        
        # 提取 result 字段
        if isinstance(result, dict) and 'result' in result:
            return result['result']
        return result
    
    def _call_crontab_tool(self, func_name, args):
        """调用Crontab工具"""
        tool_mapping = {
            'generate_expression': CrontabTools.generate_expression,
            'parse_expression': CrontabTools.parse_expression,
            'get_next_runs': CrontabTools.get_next_runs,
            'validate_expression': CrontabTools.validate_expression,
        }
        
        # 设置默认参数
        kwargs = {}
        if func_name == 'generate_expression':
            kwargs = {
                'minute': args[0] if len(args) > 0 else '*',
                'hour': args[1] if len(args) > 1 else '*',
                'day': args[2] if len(args) > 2 else '*',
                'month': args[3] if len(args) > 3 else '*',
                'weekday': args[4] if len(args) > 4 else '*'
            }
        elif func_name == 'parse_expression':
            kwargs = {'expression': args[0] if len(args) > 0 else '* * * * *'}
        elif func_name == 'get_next_runs':
            kwargs = {'expression': args[0] if len(args) > 0 else '* * * * *', 'count': args[1] if len(args) > 1 else 5}
        elif func_name == 'validate_expression':
            kwargs = {'expression': args[0] if len(args) > 0 else '* * * * *'}
        
        if func_name in tool_mapping:
            result = tool_mapping[func_name](**kwargs)
            
            # 提取 result 字段
            if isinstance(result, dict) and 'result' in result:
                return result['result']
            # 对于Crontab工具，可能返回expression字段
            if isinstance(result, dict) and 'expression' in result:
                return result['expression']
            return result
        
        return None
    
    # ========== 时间日期函数 ==========
    
    def _timestamp(self):
        """生成当前时间戳（毫秒）
        Returns:
            时间戳（毫秒）
        Example:
            ${timestamp()} -> 1704067200000
        """
        return int(datetime.now().timestamp() * 1000)
    
    def _timestamp_sec(self):
        """生成当前时间戳（秒）
        Returns:
            时间戳（秒）
        Example:
            ${timestamp_sec()} -> 1704067200
        """
        return int(datetime.now().timestamp())
    
    def _datetime(self, format_str='%Y-%m-%d %H:%M:%S'):
        """生成当前日期时间
        Args:
            format_str: 日期时间格式
        Returns:
            格式化的日期时间字符串
        Example:
            ${datetime()} -> "2024-01-01 12:00:00"
            ${datetime(%Y-%m-%d)} -> "2024-01-01"
        """
        return datetime.now().strftime(format_str)
    
    def _date(self, format_str='%Y-%m-%d'):
        """生成当前日期
        Args:
            format_str: 日期格式
        Returns:
            格式化的日期字符串
        Example:
            ${date()} -> "2024-01-01"
        """
        return datetime.now().strftime(format_str)
    
    def _time(self, format_str='%H:%M:%S'):
        """生成当前时间
        Args:
            format_str: 时间格式
        Returns:
            格式化的时间字符串
        Example:
            ${time()} -> "12:00:00"
        """
        return datetime.now().strftime(format_str)
    
    def _date_offset(self, days=0, hours=0, minutes=0, format_str='%Y-%m-%d %H:%M:%S'):
        """生成偏移后的日期时间
        Args:
            days: 天数偏移
            hours: 小时偏移
            minutes: 分钟偏移
            format_str: 日期时间格式
        Returns:
            格式化的日期时间字符串
        Example:
            ${date_offset(1)} -> "2024-01-02 12:00:00"
            ${date_offset(0, 1)} -> "2024-01-01 13:00:00"
        """
        dt = datetime.now() + timedelta(days=days, hours=hours, minutes=minutes)
        return dt.strftime(format_str)


# 创建全局解析器实例
_resolver = VariableResolver()


def resolve_variables(text):
    """解析文本中的变量表达式
    Args:
        text: 包含变量表达式的文本
    Returns:
        解析后的文本
    """
    return _resolver.resolve(text)
