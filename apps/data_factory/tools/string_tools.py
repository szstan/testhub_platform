# -*- coding: utf-8 -*-
# -----------------------------
# @Author    : 影子
# @Time      : 2026/1/18 22:16
# @Software  : PyCharm
# @FileName  : string_tools.py
# -----------------------------
"""
字符处理工具
"""
import re
import regex as bounded_regex
import json
import difflib
import logging
from typing import Dict, List, Any

from .regex_limits import (
    RegexBudgetError, compile_bounded_pattern, expand_bounded_replacement,
    replacement_template,
)

logger = logging.getLogger(__name__)
REGEX_TIMEOUT_SECONDS = 0.05
MAX_REGEX_PATTERN_LENGTH = 2048
MAX_REGEX_TEXT_LENGTH = 64_000
MAX_REGEX_MATCHES = 1000
MAX_REGEX_GROUPS = 32
MAX_REGEX_OUTPUT_LENGTH = 1_000_000


def _regex_input_error(pattern, text):
    if not isinstance(pattern, str) or not isinstance(text, str):
        return '正则表达式和文本必须是字符串'
    if len(pattern) > MAX_REGEX_PATTERN_LENGTH or len(text) > MAX_REGEX_TEXT_LENGTH:
        return '正则表达式或文本超过长度上限'
    return None


class StringTools:
    """字符工具类"""

    @staticmethod
    def remove_whitespace(text: str) -> Dict[str, Any]:
        """去除空格和换行"""
        return {
            'result': re.sub(r'\s+', '', text),
            'original_length': len(text),
            'new_length': len(re.sub(r'\s+', '', text))
        }

    @staticmethod
    def replace_string(text: str, old_str: str, new_str: str, is_regex: bool = False) -> Dict[str, Any]:
        """Replace text under the same bounded policy as regex testing."""
        error = _regex_input_error(old_str, text)
        if error or not isinstance(new_str, str) or len(new_str) > MAX_REGEX_TEXT_LENGTH:
            return {'error': error or '替换文本超过长度上限或类型无效'}
        if not is_regex:
            replacements = text.count(old_str)
            if len(text) + replacements * (len(new_str) - len(old_str)) > MAX_REGEX_OUTPUT_LENGTH:
                return {'error': '替换结果超过大小上限'}
            return {'result': text.replace(old_str, new_str), 'replacements': replacements}
        try:
            pattern = compile_bounded_pattern(old_str, max_groups=MAX_REGEX_GROUPS)
            template = replacement_template(new_str, pattern)
            replacements = 0
            output_size = 0
            last_end = 0

            def replacement(match):
                nonlocal replacements, output_size, last_end
                replacements += 1
                if replacements > MAX_REGEX_MATCHES:
                    raise ValueError('匹配数量超过上限')
                gap = match.start() - last_end
                available = MAX_REGEX_OUTPUT_LENGTH - output_size - gap - (len(text) - match.end())
                expanded = expand_bounded_replacement(match, template, available)
                output_size += gap + len(expanded)
                last_end = match.end()
                return expanded

            result = pattern.sub(replacement, text, timeout=REGEX_TIMEOUT_SECONDS)
            return {'result': result, 'replacements': replacements}
        except TimeoutError:
            return {'error': '正则表达式执行超时'}
        except RegexBudgetError as exc:
            return {'error': str(exc)}
        except (bounded_regex.error, re.error, ValueError, IndexError, RecursionError):
            return {'error': '正则表达式、替换内容无效或结果超过上限'}

    @staticmethod
    def escape_string(text: str, escape_type: str = 'json') -> Dict[str, Any]:
        """字符串转义"""
        escape_mapping = {
            'json': json.dumps,
            'html': lambda x: x.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'),
            'url': lambda x: x.replace(' ', '%20').replace('/', '%2F'),
            'xml': lambda x: x.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
        }

        if escape_type not in escape_mapping:
            logger.error(f'不支持的转义类型: {escape_type}')
            return {'error': f'不支持的转义类型: {escape_type}'}

        return {
            'result': escape_mapping[escape_type](text)
        }

    @staticmethod
    def unescape_string(text: str, unescape_type: str = 'json') -> Dict[str, Any]:
        """字符串反转义"""
        if unescape_type == 'json':
            try:
                return {'result': json.loads(text)}
            except json.JSONDecodeError as e:
                logger.error(f'JSON解析错误: {str(e)}')
                return {'error': f'JSON解析错误: {str(e)}'}
        elif unescape_type == 'html':
            html_unescape = {
                '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&nbsp;': ' '
            }
            result = text
            for key, value in html_unescape.items():
                result = result.replace(key, value)
            return {'result': result}
        elif unescape_type == 'url':
            import urllib.parse
            return {'result': urllib.parse.unquote(text)}
        else:
            logger.error(f'不支持的反转义类型: {unescape_type}')
            return {'error': f'不支持的反转义类型: {unescape_type}'}

    @staticmethod
    def word_count(text: str) -> Dict[str, Any]:
        """字数统计"""
        # 统计中文字符
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        # 统计英文单词
        english_words = len(re.findall(r'\b[a-zA-Z]+\b', text))
        # 统计数字
        numbers = len(re.findall(r'\d+', text))
        # 统计标点符号
        punctuation = len(re.findall(r'[^\w\s\u4e00-\u9fff]', text))

        result = {
            'total_length': len(text),
            'chinese_chars': chinese_chars,
            'english_words': english_words,
            'numbers': numbers,
            'punctuation': punctuation,
            'lines': len(text.split('\n')),
            'paragraphs': len([p for p in text.split('\n') if p.strip()])
        }

        return {
            'result': len(text),
            **result
        }

    @staticmethod
    def text_diff(text1: str, text2: str) -> Dict[str, Any]:
        """文本对比"""
        if text1 is None:
            text1 = ""
        if text2 is None:
            text2 = ""

        lines1 = text1.splitlines()
        lines2 = text2.splitlines()

        differ = difflib.Differ()
        diff = list(differ.compare(lines1, lines2))

        added = 0
        removed = 0
        unchanged = 0

        for line in diff:
            if line.startswith('+ '):
                added += 1
            elif line.startswith('- '):
                removed += 1
            elif line.startswith('  '):
                unchanged += 1

        result = {
            'diff': diff,
            'added': added,
            'removed': removed,
            'unchanged': unchanged,
            'total_changes': added + removed
        }

        return {
            'result': added + removed + unchanged,
            **result
        }

    @staticmethod
    def regex_test(pattern: str, text: str, flags: str = '') -> Dict[str, Any]:
        """Test an untrusted regex with bounded input, time, and output."""
        error = _regex_input_error(pattern, text)
        if error or not isinstance(flags, str) or len(flags) > 16:
            return {'error': error or '正则标志无效', 'is_valid': False}
        try:
            flag_mapping = {
                'i': (bounded_regex.IGNORECASE, re.IGNORECASE),
                'm': (bounded_regex.MULTILINE, re.MULTILINE),
                's': (bounded_regex.DOTALL, re.DOTALL),
                'u': (bounded_regex.UNICODE, re.UNICODE),
                'x': (bounded_regex.VERBOSE, re.VERBOSE),
            }
            regex_flags = 0
            stdlib_flags = 0
            for flag in flags:
                if flag in flag_mapping:
                    regex_flag, stdlib_flag = flag_mapping[flag]
                    regex_flags |= regex_flag
                    stdlib_flags |= stdlib_flag
            compiled = compile_bounded_pattern(
                pattern, regex_flags, stdlib_flags=stdlib_flags, max_groups=MAX_REGEX_GROUPS,
            )
            matches = []
            first = None
            output_size = 0
            for match in compiled.finditer(text, timeout=REGEX_TIMEOUT_SECONDS):
                if len(matches) >= MAX_REGEX_MATCHES:
                    return {'error': '匹配数量超过上限', 'is_valid': True}
                if first is None:
                    first = match
                groups = match.groups(default='')
                output_size += sum(map(len, groups)) if groups else len(match.group())
                if output_size > MAX_REGEX_OUTPUT_LENGTH:
                    return {'error': '匹配结果超过大小上限', 'is_valid': True}
                matches.append(groups[0] if len(groups) == 1 else groups if groups else match.group())
            return {
                'result': True, 'matches': matches, 'match_count': len(matches),
                'groups': first.groups() if first else [],
                'groupdict': first.groupdict() if first else {}, 'is_valid': True,
            }
        except TimeoutError:
            return {'error': '正则表达式执行超时', 'is_valid': True}
        except RegexBudgetError as exc:
            return {'error': str(exc), 'is_valid': True}
        except (bounded_regex.error, re.error, RecursionError, ValueError):
            return {'error': '正则表达式无效', 'is_valid': False}

    @staticmethod
    def case_convert(text: str, convert_type: str) -> Dict[str, Any]:
        """大小写转换"""
        convert_functions = {
            'upper': lambda s: s.upper(),
            'lower': lambda s: s.lower(),
            'capitalize': lambda s: s.capitalize(),
            'title': lambda s: s.title(),
            'swapcase': lambda s: s.swapcase()
        }

        if convert_type not in convert_functions:
            logger.error(f'不支持的转换类型: {convert_type}')
            return {'error': f'不支持的转换类型: {convert_type}'}

        return {
            'result': convert_functions[convert_type](text),
            'original': text
        }

    @staticmethod
    def string_format(text: str, format_type: str) -> Dict[str, Any]:
        """字符串格式化"""
        if format_type == 'trim':
            return {'result': text.strip()}
        elif format_type == 'reverse':
            return {'result': text[::-1]}
        elif format_type == 'split':
            return {'result': text.split()}
        elif format_type == 'join':
            return {'result': ''.join(text.split())}
        else:
            logger.error(f'不支持的格式化类型: {format_type}')
            return {'error': f'不支持的格式化类型: {format_type}'}
