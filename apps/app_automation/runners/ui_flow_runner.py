# -*- coding: utf-8 -*-
"""
UI Flow 执行器 - 将 UI Flow JSON 转换为 Airtest 动作并执行
"""
import os
import time
import logging
import json
import math
import re
import copy
import base64
import hashlib
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit
import regex as bounded_regex
from django.conf import settings
from django.db import transaction
from ..runtime_config import validate_base_url
from ..constants import APP_API_REQUEST_MAX_TIMEOUT_SECONDS
from .. import appium_locator, device_actions, native_locator, secret_input
from ..appium_runtime import AppiumOutcomeUnknown
from ..bounded_inference import BoundedInference, InferenceBusy, InferenceTimeout
from apps.core.http_transport import guarded_request
from apps.core.network import UnsafeOutboundURL, validate_outbound_url

from airtest.aircv import utils as aircv_utils
from airtest.core.api import (
    Template,
    touch,
    sleep,
    swipe,
    snapshot,
    double_click,
    G,
    text as airtest_text,
)

# 导入 OCR 工具
try:
    from ..utils.ocr_helper import get_ocr_helper
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

try:
    import allure
    ALLURE_AVAILABLE = True
except ImportError:
    ALLURE_AVAILABLE = False

logger = logging.getLogger(__name__)
_MISSING = object()
_MAX_RETRY_TIMES = 10
_MAX_RETRY_INTERVAL = 10.0
_MAX_LOOP_COUNT = 10_000
_MAX_LOOP_INTERVAL = 300.0
_MAX_FOREACH_ASSERT_WAIT = 300.0
_MAX_NATIVE_STATE_PROBE_TIMEOUT = 2.0
_MAX_IMAGE_LOOKUP_TIMEOUT = 2.0
_IMAGE_INFERENCE = BoundedInference('app-image-inference')
_ALLOWED_RUNTIME_OPTIONS = {
    'base_url', 'retry_times', 'retry_interval', 'ocr_languages', 'ocr_use_gpu', 'stop_on_error',
    'package_name',
}
_SUPPORTED_OCR_LANGUAGES = {'en', 'ch_sim'}
_SENSITIVE_OUTPUT_KEY = re.compile(
    r'password|passwd|secret|token|credential|authorization|cookie|session|api[_-]?key|private[_-]?key',
    re.IGNORECASE,
)
# foreach_assert is excluded because it may click before evaluating its assertion.
_READ_ONLY_BUSINESS_ASSERTION_ACTIONS = frozenset({
    'assert', 'assert_element', 'assert_text', 'assert_value', 'assert_response',
})


class ReadOnlyBusinessAssertionFailure(AssertionError):
    """Failed assertion from a built-in step that cannot mutate the device."""


class AppiumPartialInputError(RuntimeError):
    """An earlier part of the input step has already changed the device."""


_APPIUM_ELEMENT_ACTIONS = frozenset({
    'click', 'touch', 'double_click', 'long_press', 'input', 'text', 'clear_input',
    'wait', 'wait_element', 'assert_element', 'assert_text', 'extract_text',
})


def _safe_regex_search(pattern, value):
    pattern, value = str(pattern), str(value)
    if len(pattern) > 4096 or len(value) > 100_000:
        raise ValueError('Regex pattern or input exceeds the supported limit')
    try:
        return bool(bounded_regex.search(pattern, value, timeout=0.2))
    except TimeoutError:
        raise ValueError('Regex evaluation exceeded the time limit') from None
    except bounded_regex.error:
        raise ValueError('Invalid regex condition pattern') from None


class UiFlowRunner:
    """Execute a UI flow using an optional Appium session and visual tools."""

    appium_driver = None
    device_guard = None
    load_visual_device = None
    diagnostic_root_index = None
    diagnostic_case_revision = None
    _partial_click_chain_failures = 0
    
    def __init__(
        self,
        image_base_dir: Optional[str] = None,
        username: Optional[str] = None,
        execution_id: Optional[Any] = None,
        appium_driver=None,
        device_guard=None,
        load_visual_device=None,
        diagnostic_root_index=None,
        diagnostic_case_revision=None,
    ):
        """
        初始化 UiFlowRunner
        
        Args:
            image_base_dir: 图片元素基础目录
            username: 执行用户名，用于截图目录分组
        """
        self.execution_id = execution_id
        self.appium_driver = appium_driver
        self.device_guard = device_guard
        self.load_visual_device = load_visual_device
        self.diagnostic_root_index = diagnostic_root_index
        self.diagnostic_case_revision = diagnostic_case_revision
        if image_base_dir:
            self.image_base_dir = image_base_dir
        else:
            # 使用统一的 Template 目录作为图片基础目录
            self.image_base_dir = os.path.join(settings.BASE_DIR, 'apps', 'app_automation', 'Template')
        
        screenshot_root = os.path.realpath(
            os.path.join(settings.MEDIA_ROOT, 'app-automation', 'screenshots')
        )
        raw_username = str(username or 'unknown')
        safe_username = self._safe_filename(raw_username)
        if safe_username != raw_username:
            user_hash = hashlib.sha256(raw_username.encode('utf-8', errors='replace')).hexdigest()[:16]
            safe_username = f'{safe_username}_{user_hash}'
        self.screenshots_dir = os.path.realpath(os.path.join(screenshot_root, safe_username))
        try:
            if os.path.commonpath((screenshot_root, self.screenshots_dir)) != screenshot_root:
                raise ValueError('Screenshot directory must remain inside its configured root')
        except ValueError as exc:
            raise ValueError('Screenshot directory must remain inside its configured root') from exc

        os.makedirs(self.image_base_dir, exist_ok=True)
        os.makedirs(self.screenshots_dir, exist_ok=True)
        
        # 上下文变量
        self.context: Dict[str, Any] = {
            'global': {},
            'local': {},
            'outputs': {},
        }
        
        # 运行时配置
        self.runtime: Dict[str, Any] = {
            'retry_times': 0,
            'retry_interval': 0.5,
            'ocr_languages': ['en'],
            'ocr_use_gpu': False,
        }
        
        # OCR 工具（延迟初始化）
        self._ocr_helper = None
        
        logger.info(f"初始化UiFlowRunner，图片目录: {self.image_base_dir}")
    
    def run(
        self,
        ui_flow: List[Dict[str, Any]],
        variables: Optional[List[Dict[str, Any]]] = None,
        runtime: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        执行 UI Flow
        
        Args:
            ui_flow: UI Flow 配置列表
            variables: 变量列表
            runtime: 运行时配置
            progress_callback: 进度回调函数，签名为 callback(current_step, total_steps, step_name, status)
                其中 status 为 'running' | 'passed' | 'failed'
            
        Returns:
            执行结果字典
        """
        if not isinstance(ui_flow, list):
            raise ValueError("ui_flow 必须是列表")
        
        # 初始化上下文
        self._init_context(variables, runtime)
        self._assertions_executed = 0
        self._ui_flow_unsafe_failure_seen = False
        self._ui_flow_proof_token = object()
        self._flow_step_path = 'ui_flow'
        self._appium_screen_sensitive = False
        
        # 执行所有步骤
        total_steps = len(ui_flow)
        passed_steps = 0
        failed_steps = 0
        
        logger.info(f"开始执行 UI Flow，共 {total_steps} 个步骤")
        
        for idx, step in enumerate(ui_flow, 1):
            source_index = self.diagnostic_root_index + 1 if self.diagnostic_root_index is not None else idx
            step_name = step.get('name', step.get('type', 'unknown'))
            step_type = step.get('type', 'unknown')

            if progress_callback:
                try:
                    progress_callback(idx, total_steps, self._safe_log_value(step_name), 'running')
                except Exception as cb_err:
                    logger.debug('Progress callback failed (%s)', type(cb_err).__name__)

            try:
                logger.info('Executing step %s/%s', idx, total_steps)
                step_title = f"步骤{idx}"
                if ALLURE_AVAILABLE:
                    with allure.step(step_title):
                        self._execute_step_at(step, f'[{source_index}]')
                else:
                    self._execute_step_at(step, f'[{source_index}]')
                passed_steps += 1

                if progress_callback:
                    try:
                        progress_callback(idx, total_steps, self._safe_log_value(step_name), 'passed')
                    except Exception as cb_err:
                        logger.debug('Progress callback failed (%s)', type(cb_err).__name__)

            except Exception as e:
                self._sanitize_exception_for_reporting(e)
                self._record_appium_failure(e, safe_path=f'ui_flow[{source_index}]')
                if self._is_uncertain_device_error(e):
                    self._quarantine_uncertain_device_error(e)
                    raise
                logger.error('Step %s failed (%s)', idx, type(e).__name__)
                failed_steps += 1
                safe_step_type = self._safe_log_value(step_type)
                self._attach_allure(f"step-{idx}-{safe_step_type}-error", step)

                if progress_callback:
                    try:
                        progress_callback(idx, total_steps, self._safe_log_value(step_name), 'failed')
                    except Exception as cb_err:
                        logger.debug('Progress callback failed (%s)', type(cb_err).__name__)

                if runtime and runtime.get('stop_on_error', False):
                    if self._is_completed_read_only_assertion_failure(e):
                        raise ReadOnlyBusinessAssertionFailure(
                            'Read-only business assertion failed'
                        ) from e
                    raise
        
        logger.info(f"UI Flow 执行完成，通过: {passed_steps}，失败: {failed_steps}")
        
        return {
            'total': total_steps,
            'passed': passed_steps,
            'failed': failed_steps,
            'assertions': self._assertions_executed,
            'outputs': self._sanitize_sensitive_value(self.context.get('outputs', {})),
        }
    
    def _init_context(self, variables: Optional[List[Dict[str, Any]]] = None, runtime: Optional[Dict[str, Any]] = None):
        """初始化上下文"""
        self.context = {
            'global': {},
            'local': {},
            'outputs': {},
            'steps': {},
        }
        self._sensitive_variable_names = set()
        self._sensitive_variable_paths = {}
        self._sensitive_variable_values = set()
        self.runtime = {
            'retry_times': 0,
            'retry_interval': 0.5,
            'ocr_languages': ['en'],
            'ocr_use_gpu': False,
        }
        if runtime is not None:
            if not isinstance(runtime, dict):
                raise ValueError('runtime must be a JSON object')
            if set(runtime) - _ALLOWED_RUNTIME_OPTIONS:
                raise ValueError('runtime contains unsupported options')
            self.runtime.update(runtime)

        retry_times = self.runtime.get('retry_times', 0)
        if isinstance(retry_times, bool) or not isinstance(retry_times, int) or not 0 <= retry_times <= _MAX_RETRY_TIMES:
            raise ValueError(f'retry_times must be an integer from 0 to {_MAX_RETRY_TIMES}')
        retry_interval = self.runtime.get('retry_interval', 0.5)
        if (
            isinstance(retry_interval, bool)
            or not isinstance(retry_interval, (int, float))
            or retry_interval < 0
            or retry_interval > _MAX_RETRY_INTERVAL
            or not math.isfinite(retry_interval)
        ):
            raise ValueError(f'retry_interval must be from 0 to {_MAX_RETRY_INTERVAL} seconds')

        languages = self.runtime.get('ocr_languages', ['en'])
        if (
            not isinstance(languages, (list, tuple))
            or not 1 <= len(languages) <= len(_SUPPORTED_OCR_LANGUAGES)
            or any(not isinstance(language, str) or language not in _SUPPORTED_OCR_LANGUAGES for language in languages)
            or len(set(languages)) != len(languages)
        ):
            raise ValueError('ocr_languages must be a unique non-empty list of en/ch_sim')
        self.runtime['ocr_languages'] = list(languages)
        if not isinstance(self.runtime.get('ocr_use_gpu', False), bool):
            raise ValueError('ocr_use_gpu must be a boolean')
        if not isinstance(self.runtime.get('stop_on_error', False), bool):
            raise ValueError('stop_on_error must be a boolean')
        if (
            self.runtime.get('package_name') is not None
            and not isinstance(self.runtime.get('package_name'), str)
        ):
            raise ValueError('package_name must be a string')
        base_url = validate_base_url(self.runtime.get('base_url', ''))

        if variables:
            self._load_variables(variables)
        if base_url:
            self.context['global']['API_BASE_URL'] = base_url
    
    def _load_variables(self, variables: List[Dict[str, Any]]):
        """加载变量到上下文"""
        for item in variables:
            if not isinstance(item, dict):
                continue
            
            name = item.get('name')
            if not name:
                continue
            
            scope = str(item.get('scope', 'local')).lower()
            if scope not in self.context:
                scope = 'local'
            value = item.get('value')
            self._set_variable(name, value, scope, sensitive=item.get('sensitive') is True)
    
    def _remember_sensitive_values(self, value):
        if isinstance(value, dict):
            for child in value.values():
                self._remember_sensitive_values(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                self._remember_sensitive_values(child)
        elif value is not None and not isinstance(value, bool):
            rendered = str(value)
            if rendered:
                self._sensitive_variable_values.add(rendered)

    def _sensitive_paths_for_reference(self, reference):
        if not isinstance(reference, str):
            return set()
        reference = reference.strip()
        if reference.startswith('{{') and reference.endswith('}}'):
            reference = reference[2:-2].strip()
        elif reference.startswith('${') and reference.endswith('}'):
            reference = reference[2:-1].strip()
        parts = self._parse_path(reference)
        if not parts:
            return set()

        scope = None
        if parts[0] in {'local', 'global', 'outputs', 'steps'}:
            if len(parts) < 2:
                return set()
            scope, name, selected_path = parts[0], parts[1], tuple(parts[2:])
        else:
            scope = next((candidate for candidate in ('local', 'global', 'outputs', 'steps')
                          if reference in self.context.get(candidate, {})), None)
            if scope is not None:
                name, selected_path = reference, ()
            else:
                scope = next((candidate for candidate in ('local', 'global', 'outputs', 'steps')
                              if parts[0] in self.context.get(candidate, {})), None)
                if scope is None:
                    return set()
                name, selected_path = parts[0], tuple(parts[1:])

        key = (scope, str(name))
        if key in getattr(self, '_sensitive_variable_names', set()):
            return {()}
        tainted_paths = getattr(self, '_sensitive_variable_paths', {}).get(key, set())
        result = set()
        for tainted_path in tainted_paths:
            if tainted_path[:len(selected_path)] == selected_path:
                result.add(tainted_path[len(selected_path):])
            elif selected_path[:len(tainted_path)] == tainted_path:
                result.add(())
        return result

    @staticmethod
    def _project_sensitive_paths(tainted_paths, selected_path):
        selected_path = tuple(selected_path)
        result = set()
        for tainted_path in tainted_paths:
            if tainted_path[:len(selected_path)] == selected_path:
                result.add(tainted_path[len(selected_path):])
            elif selected_path[:len(tainted_path)] == tainted_path:
                result.add(())
        return result

    def _references_sensitive_variable(self, value):
        if isinstance(value, dict):
            return any(
                self._references_sensitive_variable(key)
                or self._references_sensitive_variable(child)
                for key, child in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(self._references_sensitive_variable(child) for child in value)
        if not isinstance(value, str):
            return False
        references = re.finditer(r'\{\{\s*([^}]+)\s*\}\}|\$\{\s*([^}]+)\s*\}', value)
        for match in references:
            reference = (match.group(1) or match.group(2)).strip()
            if match.group(1) is None and self._is_dynamic_function(reference):
                continue
            parts = self._parse_path(reference)
            if self._sensitive_paths_for_reference(reference):
                return True
            if any(_SENSITIVE_OUTPUT_KEY.search(part) for part in parts):
                return True
        return False

    def _contains_sensitive_value(self, value):
        if isinstance(value, dict):
            return any(
                self._contains_sensitive_value(key)
                or self._contains_sensitive_value(child)
                for key, child in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(self._contains_sensitive_value(child) for child in value)
        if value is None:
            return False
        rendered = str(value)
        return any(secret in rendered for secret in getattr(self, '_sensitive_variable_values', set()))

    def _sanitize_sensitive_value(self, value):
        if isinstance(value, dict):
            return {
                self._sanitize_sensitive_value(key): self._sanitize_sensitive_value(child)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [self._sanitize_sensitive_value(child) for child in value]
        if isinstance(value, tuple):
            return tuple(self._sanitize_sensitive_value(child) for child in value)
        if isinstance(value, str) and (
            self._references_sensitive_variable(value) or self._contains_sensitive_value(value)
        ):
            return '[REDACTED]'
        return value

    def _safe_log_value(self, value):
        return self._sanitize_sensitive_value(value)

    def _sanitize_exception_for_reporting(self, error, seen=None):
        """Redact exception messages and structured attributes before reporting."""
        if not isinstance(error, BaseException):
            return
        seen = set() if seen is None else seen
        if id(error) in seen:
            return
        seen.add(id(error))

        try:
            args = tuple(self._sanitize_sensitive_value(arg) for arg in error.args)
            if args != error.args:
                error.args = args
        except Exception:
            pass

        try:
            attributes = getattr(error, '__dict__', None)
            if isinstance(attributes, dict):
                safe_attributes = self._sanitize_sensitive_value(attributes)
                if safe_attributes != attributes:
                    attributes.clear()
                    attributes.update(safe_attributes)
        except Exception:
            pass

        attribute_names = {'errno', 'strerror', 'filename', 'filename2', 'winerror'}
        for error_type in type(error).__mro__:
            slots = error_type.__dict__.get('__slots__', ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot_name in slots:
                if slot_name in {'__dict__', '__weakref__'}:
                    continue
                if slot_name.startswith('__') and not slot_name.endswith('__'):
                    slot_name = f"_{error_type.__name__.lstrip('_')}{slot_name}"
                attribute_names.add(slot_name)

        for attribute_name in attribute_names:
            try:
                value = getattr(error, attribute_name)
                safe_value = self._sanitize_sensitive_value(value)
                if safe_value != value:
                    setattr(error, attribute_name, safe_value)
            except Exception:
                pass

        notes = getattr(error, '__notes__', None)
        if isinstance(notes, list):
            try:
                error.__notes__ = [self._sanitize_sensitive_value(note) for note in notes]
            except Exception:
                pass

        try:
            message = str(error)
        except Exception:
            message = ''
        safe_message = self._sanitize_sensitive_value(message)
        if safe_message != message:
            try:
                error.args = (safe_message,)
            except Exception:
                pass

        self._sanitize_exception_for_reporting(error.__cause__, seen)
        self._sanitize_exception_for_reporting(error.__context__, seen)

    def _is_completed_read_only_assertion_failure(self, error):
        return (
            isinstance(error, AssertionError)
            and getattr(error, '_ui_flow_read_only_assertion_failure', False) is True
            and getattr(error, '_ui_flow_read_only_assertion_run', None)
            is getattr(self, '_ui_flow_proof_token', None)
            and not getattr(self, '_ui_flow_unsafe_failure_seen', False)
        )

    def _track_step_failure(self, action, error):
        if (
            getattr(error, '_ui_flow_read_only_assertion_run', None)
            is getattr(self, '_ui_flow_proof_token', None)
        ):
            return
        if (
            isinstance(error, AssertionError)
            and action in _READ_ONLY_BUSINESS_ASSERTION_ACTIONS
            and not getattr(self, '_ui_flow_unsafe_failure_seen', False)
        ):
            try:
                error._ui_flow_read_only_assertion_failure = True
                error._ui_flow_read_only_assertion_run = self._ui_flow_proof_token
                return
            except Exception:
                pass
        self._ui_flow_unsafe_failure_seen = True

    def _set_variable(
        self, name: str, value: Any, scope: str = 'local', *, sensitive=None, sensitive_paths=None,
    ):
        """Set a variable and replace or preserve its sensitivity contract."""
        if scope not in self.context:
            scope = 'local'
        self.context[scope][name] = value
        key = (scope, str(name))
        names = getattr(self, '_sensitive_variable_names', set())
        paths_by_variable = getattr(self, '_sensitive_variable_paths', {})
        if sensitive is True:
            names.add(key)
            paths_by_variable.pop(key, None)
            self._remember_sensitive_values(value)
        elif sensitive is False:
            names.discard(key)
            paths_by_variable.pop(key, None)
        if sensitive_paths is not None:
            normalized = {tuple(path) for path in sensitive_paths}
            if () in normalized:
                names.add(key)
                paths_by_variable.pop(key, None)
                self._remember_sensitive_values(value)
            else:
                names.discard(key)
                if normalized:
                    paths_by_variable[key] = normalized
                    for path in normalized:
                        current = value
                        for part in path:
                            current = self._lookup_path_value(current, str(part))
                            if current is _MISSING:
                                break
                        if current is not _MISSING:
                            self._remember_sensitive_values(current)
                else:
                    paths_by_variable.pop(key, None)

    def _sensitive_output_paths(self, value):
        paths = set()
        budget = {'nodes': 0}

        def visit(item, path=(), depth=0):
            budget['nodes'] += 1
            if budget['nodes'] > 10_000 or depth > 32:
                paths.add(())
                return
            if isinstance(item, dict):
                for key, child in item.items():
                    child_path = path + (str(key),)
                    if _SENSITIVE_OUTPUT_KEY.search(str(key)):
                        paths.add(child_path)
                    else:
                        visit(child, child_path, depth + 1)
            elif isinstance(item, (list, tuple)):
                for index, child in enumerate(item):
                    visit(child, path + (str(index),), depth + 1)

        visit(value)
        return paths
    
    def _get_variable(self, name: str, scope: Optional[str] = None) -> Any:
        """获取变量"""
        if scope:
            return self.context.get(scope, {}).get(name)
        
        # 按优先级查找：local -> global -> outputs
        for s in ['local', 'global', 'outputs']:
            if name in self.context.get(s, {}):
                return self.context[s][name]
        
        return None
    
    def _resolve_reference(self, reference: str) -> Any:
        """Resolve a scoped variable or a nested dict/list path."""
        reference = reference.strip()
        parts = self._parse_path(reference)
        if not parts:
            return _MISSING
        if parts[0] in {'local', 'global', 'outputs', 'steps'}:
            current = self.context.get(parts[0], _MISSING)
            parts = parts[1:]
            if current is _MISSING:
                return _MISSING
            for part in parts:
                current = self._lookup_path_value(current, part)
                if current is _MISSING:
                    return _MISSING
            return current

        for scope in ('local', 'global', 'outputs'):
            values = self.context.get(scope, {})
            if reference in values:
                return values[reference]

        for scope in ('local', 'global', 'outputs'):
            values = self.context.get(scope, {})
            if parts[0] not in values:
                continue
            current = values[parts[0]]
            for part in parts[1:]:
                current = self._lookup_path_value(current, part)
                if current is _MISSING:
                    break
            if current is not _MISSING:
                return current
        return _MISSING

    @staticmethod
    def _lookup_path_value(value: Any, key: str) -> Any:
        if isinstance(value, dict):
            return value.get(key, _MISSING)
        if isinstance(value, (list, tuple)):
            try:
                return value[int(key)]
            except (ValueError, IndexError):
                return _MISSING
        return _MISSING

    @staticmethod
    def _is_dynamic_function(expression: str) -> bool:
        return bool(re.fullmatch(r'[A-Za-z_]\w*\s*\(.*\)', expression.strip()))

    def _render_value(self, value: Any) -> Any:
        """Render scoped variable references, preserving typed full-value references."""
        if isinstance(value, str):
            pattern = re.compile(r'\{\{\s*([^}]+)\s*\}\}|\$\{\s*([^}]+)\s*\}')
            full_match = pattern.fullmatch(value)
            if full_match:
                expression = (full_match.group(1) or full_match.group(2)).strip()
                if full_match.group(1) is None and self._is_dynamic_function(expression):
                    return value
                resolved = self._resolve_reference(expression)
                if resolved is _MISSING:
                    raise ValueError(f"未解析的变量引用: {expression}")
                return resolved

            def replace_var(match):
                expression = (match.group(1) or match.group(2)).strip()
                if match.group(1) is None and self._is_dynamic_function(expression):
                    return match.group(0)
                resolved = self._resolve_reference(expression)
                if resolved is _MISSING:
                    raise ValueError(f"未解析的变量引用: {expression}")
                return str(resolved)

            return pattern.sub(replace_var, value)

        if isinstance(value, dict):
            return {key: self._render_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._render_value(item) for item in value]
        return value

    def _record_step_output(self, step: Dict[str, Any], output: Any) -> None:
        sensitive_paths = self._sensitive_output_paths(output)
        step_id = step.get('id', step.get('step_id'))
        if step_id is not None:
            self._set_variable(
                str(step_id), output, 'steps', sensitive_paths=sensitive_paths,
            )
        if output is not None:
            self._set_variable(
                'last', output, 'outputs', sensitive_paths=sensitive_paths,
            )
    
    @staticmethod
    def _is_uncertain_device_error(error):
        if isinstance(error, (device_actions.DeviceActionUncertainError, AppiumOutcomeUnknown)):
            return True
        return (
            isinstance(error, secret_input.SecretInputError)
            and not isinstance(error, (
                secret_input.SecretInputValidationError,
                secret_input.SecretInputDeviceError,
            ))
        )

    def _quarantine_uncertain_device_error(self, error):
        if getattr(error, '_ui_flow_quarantine_attempted', False):
            return
        try:
            self._quarantine_execution_device()
        except Exception as quarantine_error:
            logger.error('Unable to quarantine device after uncertain action (%s)', type(quarantine_error).__name__)
        try:
            error._ui_flow_quarantine_attempted = True
        except Exception:
            pass

    @staticmethod
    def _validate_assert_value_left(value):
        if value is _MISSING or value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError('assert_value requires a non-blank left operand')

    @staticmethod
    def _validate_assert_response_config(source, path):
        if (
            not isinstance(source, str) or not source.strip()
            or not isinstance(path, str) or not path.strip()
        ):
            raise ValueError('assert_response requires configured source and path')

    def _execute_step_at(self, step, segment):
        parent_path = getattr(self, '_flow_step_path', 'ui_flow')
        self._flow_step_path = parent_path + segment
        started = time.monotonic()
        try:
            return self._execute_step(step)
        except Exception as error:
            if self.appium_driver is not None and not hasattr(error, '_appium_failed_step'):
                error._appium_failed_step = (
                    self._safe_log_value(step.get('name') or step.get('type') or 'UI Flow'),
                    self._flow_step_path,
                    round((time.monotonic() - started) * 1000),
                )
            raise
        finally:
            self._flow_step_path = parent_path

    def _execute_children(self, steps, field):
        for index, child in enumerate(steps, 1):
            self._execute_step_at(child, f'.{field}[{index}]')

    def _record_appium_failure(self, error, *, safe_path):
        if self.appium_driver is None or self.execution_id is None:
            return
        try:
            from ..app_flow_diagnostics import FLOW_FAILURE_CODES, record_flow_failure

            default_code = 'assertion_failed' if isinstance(error, AssertionError) else 'step_failed'
            code = getattr(error, 'code', default_code)
            if not isinstance(code, str) or code not in FLOW_FAILURE_CODES:
                code = default_code
            name, path, duration = getattr(error, '_appium_failed_step', ('UI Flow', safe_path, 0))
            screenshot_path = None
            if not self._is_uncertain_device_error(error) and not self._appium_screen_sensitive:
                screenshot_path = self._capture_screenshot(f'execution-{self.execution_id}-failure')
            record_flow_failure(
                self.execution_id, step_name=str(name), step_path=path, code=code,
                duration_ms=duration, match_count=getattr(error, 'match_count', None),
                screenshot_path=screenshot_path, case_revision=self.diagnostic_case_revision,
            )
        except Exception as exc:
            logger.warning('Appium failure details could not be recorded (%s)', type(exc).__name__)

    def _execute_step(self, step: Dict[str, Any]):
        """Sanitize step failures before Allure or enclosing control flow sees them."""
        try:
            return self._execute_step_impl(step)
        except Exception as error:
            self._sanitize_exception_for_reporting(error)
            raise

    def _execute_step_impl(self, step: Dict[str, Any]):
        """执行单个步骤，支持基础组件和自定义组件"""
        # 使用 type 字段获取步骤类型
        action_type = step.get('type', '')
        action = action_type.lower() if action_type else ''
        
        raw_config = step.get('config') if isinstance(step.get('config'), dict) else {}
        from ..scene_component_runtime import _LEGACY_BUILTIN_ACTIONS

        raw_component_config = (
            copy.deepcopy(raw_config)
            if step.get('kind') == 'custom' or action not in _LEGACY_BUILTIN_ACTIONS
            else None
        )
        if action == 'assert_value':
            raw_left = step.get('left', raw_config.get('left', _MISSING))
            self._validate_assert_value_left(raw_left)
        elif action == 'assert_response':
            raw_source = step.get('source', raw_config.get('source', _MISSING))
            raw_path = step.get('path', raw_config.get('path', _MISSING))
            self._validate_assert_response_config(raw_source, raw_path)
        if action == 'text':
            input_expression = step.get(
                'text', raw_config.get('text', step.get('value', raw_config.get('value')))
            )
        else:
            input_expression = step.get('value', raw_config.get('value'))
        raw_sensitive_assignment = False
        raw_sensitive_extract_paths = None
        if action == 'set_variable':
            raw_assignment = step.get('value', raw_config.get('value'))
            raw_sensitive_assignment = (
                bool(self._sensitive_paths_for_reference(raw_assignment))
                or self._references_sensitive_variable(raw_assignment)
                or self._contains_sensitive_value(raw_assignment)
            )
        elif action == 'extract_output':
            raw_source = step.get('source', raw_config.get('source', ''))
            raw_path = step.get('path', raw_config.get('path', ''))
            source_paths = self._sensitive_paths_for_reference(raw_source)
            raw_sensitive_extract_paths = self._project_sensitive_paths(
                source_paths, self._parse_path(raw_path) if isinstance(raw_path, str) else (),
            )
        secret_derived_input = (
            action in {'input', 'text'}
            and (
                self._references_sensitive_variable(input_expression)
                or self._contains_sensitive_value(input_expression)
            )
        )
        if secret_derived_input:
            step['sensitive'] = True
        preserve_loop_operands = {}
        raw_loop_mode = step.get('mode', raw_config.get('mode', 'count'))
        if action == 'loop' and str(self._render_value(raw_loop_mode)).strip().lower() == 'condition':
            for key in ('left', 'right'):
                if key in step:
                    preserve_loop_operands[key] = step[key]
                elif key in raw_config:
                    preserve_loop_operands[key] = raw_config[key]

        nested_step_keys = {
            'if': ('then_steps', 'else_steps'),
            'loop': ('steps',),
            'sequence': ('steps',),
            'try': ('try_steps', 'catch_steps', 'finally_steps'),
        }.get(action, ())
        if step.get('kind') == 'custom':
            nested_step_keys += ('steps',)
        preserve_child_steps = {}
        for key in nested_step_keys:
            if key in step:
                preserve_child_steps[key] = step[key]
            elif key in raw_config:
                preserve_child_steps[key] = raw_config[key]

        # Child steps and condition-loop operands must not be rendered until their
        # branch or iteration actually executes.
        render_source = dict(step)
        resolved_appium_elements = render_source.pop('resolved_appium_elements', None)
        if isinstance(step.get('config'), dict):
            render_source['config'] = dict(step['config'])
            render_source['config'].pop('resolved_appium_elements', None)
        for key in (*nested_step_keys, *preserve_loop_operands):
            render_source.pop(key, None)
            if isinstance(render_source.get('config'), dict):
                render_source['config'].pop(key, None)
        step = self._render_value(render_source)

        # Legacy top-level fields retain precedence, except that a variable
        # component's config.name is its parameter rather than its display name.
        config = step.get('config')
        if isinstance(config, dict):
            for key, value in config.items():
                if key not in step or (key == 'name' and action in {
                    'set_variable', 'unset_variable', 'extract_output', 'extract_text'
                }):
                    step[key] = value
        step.update(preserve_loop_operands)
        step.update(preserve_child_steps)
        if resolved_appium_elements is not None:
            step['resolved_appium_elements'] = resolved_appium_elements
        if raw_component_config is not None:
            step['_ui_flow_raw_component_config'] = raw_component_config
        if action == 'assert_value':
            step['_ui_flow_raw_assert_value_left_validated'] = True
        if action == 'set_variable':
            step['_ui_flow_sensitive_assignment'] = raw_sensitive_assignment
        elif action == 'extract_output' and raw_sensitive_extract_paths is not None:
            step['_ui_flow_sensitive_extract_paths'] = tuple(raw_sensitive_extract_paths)
        resolved_input = step.get('text', step.get('value')) if action == 'text' else step.get('value')
        if action in {'input', 'text'} and (
            secret_derived_input or self._contains_sensitive_value(resolved_input)
        ):
            step['sensitive'] = True

        retry_times_value = step.get('retry_times', self.runtime.get('retry_times', 0))
        retry_interval_value = step.get('retry_interval', self.runtime.get('retry_interval', 0.5))
        try:
            retry_times = int(retry_times_value)
            retry_interval = float(retry_interval_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('retry_times and retry_interval must be numeric') from exc
        if (
            isinstance(retry_times_value, bool)
            or (isinstance(retry_times_value, float) and not retry_times_value.is_integer())
            or not 0 <= retry_times <= _MAX_RETRY_TIMES
        ):
            raise ValueError(f'retry_times must be from 0 to {_MAX_RETRY_TIMES}')
        if (
            isinstance(retry_interval_value, bool)
            or retry_interval < 0
            or retry_interval > _MAX_RETRY_INTERVAL
            or not math.isfinite(retry_interval)
        ):
            raise ValueError(f'retry_interval must be from 0 to {_MAX_RETRY_INTERVAL} seconds')

        sensitive_input = action in {'input', 'text'} and self._is_sensitive_input(step)
        if sensitive_input:
            self._appium_screen_sensitive = True
            retry_times = 0

        def dispatch_and_record():
            try:
                output = self._dispatch_step(step, action)
            except Exception as error:
                proof_action = action if step.get('kind') != 'custom' else ''
                self._track_step_failure(proof_action, error)
                raise
            self._record_step_output(step, output)
            return output

        if retry_times > 0:
            last_error = None
            for attempt in range(1 + retry_times):
                prior_partial_click_chain_failures = self._partial_click_chain_failures
                try:
                    return dispatch_and_record()
                except Exception as e:
                    if self._is_uncertain_device_error(e):
                        self._quarantine_uncertain_device_error(e)
                        raise
                    # Catch/finally can replace the exception from a partially executed click chain.
                    if (
                        isinstance(e, AppiumPartialInputError)
                        or self._partial_click_chain_failures != prior_partial_click_chain_failures
                    ):
                        raise
                    last_error = e
                    if attempt < retry_times:
                        logger.warning(
                            'Step retry failed (%s)', type(e).__name__,
                        )
                        sleep(retry_interval)
            raise last_error
        else:
            try:
                return dispatch_and_record()
            except Exception as e:
                if self._is_uncertain_device_error(e):
                    self._quarantine_uncertain_device_error(e)
                raise
    
    def _dispatch_step(self, step: Dict[str, Any], action: str):
        """分发步骤到对应的 handler（基础组件）或展开执行（自定义组件）"""
        
        # 自定义组件展开执行
        if step.get('kind') == 'custom':
            return self._execute_custom_component(step)
        
        # 根据动作类型执行
        action_map = {
            # 基础动作
            'click': self._action_click,
            'touch': self._action_click,
            'input': self._action_input,
            'text': self._action_text,
            'swipe': self._action_swipe,
            'double_click': self._action_double_click,
            'long_press': self._action_long_press,
            'drag': self._action_drag,
            'swipe_to': self._action_swipe_to,
            
            # 条件动作
            'image_exists_click': self._action_image_exists_click,
            'image_exists_click_chain': self._action_image_exists_click_chain,
            
            # 工具类
            'set_variable': self._action_set_variable,
            'unset_variable': self._action_unset_variable,
            'extract_output': self._action_extract_output,
            'extract_text': self._action_extract_text,
            'screenshot': self._action_screenshot,
            'api_request': self._action_api_request,
            'clear_input': self._action_clear_input,
            'key_event': self._action_key_event,
            'hide_keyboard': self._action_hide_keyboard,
            'app_start': self._action_app_start,
            'app_stop': self._action_app_stop,
            'app_restart': self._action_app_restart,
            'handle_permission': self._action_handle_permission,
            'wait_element': self._action_wait_element,
            'assert_element': self._action_assert_element,
            'assert_text': self._action_assert_text,
            'assert_value': self._action_assert_value,
            'assert_response': self._action_assert_response,
            
            # 控制流
            'wait': self._action_wait,
            'sleep': self._action_wait,
            'if': self._action_if,
            'loop': self._action_loop,
            'sequence': self._action_sequence,
            'try': self._action_try,
            
            # 断言
            'assert': self._action_assert,
            'foreach_assert': self._action_foreach_assert,
        }
        
        handler = action_map.get(action)
        if self.appium_driver is not None:
            handler = self._appium_action_handler(step, action, handler)
        if handler:
            output = handler(step)
            if action in {
                'assert', 'assert_element', 'assert_text', 'assert_value', 'assert_response',
            }:
                self._assertions_executed = getattr(self, '_assertions_executed', 0) + 1
            elif action == 'foreach_assert':
                config = step.get('config') if isinstance(step.get('config'), dict) else {}
                options = {**config, **{key: value for key, value in step.items() if key != 'config'}}
                min_match = options.get('min_match', 1)
                if min_match not in (None, '', False) and self._validated_loop_integer(
                    min_match, 'min_match', 0, _MAX_LOOP_COUNT,
                ) > 0:
                    self._assertions_executed = getattr(self, '_assertions_executed', 0) + 1
            return output
        else:
            # action_map 中找不到，尝试作为自定义组件查找
            if self._try_execute_as_custom(step, action):
                return getattr(self, '_last_custom_output', None)
            raise ValueError(f"未知的动作类型: {action} (步骤: {step.get('name', 'unknown')})")

    def _inline_appium_locator(self, step):
        if self.appium_driver is None:
            return None
        selector_type = step.get('selector_type')
        if selector_type not in appium_locator.INLINE_NATIVE_TYPES:
            return None
        return {
            'strategy': selector_type, 'value': step.get('selector'),
            'match': step.get('selector_match', 'exact'),
            'package_name': self.runtime.get('package_name'),
        }

    def _appium_definition(self, step):
        if self.appium_driver is None:
            return None
        locators = step.get('resolved_appium_elements') or {}
        saved = locators.get('element_id') or locators.get('config.element_id')
        if saved is not None:
            return saved
        action = step.get('type')
        if not step.get('element_id') and isinstance(action, str) and action.lower() in _APPIUM_ELEMENT_ACTIONS:
            return self._inline_appium_locator(step)
        return None

    def _wait_for_appium(
        self, step, state='interactable', *, timeout=None, deadline=None, return_deadline=False, locator=None,
    ):
        if locator is None:
            locator = self._appium_definition(step)
        if locator is None:
            raise ValueError('An Appium action requires a prepared native element')
        configured_timeout, interval = self._validated_element_poll(step)
        if timeout is None:
            lookup_timeout = configured_timeout
        else:
            try:
                lookup_timeout = float(timeout)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError('Appium locator timeout must be numeric') from exc
            if not math.isfinite(lookup_timeout) or lookup_timeout < 0:
                raise ValueError('Appium locator timeout must be finite and non-negative')
            if configured_timeout > 0:
                if lookup_timeout <= 0:
                    raise appium_locator.AppiumLocatorFailure(
                        'element_not_found', 'The Appium locator lookup exceeded its deadline', 0,
                    )
                lookup_timeout = min(configured_timeout, lookup_timeout)

        started = time.monotonic()
        if deadline is not None and started >= deadline:
            raise appium_locator.AppiumLocatorFailure(
                'element_not_found', 'The Appium locator lookup exceeded its deadline', 0,
            )
        if lookup_timeout > 0:
            local_deadline = started + lookup_timeout
            deadline = min(deadline, local_deadline) if deadline is not None else local_deadline
        element = appium_locator.wait_for_locator(
            self.appium_driver, locator, state=state,
            timeout=lookup_timeout, interval=interval, guard=self.device_guard,
            deadline=deadline,
        )
        return (element, deadline) if return_deadline else element

    def _appium_mutate(self, command, *, deadline=None):
        return appium_locator.apply_native_mutation(
            command, guard=self.device_guard, deadline=deadline,
        )

    def _visual_mutate(self, command, *args, **kwargs):
        guard = getattr(self, 'device_guard', None)
        if getattr(self, 'appium_driver', None) is not None and guard is not None:
            guard()
        return command(*args, **kwargs)

    def _clear_focused_visual_input(self, device):
        if getattr(self, 'appium_driver', None) is None or getattr(self, 'device_guard', None) is None:
            return device_actions.clear_focused_input(device)
        self._visual_mutate(
            device_actions._command,
            device,
            ['input', 'keycombination', 'KEYCODE_CTRL_LEFT', 'KEYCODE_A'],
            timeout=5,
            mutation=True,
        )
        self._visual_mutate(device_actions.send_key, device, 'DELETE')

    def _appium_action_handler(self, step, action, fallback):
        if self.device_guard is not None:
            self.device_guard()
        if action in {'app_start', 'app_stop', 'app_restart'}:
            return lambda config: self._appium_app_lifecycle(config, action)
        if self._appium_definition(step) is not None:
            native = {
                'click': self._appium_click, 'touch': self._appium_click,
                'double_click': self._appium_double_click, 'long_press': self._appium_long_press,
                'input': self._appium_input, 'text': self._appium_input,
                'clear_input': self._appium_clear_input, 'wait': self._appium_wait,
                'wait_element': self._action_wait_element, 'assert_element': self._action_assert_element,
                'assert_text': self._action_assert_text, 'extract_text': self._action_extract_text,
            }
            if action not in native:
                raise ValueError('This action requires an explicit visual or coordinate target')
            return native[action]
        direct_visual = {
            'click', 'touch', 'double_click', 'long_press', 'input', 'text', 'clear_input',
            'swipe', 'drag', 'swipe_to', 'key_event', 'hide_keyboard', 'handle_permission',
            'screenshot', 'image_exists_click', 'foreach_assert',
        }
        if action in direct_visual:
            self._require_device()
        return fallback

    def _appium_click(self, step):
        target, deadline = self._wait_for_appium(step, return_deadline=True)
        self._appium_mutate(target.click, deadline=deadline)

    def _appium_double_click(self, step):
        element, deadline = self._wait_for_appium(step, return_deadline=True)
        self._appium_mutate(lambda: self.appium_driver.execute_script(
            'mobile: doubleClickGesture', {'elementId': element.id},
        ), deadline=deadline)

    def _appium_long_press(self, step):
        duration = self._validated_swipe_duration(step.get('duration', 2))
        element, deadline = self._wait_for_appium(step, return_deadline=True)
        self._appium_mutate(lambda: self.appium_driver.execute_script(
            'mobile: longClickGesture', {'elementId': element.id, 'duration': round(duration * 1000)},
        ), deadline=deadline)

    def _appium_input(self, step):
        element, deadline = self._wait_for_appium(step, return_deadline=True)
        value = step.get('text', step.get('value', '')) if step.get('type') == 'text' else step.get('value', '')
        if not self._is_sensitive_input(step):
            from apps.core.variable_resolver import resolve_variables
            value = resolve_variables(value)
        commands = []
        if step.get('clear_first') is True:
            commands.append(element.clear)
        commands.append(lambda: element.send_keys(str(value)))
        if step.get('send_enter') is True:
            commands.append(lambda: self.appium_driver.press_keycode(66))
        for index, command in enumerate(commands):
            try:
                self._appium_mutate(command, deadline=deadline)
            except AppiumOutcomeUnknown:
                raise
            except Exception:
                if index:
                    raise AppiumPartialInputError('The Appium input step partially completed') from None
                raise

    def _appium_clear_input(self, step):
        element, deadline = self._wait_for_appium(step, return_deadline=True)
        self._appium_mutate(element.clear, deadline=deadline)

    def _appium_wait(self, step):
        self._wait_for_appium(step, 'visible')

    def _appium_app_lifecycle(self, step, action):
        package_name = self._lifecycle_package(step)
        if package_name != self.runtime.get('package_name'):
            raise ValueError('The app action must use the current execution package')
        if action in {'app_stop', 'app_restart'}:
            self._appium_mutate(lambda: self.appium_driver.terminate_app(package_name))
        if action in {'app_start', 'app_restart'}:
            self._appium_mutate(lambda: self.appium_driver.activate_app(package_name))

    # ---------- 自定义组件展开 ----------

    def _load_custom_component_defs(self) -> Dict[str, Any]:
        """从数据库加载所有启用的自定义组件定义，缓存到实例上"""
        if not hasattr(self, '_custom_defs_cache'):
            try:
                from ..models import AppCustomComponent
                defs = {}
                for comp in AppCustomComponent.objects.filter(enabled=True):
                    defs[comp.type] = {
                        'name': comp.name,
                        'steps': comp.steps or [],
                        'schema': comp.schema or {},
                        'default_config': comp.default_config or {},
                    }
                self._custom_defs_cache = defs
                logger.debug(f"已加载 {len(defs)} 个自定义组件定义")
            except Exception as exc:
                logger.warning('Custom component definitions could not be loaded (%s)', type(exc).__name__)
                self._custom_defs_cache = {}
        return self._custom_defs_cache

    def _execute_custom_component(self, step: Dict[str, Any]):
        """
        展开并执行自定义组件。
        自定义组件的 steps 是基础组件步骤列表，逐个执行。
        步骤中的参数可通过 config 覆盖默认值。
        """
        comp_type = step.get('type', '')
        comp_name = step.get('name', comp_type)
        definitions = self._load_custom_component_defs()
        comp_def = definitions.get(comp_type)
        
        # 优先从步骤自带的 steps 字段获取（前端可能直接带了）
        sub_steps = step.get('steps')
        
        if not sub_steps:
            comp_def = comp_def or {}
            sub_steps = comp_def.get('steps')
            if not sub_steps:
                raise ValueError(f"自定义组件 '{comp_name}' 未找到或没有可执行步骤")
        comp_def = comp_def or {}
        
        if not sub_steps or not isinstance(sub_steps, list):
            raise ValueError(f"自定义组件 '{comp_name}' 没有可执行步骤")
        
        # 深拷贝，避免修改原始定义
        sub_steps = copy.deepcopy(sub_steps)
        
        comp_config = step.get('config', {})
        if not isinstance(comp_config, dict):
            raise ValueError('自定义组件参数配置无效')
        schema = step.get('schema', comp_def.get('schema', {}))
        defaults = step.get('default_config', comp_def.get('default_config', {}))
        validation_config = step.get('_ui_flow_raw_component_config', comp_config)
        if not isinstance(validation_config, dict):
            validation_config = comp_config
        parameter_values = {
            key: value for key, value in validation_config.items()
            if key not in {'type', 'name', 'kind', 'steps', 'schema', 'default_config', 'version'}
        }
        try:
            from ..scene_component_runtime import (
                SceneComponentError,
                validate_legacy_component_configuration,
            )

            sensitive_parameters = validate_legacy_component_configuration(
                schema or {}, defaults or {}, sub_steps, parameter_values,
                stored_definition=comp_def if comp_def else None,
            )
        except SceneComponentError:
            raise ValueError('自定义组件参数配置无效') from None
        for key, value in comp_config.items():
            if key not in ('type', 'name', 'kind', 'steps'):
                self._set_variable(
                    key, value, 'local', sensitive=key in sensitive_parameters,
                )

        logger.info('Expanding custom component (%s steps)', len(sub_steps))
        
        last_output = None
        for sub_idx, sub_step in enumerate(sub_steps, 1):
            sub_name = sub_step.get('name', sub_step.get('type', 'unknown'))
            logger.info('Custom component child step %s/%s', sub_idx, len(sub_steps))
            output = self._execute_step_at(sub_step, f'.steps[{sub_idx}]')
            if output is not None:
                last_output = output
        return last_output

    def _try_execute_as_custom(self, step: Dict[str, Any], action: str) -> bool:
        """尝试将未知的 action_type 作为自定义组件执行，成功返回 True"""
        defs = self._load_custom_component_defs()
        if action in defs:
            step['kind'] = 'custom'
            self._last_custom_output = self._execute_custom_component(step)
            return True
        return False

    def _safe_filename(self, text: str) -> str:
        safe = re.sub(r'[^0-9a-zA-Z_\-]+', '_', str(text))
        return (safe.strip('_') or 'step')[:80]

    def _screenshot_path(self, name_prefix: str) -> str:
        root = os.path.realpath(self.screenshots_dir)
        os.makedirs(root, exist_ok=True)
        if (
            self._references_sensitive_variable(name_prefix)
            or self._contains_sensitive_value(name_prefix)
        ):
            safe_name_prefix = '[REDACTED]'
        else:
            safe_name_prefix = self._sanitize_sensitive_value(name_prefix)
        filename = (
            f"{self._safe_filename(safe_name_prefix)}_{int(time.time())}_"
            f"{uuid.uuid4().hex}.png"
        )
        path = os.path.abspath(os.path.join(root, filename))
        resolved = os.path.realpath(path)
        try:
            if os.path.commonpath((root, resolved)) != root:
                raise ValueError('Screenshot path must remain inside the screenshot directory')
        except ValueError as exc:
            raise ValueError('Screenshot path must remain inside the screenshot directory') from exc
        return path

    def _is_screenshot_path(self, path: str) -> bool:
        root = os.path.realpath(self.screenshots_dir)
        resolved = os.path.realpath(path)
        try:
            return os.path.commonpath((root, resolved)) == root
        except ValueError:
            return False

    def _capture_screenshot(self, name_prefix: str) -> Optional[str]:
        try:
            full_path = self._screenshot_path(name_prefix)
            if self.appium_driver is not None:
                if getattr(self, '_appium_screen_sensitive', False):
                    return None
                from ..appium_runtime import screenshot
                if self.device_guard is not None:
                    self.device_guard()
                content = screenshot(self.appium_driver).split(',', 1)[1]
                with open(full_path, 'wb') as screenshot_file:
                    screenshot_file.write(base64.b64decode(content, validate=True))
                return full_path
            result = snapshot(filename=full_path)
            if isinstance(result, dict):
                path = result.get('screen')
                if path and os.path.exists(path) and self._is_screenshot_path(path):
                    return path
            if os.path.exists(full_path) and self._is_screenshot_path(full_path):
                return full_path
        except Exception as exc:
            logger.error('Screenshot failed (%s)', type(exc).__name__)
        return None

    def _attach_allure(self, name: str, step: Optional[Dict[str, Any]] = None) -> None:
        if not ALLURE_AVAILABLE:
            return
        safe_name = self._safe_filename(self._sanitize_sensitive_value(name))
        if step is not None:
            try:
                from apps.quality_intelligence.services.redaction import redact_payload
                safe_step = self._sanitize_sensitive_value(copy.deepcopy(step))
                safe_step = redact_payload(safe_step)
                safe_step = self._sanitize_sensitive_value(safe_step)
                if str(step.get('type', '')).lower() in {'input', 'text', 'set_variable'}:
                    for item in (safe_step, safe_step.get('config', {})):
                        if isinstance(item, dict):
                            for key in ('value', 'text'):
                                if key in item:
                                    item[key] = '[REDACTED]'
                allure.attach(
                    json.dumps(safe_step, ensure_ascii=False, indent=2),
                    name=f"{safe_name}-step",
                    attachment_type=allure.attachment_type.JSON
                )
            except Exception as exc:
                logger.warning('Allure JSON attachment failed (%s)', type(exc).__name__)
        config = step.get('config', {}) if isinstance(step, dict) else {}
        if (isinstance(step, dict) and str(step.get('type', '')).lower() in {'input', 'text'}
                and (step.get('sensitive') is True or
                     (isinstance(config, dict) and config.get('sensitive') is True))):
            return
        path = self._capture_screenshot(safe_name)
        if path:
            try:
                allure.attach.file(path, name=safe_name, attachment_type=allure.attachment_type.PNG)
            except Exception as exc:
                logger.warning('Allure screenshot attachment failed (%s)', type(exc).__name__)
    
    def _safe_image_path(self, image_scope: str, image_name: str) -> str:
        if not isinstance(image_scope, str) or not isinstance(image_name, str):
            raise ValueError('Image path must use string scope and filename values')
        root = os.path.realpath(self.image_base_dir)
        resolved = os.path.realpath(os.path.join(root, image_scope, image_name))
        try:
            if os.path.commonpath((root, resolved)) != root:
                raise ValueError('Image path must remain inside the configured image directory')
        except ValueError as exc:
            raise ValueError('Image path must remain inside the configured image directory') from exc
        return resolved

    def _require_device(self):
        if self.load_visual_device is not None:
            load_visual_device, self.load_visual_device = self.load_visual_device, None
            load_visual_device()
        try:
            device = G.DEVICE
        except Exception:
            device = None
        if device is None:
            raise RuntimeError('Android device is unavailable for this action')
        return device

    def _device_resolution(self):
        device = self._require_device()
        try:
            resolution = device.get_current_resolution()
            if not isinstance(resolution, (tuple, list)) or len(resolution) < 2:
                raise ValueError
            width, height = resolution[0], resolution[1]
            if (
                isinstance(width, bool) or isinstance(height, bool)
                or not isinstance(width, (int, float)) or not isinstance(height, (int, float))
                or not math.isfinite(width) or not math.isfinite(height)
                or width < 1 or height < 1
            ):
                raise ValueError
            return int(width), int(height)
        except Exception:
            raise RuntimeError('Unable to determine the Android device resolution') from None

    @staticmethod
    def _parse_ratio_pair(value, label):
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(',')]
        elif isinstance(value, (list, tuple)):
            parts = list(value)
        else:
            parts = []
        if len(parts) != 2:
            raise ValueError(f'{label} must contain two ratios from 0 to 1')
        try:
            ratios = tuple(float(part) for part in parts)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f'{label} must contain two ratios from 0 to 1') from exc
        if not all(math.isfinite(ratio) and 0 <= ratio <= 1 for ratio in ratios):
            raise ValueError(f'{label} must contain two ratios from 0 to 1')
        return ratios

    def _scale_ratio_pair(self, value, label):
        x_ratio, y_ratio = self._parse_ratio_pair(value, label)
        width, height = self._device_resolution()
        return round(x_ratio * (width - 1)), round(y_ratio * (height - 1))

    def _find_native_match(self, step, *, timeout=None, probe=False, active_only=False, all_matches=False):
        selector_type = step.get('selector_type', 'resource_id')
        strategy = {
            'resource_id': 'resource_id',
            'text': 'text',
            'content_desc': 'content_desc',
            'class': 'class',
        }.get(selector_type)
        if strategy is None:
            raise ValueError('Unsupported native selector type')
        selector = step.get('selector')
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError('A non-empty native selector is required')
        try:
            lookup_timeout = step.get('timeout', 5) if timeout is None else timeout
            lookup_timeout = float(lookup_timeout)
            if lookup_timeout <= 0:
                lookup_timeout = _MAX_NATIVE_STATE_PROBE_TIMEOUT
            interval = max(float(step.get('interval', 0.5)), 0.001)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Native lookup timeout and interval must be numeric') from exc
        device = self._require_device()
        if probe:
            probe_method = (
                native_locator.probe_native_elements if all_matches
                else native_locator.probe_native_element
            )
            return probe_method(
                getattr(device, 'adb', None), strategy, selector.strip(),
                timeout=lookup_timeout,
                match=step.get('selector_match', 'exact'),
            )
        return native_locator.find_native_element(
            getattr(device, 'adb', None), strategy, selector.strip(),
            timeout=lookup_timeout,
            interval=min(interval, lookup_timeout),
            match=step.get('selector_match', 'exact'),
            active_only=active_only,
        )

    def _resolve_selector(self, step: Dict[str, Any]) -> Any:
        """
        解析选择器
        
        支持的类型:
        - element_id: 从数据库加载元素
        - image: 图片元素
        - pos: 坐标点
        - region: 区域
        """
        # 优先使用 element_id
        element_id = step.get('element_id')
        if element_id:
            return self._resolve_element_by_id(element_id)
        
        selector_type = step.get('selector_type', 'image')
        selector = step.get('selector', '')
        
        if selector_type == 'image':
            # 图片选择器
            if not selector:
                logger.warning('Image selector is empty')
                return None
            
            image_scope = step.get('image_scope', 'common')
            image_path = self._safe_image_path(image_scope, selector)

            if not os.path.isfile(image_path):
                logger.warning('Image file is unavailable')
                return None
            
            threshold = step.get('image_threshold', 0.7)
            return Template(image_path, threshold=threshold)
        
        elif selector_type == 'relative':
            return self._scale_ratio_pair(selector, 'Relative selector')

        elif selector_type in appium_locator.INLINE_NATIVE_TYPES:
            locator = self._inline_appium_locator(step)
            if locator is not None:
                try:
                    element, deadline = self._wait_for_appium(step, locator=locator, return_deadline=True)
                    return appium_locator.read_native_center(element, deadline=deadline, guard=self.device_guard)
                except appium_locator.AppiumLocatorFailure as error:
                    if error.code == 'element_ambiguous':
                        raise
                    return None
            match = self._find_native_match(step, active_only=True)
            return match.center if match is not None else None

        elif selector_type == 'pos':
            # 坐标选择器
            if isinstance(selector, str):
                parts = [p.strip() for p in selector.split(',')]
                if len(parts) >= 2:
                    return (int(parts[0]), int(parts[1]))
            elif isinstance(selector, (list, tuple)) and len(selector) >= 2:
                return (int(selector[0]), int(selector[1]))
            
            logger.warning('Invalid coordinate selector configuration')
            return None
        
        elif selector_type == 'region':
            # 区域选择器（用于 exists 等）
            if isinstance(selector, str):
                parts = [p.strip() for p in selector.split(',')]
                if len(parts) >= 4:
                    return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
            elif isinstance(selector, (list, tuple)) and len(selector) >= 4:
                return tuple(int(x) for x in selector[:4])
            
            logger.warning('Invalid region selector configuration')
            return None
        
        logger.warning('Unknown selector type')
        return None
    
    def _resolve_element_by_id(self, element_id: int) -> Any:
        """从数据库加载元素"""
        try:
            from apps.app_automation.models import AppElement
            
            element = AppElement.objects.filter(id=element_id, is_active=True).first()
            if not element:
                logger.warning('Configured element was not found')
                return None
            
            # 记录使用次数
            element.increment_usage()
            
            # 根据元素类型返回选择器
            if element.element_type == 'image':
                image_rel_path = element.config.get('image_path', '')
                if not image_rel_path:
                    logger.warning('Element image path is empty')
                    return None

                image_path = self._safe_image_path('', image_rel_path)

                if not os.path.isfile(image_path):
                    logger.warning('Element image is unavailable')
                    return None
                
                threshold = element.config.get('image_threshold', 0.7)
                return Template(image_path, threshold=threshold)
            
            elif element.element_type == 'pos':
                x = element.config.get('x')
                y = element.config.get('y')
                return (x, y)
            
            elif element.element_type == 'region':
                x1 = element.config.get('x1')
                y1 = element.config.get('y1')
                x2 = element.config.get('x2')
                y2 = element.config.get('y2')
                return (x1, y1, x2, y2)
            
        except Exception as exc:
            logger.error('Unable to resolve element (error=%s)', type(exc).__name__)
            return None
    
    def _image_lookup_once(self, target, *, timeout):
        if not callable(getattr(target, 'match_in', None)):
            raise ValueError('Image lookup requires an image template')
        if (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= _MAX_IMAGE_LOOKUP_TIMEOUT
        ):
            raise ValueError('Image lookup timeout must be greater than 0 and at most 2 seconds')
        deadline = time.monotonic() + float(timeout)
        screenshot = device_actions.capture_screenshot(
            self._require_device(), timeout=float(timeout),
        )

        def infer():
            screen = aircv_utils.string_2_img(screenshot)
            if screen is None:
                raise ValueError('Invalid screenshot image')
            return target.match_in(screen)

        try:
            result = _IMAGE_INFERENCE.run(infer, deadline)
        except InferenceTimeout:
            raise device_actions.DeviceActionError('Image lookup exceeded its deadline') from None
        except InferenceBusy:
            raise device_actions.DeviceActionError(
                'Image recognition is unavailable while a previous request is still running'
            ) from None
        except Exception:
            raise device_actions.DeviceActionError('Image recognition failed') from None
        if time.monotonic() >= deadline:
            raise device_actions.DeviceActionError('Image lookup exceeded its deadline')
        return result

    def _resolve_action_target(self, target, *, timeout=_MAX_IMAGE_LOOKUP_TIMEOUT, required=True):
        target = self._interaction_target(target)
        if target is None or not callable(getattr(target, 'match_in', None)):
            return target
        position = self._image_lookup_once(target, timeout=timeout)
        if position is None:
            if required:
                raise device_actions.DeviceActionError('Image target was not found before mutation dispatch')
            return None
        if not isinstance(position, (tuple, list)) or len(position) < 2:
            raise device_actions.DeviceActionError('Image recognition returned invalid coordinates')
        try:
            x, y = float(position[0]), float(position[1])
        except (TypeError, ValueError, OverflowError) as exc:
            raise device_actions.DeviceActionError('Image recognition returned invalid coordinates') from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise device_actions.DeviceActionError('Image recognition returned invalid coordinates')
        return (int(x), int(y))

    @staticmethod
    def _interaction_target(target):
        """Use the center of a rectangular region for point-based gestures."""
        if not isinstance(target, (tuple, list)) or len(target) != 4:
            return target
        try:
            x1, y1, x2, y2 = (float(value) for value in target)
        except (TypeError, ValueError) as exc:
            raise ValueError('区域坐标无效，请检查元素配置') from exc
        if not all(math.isfinite(value) for value in (x1, y1, x2, y2)) or x1 == x2 or y1 == y2:
            raise ValueError('区域坐标无效，请重新框选区域')
        return (int((x1 + x2) / 2), int((y1 + y2) / 2))

    def _action_touch(self, step: Dict[str, Any]):
        """点击动作"""
        target = self._resolve_action_target(self._resolve_selector(step))
        if target is None:
            raise ValueError('Unable to resolve selector for click step')
        logger.info('Executing click action')
        self._visual_mutate(touch, target)
    
    def _action_double_click(self, step: Dict[str, Any]):
        """双击动作"""
        target = self._resolve_action_target(self._resolve_selector(step))
        if target is None:
            raise ValueError('双击步骤无法解析目标元素')
        logger.info('Executing double-click action')
        self._visual_mutate(double_click, target)
    
    def _swipe_by_direction(self, direction: str, duration: float = 0.5):
        duration = self._validated_swipe_duration(duration)
        resolution = G.DEVICE.get_current_resolution()
        if not isinstance(resolution, (tuple, list)) or len(resolution) < 2:
            raise ValueError('Unable to determine device resolution for directional swipe')
        width, height = int(resolution[0]), int(resolution[1])
        if width <= 0 or height <= 0:
            raise ValueError('Device resolution must be positive')

        center_x, center_y = width // 2, height // 2
        if direction == 'up':
            start, end = (center_x, int(height * 0.7)), (center_x, int(height * 0.3))
        elif direction == 'down':
            start, end = (center_x, int(height * 0.3)), (center_x, int(height * 0.7))
        elif direction == 'left':
            start, end = (int(width * 0.7), center_y), (int(width * 0.3), center_y)
        elif direction == 'right':
            start, end = (int(width * 0.3), center_y), (int(width * 0.7), center_y)
        else:
            raise ValueError(f'Unsupported swipe direction: {direction}')
        self._visual_mutate(swipe, start, end, duration=duration)

    def _action_swipe(self, step: Dict[str, Any]):
        """Swipe between explicit legacy coordinates or in a device-relative direction."""
        import json

        start = step.get('start')
        end = step.get('end')
        if step.get('coordinate_space') == 'image_ratio':
            start = step.get('start_ratio', start)
            end = step.get('end_ratio', end)
        if isinstance(start, str) and not start.strip():
            start = None
        if isinstance(end, str) and not end.strip():
            end = None
        duration = step.get('duration', 0.5)

        if start is None and end is None:
            selector = step.get('selector')
            if selector and step.get('selector_type') == 'pos':
                try:
                    locator = json.loads(selector) if isinstance(selector, str) else selector
                except json.JSONDecodeError as exc:
                    raise ValueError('Unable to parse legacy swipe coordinates') from exc
                if (
                    not isinstance(locator, (list, tuple))
                    or len(locator) < 2
                    or not isinstance(locator[0], (list, tuple))
                    or not isinstance(locator[1], (list, tuple))
                    or len(locator[0]) < 2
                    or len(locator[1]) < 2
                ):
                    raise ValueError('Legacy swipe locator must contain start and end coordinate pairs')
                start, end = tuple(locator[0][:2]), tuple(locator[1][:2])

        if start is None and end is None:
            direction = str(step.get('direction', 'up')).lower()
            logger.info('Executing directional swipe')
            self._swipe_by_direction(direction, duration=duration)
            return
        if start is None or end is None:
            raise ValueError('Swipe requires both start and end coordinates')

        coordinate_space = step.get('coordinate_space')
        if coordinate_space is not None:
            if coordinate_space != 'image_ratio':
                raise ValueError('Unsupported swipe coordinate space')
            start = self._scale_ratio_pair(start, 'Swipe start ratio')
            end = self._scale_ratio_pair(end, 'Swipe end ratio')
        else:
            start = self._parse_coordinate_pair(start, 'start')
            end = self._parse_coordinate_pair(end, 'end')
        duration = self._validated_swipe_duration(duration)
        logger.info('Executing swipe between explicit coordinates')
        self._visual_mutate(swipe, start, end, duration=duration)

    @staticmethod
    def _parse_coordinate_pair(value, label):
        if isinstance(value, str):
            values = [part.strip() for part in value.split(',')]
        elif isinstance(value, (list, tuple)):
            values = list(value)
        else:
            values = []
        if len(values) != 2:
            raise ValueError(f'Swipe {label} must contain exactly two coordinates')
        try:
            coordinates = tuple(float(item) for item in values)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f'Swipe {label} coordinates must be numeric') from exc
        if not all(math.isfinite(value) for value in coordinates):
            raise ValueError(f'Swipe {label} coordinates must be finite')
        return tuple(int(value) if value.is_integer() else value for value in coordinates)

    @staticmethod
    def _validated_swipe_duration(duration):
        try:
            duration = float(duration)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Swipe duration must be a positive finite number') from exc
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('Swipe duration must be a positive finite number')
        return duration
    
    def _action_wait(self, step: Dict[str, Any]):
        """Wait for a bounded image match or for the configured duration."""
        target = self._resolve_selector(step)
        if target is None and (step.get('element_id') or step.get('selector')):
            raise ValueError('等待步骤无法解析已配置的目标元素')

        if target is not None:
            if isinstance(target, (tuple, list)):
                raise ValueError('等待元素出现需要图片元素；坐标或区域可用于点击，纯等待请清空定位')
            timeout, interval = self._validated_element_poll(step, timeout_default=3)
            deadline = time.monotonic() + (timeout if timeout > 0 else _MAX_IMAGE_LOOKUP_TIMEOUT)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError('Target image did not appear before the deadline')
                found = self._image_lookup_once(
                    target, timeout=min(_MAX_IMAGE_LOOKUP_TIMEOUT, remaining),
                ) is not None
                remaining = deadline - time.monotonic()
                if found and remaining > 0:
                    return
                if timeout == 0 or remaining <= 0:
                    raise AssertionError('Target image did not appear before the deadline')
                time.sleep(min(interval, remaining))
        else:
            timeout = step.get('timeout', step.get('duration', 3))
            logger.info('Waiting without a selector')
            sleep(timeout)

    def _validated_element_poll(self, step, *, timeout_default=5):
        try:
            timeout = float(step.get('timeout', timeout_default))
            interval = float(step.get('interval', 0.5))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Element timeout and interval must be numeric') from exc
        if not math.isfinite(timeout) or not 0 <= timeout <= _MAX_LOOP_INTERVAL:
            raise ValueError('Element timeout must be from 0 to 300 seconds')
        if not math.isfinite(interval) or not 0 < interval <= _MAX_LOOP_INTERVAL:
            raise ValueError('Element interval must be greater than 0 and no more than 300 seconds')
        return timeout, interval

    def _element_state_matches(self, step, state, lookup_timeout):
        selector_type = step.get('selector_type', 'image')
        self._require_device()
        if selector_type in {'resource_id', 'text', 'content_desc', 'class'}:
            matches = self._find_native_match(
                step, timeout=lookup_timeout, probe=True, all_matches=True,
            )
            if state == 'visible':
                return any(match.visible for match in matches)
            if state == 'hidden':
                return not any(match.visible for match in matches)
            if not matches:
                return False
            attribute = {
                'enabled': 'enabled', 'disabled': 'enabled',
                'checked': 'checked', 'unchecked': 'checked',
                'selected': 'selected', 'unselected': 'selected',
            }[state]
            if state in {'enabled', 'checked', 'selected'}:
                return any(getattr(match, attribute) for match in matches)
            return all(not getattr(match, attribute) for match in matches)
        if selector_type != 'image':
            raise ValueError('Element state checks support image or native selectors')
        if state not in {'visible', 'hidden'}:
            raise ValueError('Image selectors support only visible or hidden state checks')
        target = self._resolve_selector(step)
        if target is None:
            raise RuntimeError('Image element is unavailable for the requested state check')
        try:
            present = self._image_lookup_once(target, timeout=lookup_timeout) is not None
        except Exception:
            raise RuntimeError('Image recognition is unavailable for this device') from None
        return present if state == 'visible' else not present

    def _wait_for_element_state(self, step, state):
        if self._appium_definition(step) is not None:
            self._wait_for_appium(step, state)
            return True
        timeout, interval = self._validated_element_poll(step)
        allowed = {'visible', 'hidden', 'enabled', 'disabled', 'checked', 'unchecked', 'selected', 'unselected'}
        if state not in allowed:
            raise ValueError('Unsupported element state')
        probe_limit = (
            _MAX_NATIVE_STATE_PROBE_TIMEOUT
            if step.get('selector_type', 'image') in {'resource_id', 'text', 'content_desc', 'class'}
            else _MAX_IMAGE_LOOKUP_TIMEOUT
        )
        deadline = time.monotonic() + (timeout if timeout > 0 else probe_limit)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError('Element did not reach the requested state')
            matches = self._element_state_matches(step, state, min(probe_limit, remaining))
            remaining = deadline - time.monotonic()
            if matches and remaining > 0:
                return True
            if timeout == 0 or remaining <= 0:
                raise AssertionError('Element did not reach the requested state')
            time.sleep(min(interval, remaining))

    def _action_wait_element(self, step):
        state = step.get('state', 'visible')
        if state not in {'visible', 'hidden'}:
            raise ValueError('wait_element state must be visible or hidden')
        self._wait_for_element_state(step, state)

    def _action_assert_element(self, step):
        self._wait_for_element_state(step, step.get('state', 'visible'))

    def _action_clear_input(self, step):
        device = self._require_device()
        has_target = bool(step.get('element_id') or step.get('selector'))
        if has_target:
            target = self._resolve_action_target(self._resolve_selector(step))
            if target is None:
                raise ValueError('Unable to resolve selector for clear_input')
            self._visual_mutate(touch, target)
        self._clear_focused_visual_input(device)

    def _action_key_event(self, step):
        self._visual_mutate(device_actions.send_key, self._require_device(), step.get('key'))

    def _action_hide_keyboard(self, _step):
        self._visual_mutate(device_actions.hide_keyboard, self._require_device())

    def _lifecycle_package(self, step):
        package_name = step.get('package_name') or self.runtime.get('package_name')
        if not isinstance(package_name, str) or not package_name.strip():
            raise ValueError('An explicit package_name or current execution package is required')
        return package_name

    def _action_app_start(self, step):
        self._visual_mutate(
            device_actions.start_application, self._require_device(), self._lifecycle_package(step),
        )

    def _action_app_stop(self, step):
        self._visual_mutate(
            device_actions.stop_application, self._require_device(), self._lifecycle_package(step),
        )

    def _action_app_restart(self, step):
        device = self._require_device()
        package_name = self._lifecycle_package(step)
        if getattr(self, 'appium_driver', None) is not None:
            self._visual_mutate(device_actions.stop_application, device, package_name)
            self._visual_mutate(device_actions.start_application, device, package_name)
        else:
            device_actions.restart_application(device, package_name)

    def _action_handle_permission(self, step):
        self._visual_mutate(
            device_actions.handle_permission,
            self._require_device(),
            step.get('choice'),
            timeout=step.get('timeout', 5),
            optional=step.get('optional', True),
        )

    def _action_snapshot(self, step: Dict[str, Any]):
        """Capture a screenshot using a generated filename confined to the screenshot directory."""
        name = step.get('name', f'snapshot_{int(time.time())}')
        filepath = self._screenshot_path(name)
        logger.info('Saving screenshot inside screenshot directory')
        snapshot(filename=filepath)
    
    def _action_text(self, step: Dict[str, Any]):
        """Input text without logging the value; sensitive text bypasses Airtest log wrappers."""
        value = step.get('text', step.get('value', ''))
        sensitive = self._is_sensitive_input(step)
        if sensitive:
            self._visual_mutate(self._enter_sensitive_text, value)
        else:
            logger.info('Executing text input; content is hidden')
            self._visual_mutate(airtest_text, value)

    @staticmethod
    def _is_sensitive_input(step):
        config = step.get('config') if isinstance(step.get('config'), dict) else {}
        return step.get('sensitive') is True or config.get('sensitive') is True

    def _enter_sensitive_text(self, value):
        secret_input.enter_sensitive_text(self._require_device(), value)

    def _quarantine_execution_device(self):
        execution_id = getattr(self, 'execution_id', None)
        if execution_id is None:
            return False
        try:
            from ..models import AppDevice, AppTestExecution

            snapshot = AppTestExecution.objects.filter(pk=execution_id).values(
                'device_id', 'user_id', 'device_lease_generation'
            ).first()
            if not snapshot or not snapshot['device_id']:
                return False
            execution_generation = snapshot['device_lease_generation']
            device_generation = execution_generation if execution_generation is not None else 0
            device_filters = {
                'pk': snapshot['device_id'],
                'lease_generation': device_generation,
            }
            if snapshot.get('user_id') is not None:
                device_filters['locked_by_id'] = snapshot['user_id']
            with transaction.atomic():
                device = AppDevice.objects.select_for_update().filter(**device_filters).first()
                if device is None:
                    return False
                execution_filters = {
                    'pk': execution_id,
                    'device_id': device.pk,
                    'device_lease_generation': execution_generation,
                    'status__in': ('pending', 'running'),
                }
                if snapshot.get('user_id') is not None:
                    execution_filters['user_id'] = snapshot['user_id']
                execution = AppTestExecution.objects.select_for_update().filter(
                    **execution_filters
                ).first()
                if execution is None:
                    return False
                device.maintenance_mode = True
                device.health_status = 'unknown'
                device.save(update_fields=['maintenance_mode', 'health_status', 'updated_at'])
                if getattr(execution, 'device_action_pending', False):
                    execution.device_action_pending = False
                    execution.save(update_fields=['device_action_pending', 'updated_at'])
            logger.warning('Uncertain Android action failed; bound device was quarantined')
            return True
        except Exception as exc:
            logger.error('Unable to quarantine device after uncertain Android action (%s)', type(exc).__name__)
            return False
    
    @staticmethod
    def _convert_variable_value(value, value_type):
        conversion_error = 'set_variable value cannot be converted to the configured type'
        if not isinstance(value_type, str):
            raise ValueError('set_variable value_type must be string, number, boolean, array, or object')
        if value_type == 'string':
            if isinstance(value, str):
                return value
            if type(value) in (bool, int):
                return str(value)
            if type(value) is float and math.isfinite(value):
                return str(value)
            raise ValueError(conversion_error)

        if value_type == 'number':
            if type(value) in (int, float):
                if type(value) is float and not math.isfinite(value):
                    raise ValueError(conversion_error)
                return value
            if isinstance(value, str):
                text_value = value.strip()
                number_pattern = r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?'
                if re.fullmatch(number_pattern, text_value):
                    try:
                        if '.' not in text_value and 'e' not in text_value.lower():
                            return int(text_value)
                        converted = float(text_value)
                    except (ValueError, OverflowError):
                        raise ValueError(conversion_error) from None
                    if math.isfinite(converted):
                        return converted
            raise ValueError(conversion_error)

        if value_type == 'boolean':
            if type(value) is bool:
                return value
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {'true', 'false'}:
                    return normalized == 'true'
            raise ValueError(conversion_error)

        if value_type in {'array', 'object'}:
            if isinstance(value, str):
                def reject_json_constant(_constant):
                    raise ValueError(conversion_error)
                try:
                    value = json.loads(value, parse_constant=reject_json_constant)
                except (TypeError, ValueError, RecursionError):
                    raise ValueError(conversion_error) from None

            def is_json_value(item):
                if item is None or type(item) in (str, bool, int):
                    return True
                if type(item) is float:
                    return math.isfinite(item)
                if type(item) is list:
                    return all(is_json_value(child) for child in item)
                if type(item) is dict:
                    return all(type(key) is str and is_json_value(child) for key, child in item.items())
                return False

            correct_container = type(value) is list if value_type == 'array' else type(value) is dict
            try:
                valid_container = correct_container and is_json_value(value)
            except RecursionError:
                valid_container = False
            if valid_container:
                return value
            raise ValueError(conversion_error)

        raise ValueError('set_variable value_type must be string, number, boolean, array, or object')

    def _action_set_variable(self, step: Dict[str, Any]):
        """设置变量"""
        name = step.get('name')
        value = step.get('value')
        scope = step.get('scope', 'local')

        if name:
            if 'value_type' in step:
                value = self._convert_variable_value(value, step['value_type'])
            self._set_variable(name, value, scope, sensitive=step.get('_ui_flow_sensitive_assignment') is True)
            logger.info('Variable set')
    
    def _action_assert(self, step: Dict[str, Any]):
        """
        断言入口，根据 assert_type 分发到具体实现。
        支持 timeout 参数：断言失败后在 timeout 秒内持续重试（适配页面加载延迟）。
        
        支持的 assert_type:
        - text:   OCR 识别文本，支持 exact/contains/regex 匹配
        - number: OCR 识别数字（自动去除逗号等格式符号），精确匹配
        - regex:  OCR 识别文本，用正则表达式匹配（text + match_mode=regex 的快捷方式）
        - range:  OCR 识别数字，判断是否在 [min, max] 范围内
        - exists: 判断图片元素是否存在于屏幕上
        - image:  在屏幕上查找期望图片是否存在（图片对比断言）
        """
        assert_type = step.get('assert_type', 'text')
        try:
            timeout = float(step.get('timeout', 0))
            retry_interval = float(step.get('retry_interval', 1))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Assertion timeout and retry interval must be numeric') from exc
        if not math.isfinite(timeout) or not 0 <= timeout <= _MAX_LOOP_INTERVAL:
            raise ValueError('Assertion timeout must be from 0 to 300 seconds')
        if not math.isfinite(retry_interval) or not 0 <= retry_interval <= _MAX_RETRY_INTERVAL:
            raise ValueError('Assertion retry interval must be from 0 to 10 seconds')

        assert_map = {
            'text': self._assert_text,
            'number': self._assert_number,
            'regex': self._assert_regex,
            'range': self._assert_range,
            'exists': self._assert_exists,
            'image': self._assert_image,
        }
        handler = assert_map.get(assert_type)
        if not handler:
            raise ValueError(f"未知的断言类型: {assert_type}，支持: {', '.join(assert_map.keys())}")

        deadline = time.monotonic() + (
            timeout if timeout > 0 else _MAX_NATIVE_STATE_PROBE_TIMEOUT
        )
        step['_ui_flow_deadline'] = deadline
        if timeout == 0:
            handler(step)
            if time.monotonic() >= deadline:
                raise AssertionError('Assertion read exceeded its deadline')
            return

        last_error = None
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                handler(step)
                if time.monotonic() >= deadline:
                    raise AssertionError('Assertion read exceeded its deadline')
                if attempt > 1:
                    logger.info('Assertion passed after retrying')
                return
            except Exception as error:
                if self._is_uncertain_device_error(error):
                    self._quarantine_uncertain_device_error(error)
                    raise
                last_error = error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(retry_interval, remaining))

        if last_error is None:
            raise AssertionError('Assertion timed out')
        raise last_error
    
    # ---------- 断言内部实现 ----------
    
    def _parse_ocr_region(self, step: Dict[str, Any]) -> tuple:
        """
        从步骤配置中解析 OCR 区域坐标 (x1, y1, x2, y2)。
        同时支持 selector 和 ocr_selector 字段名。
        """
        selector = step.get('ocr_selector') or step.get('selector')
        selector_type = step.get('ocr_selector_type') or step.get('selector_type', 'region')
        
        if not selector:
            raise ValueError('OCR assertion requires a region selector')
        
        if selector_type != 'region':
            raise ValueError('OCR assertions only support region selectors')
        
        if isinstance(selector, str):
            try:
                parts = selector.split(',')
                if len(parts) != 4:
                    raise ValueError
                return tuple(int(part.strip()) for part in parts)
            except (TypeError, ValueError, OverflowError):
                raise ValueError('Invalid OCR region') from None
        elif isinstance(selector, (list, tuple)) and len(selector) >= 4:
            try:
                return tuple(int(value) for value in selector[:4])
            except (TypeError, ValueError, OverflowError):
                raise ValueError('Invalid OCR region') from None
        else:
            raise ValueError('Invalid OCR region')
    
    def _ocr_recognize_text(self, region: tuple, *, deadline=None) -> str:
        """Recognize a region from a bounded screenshot before the deadline."""
        ocr = self._get_ocr_helper()
        return ocr.recognize_region_text(region, deadline=deadline)

    def _ocr_recognize_number(self, region: tuple, *, deadline=None) -> int:
        """Recognize a number from a bounded screenshot before the deadline."""
        ocr = self._get_ocr_helper()
        return ocr.recognize_region_number(region, deadline=deadline)
    
    def _assert_text(self, step: Dict[str, Any]):
        """文本断言：OCR 识别文本，支持 exact/contains/regex 匹配"""
        if not OCR_AVAILABLE:
            raise RuntimeError("文本断言需要 OCR 支持，请安装 easyocr")
        
        region = self._parse_ocr_region(step)
        expected = step.get('expected', '')
        match_mode = step.get('match_mode', 'contains')
        if match_mode in {'contains', 'regex'} and (not isinstance(expected, str) or expected == ''):
            raise ValueError('Text contains/regex assertions require a non-empty expected value')

        actual_text = self._ocr_recognize_text(
            region, deadline=step.get('_ui_flow_deadline'),
        )
        
        if match_mode == 'exact':
            passed = actual_text == expected
        elif match_mode == 'contains':
            passed = expected in actual_text
        elif match_mode == 'regex':
            passed = _safe_regex_search(expected, actual_text)
        else:
            raise ValueError(f"不支持的 match_mode: {match_mode}")
        
        if not passed:
            raise AssertionError('Text assertion failed')
        
        logger.info('Text assertion passed')
    
    def _assert_number(self, step: Dict[str, Any]):
        """数值断言：OCR 识别数字（去逗号），与期望值精确匹配"""
        if not OCR_AVAILABLE:
            raise RuntimeError("数值断言需要 OCR 支持，请安装 easyocr")
        
        region = self._parse_ocr_region(step)
        expected_raw = step.get('expected', '0')
        
        # 期望值也做去逗号处理，兼容用户填 "3,000,000" 或 "3000000"
        try:
            expected_num = int(str(expected_raw).replace(',', '').replace(' ', ''))
        except (ValueError, TypeError):
            raise ValueError('Number assertion expected value must be an integer')
        
        actual_num = self._ocr_recognize_number(
            region, deadline=step.get('_ui_flow_deadline'),
        )
        
        if actual_num != expected_num:
            raise AssertionError('Number assertion failed')
        
        logger.info('Number assertion passed')
    
    def _assert_regex(self, step: Dict[str, Any]):
        """正则断言：OCR 识别文本，用正则表达式匹配"""
        if not OCR_AVAILABLE:
            raise RuntimeError("正则断言需要 OCR 支持，请安装 easyocr")
        
        region = self._parse_ocr_region(step)
        pattern = step.get('expected', '')
        
        if not pattern:
            raise ValueError("regex 断言需要在 expected 字段填写正则表达式")
        
        actual_text = self._ocr_recognize_text(
            region, deadline=step.get('_ui_flow_deadline'),
        )
        
        match = _safe_regex_search(pattern, actual_text)
        if not match:
            raise AssertionError('Regex assertion failed')
        
        logger.info('Regex assertion passed')
    
    def _assert_range(self, step: Dict[str, Any]):
        """范围断言：OCR 识别数字，判断是否在 [min, max] 范围内"""
        if not OCR_AVAILABLE:
            raise RuntimeError("范围断言需要 OCR 支持，请安装 easyocr")
        
        region = self._parse_ocr_region(step)
        
        min_val = step.get('min')
        max_val = step.get('max')
        
        if min_val is None and max_val is None:
            raise ValueError("range 断言需要至少设置 min 或 max 之一")
        
        # 转换为数值
        try:
            min_num = int(str(min_val).replace(',', '').replace(' ', '')) if min_val is not None else None
        except (ValueError, TypeError):
            raise ValueError('Range assertion minimum must be an integer')
        try:
            max_num = int(str(max_val).replace(',', '').replace(' ', '')) if max_val is not None else None
        except (ValueError, TypeError):
            raise ValueError('Range assertion maximum must be an integer')
        
        actual_num = self._ocr_recognize_number(
            region, deadline=step.get('_ui_flow_deadline'),
        )
        
        if min_num is not None and actual_num < min_num:
            raise AssertionError('Range assertion failed: below minimum')
        if max_num is not None and actual_num > max_num:
            raise AssertionError('Range assertion failed: above maximum')
        
        range_desc = f"[{min_num if min_num is not None else '-∞'}, {max_num if max_num is not None else '+∞'}]"
        logger.info('Range assertion passed')
    
    def _assert_exists(self, step: Dict[str, Any]):
        """存在性断言：判断图片元素是否存在于屏幕上"""
        target = self._resolve_selector(step)
        expected_exists = step.get('expected_exists', True)
        if target is None:
            raise ValueError('存在性断言无法解析目标元素')

        lookup_timeout = _MAX_IMAGE_LOOKUP_TIMEOUT
        if step.get('_ui_flow_deadline') is not None:
            lookup_timeout = min(
                lookup_timeout, step['_ui_flow_deadline'] - time.monotonic(),
            )
        if lookup_timeout <= 0:
            raise TimeoutError('Existence assertion exceeded its deadline')
        result = self._image_lookup_once(target, timeout=lookup_timeout) is not None
        
        if expected_exists and not result:
            raise AssertionError(f"期望元素存在，但实际不存在")
        elif not expected_exists and result:
            raise AssertionError(f"期望元素不存在，但实际存在")
        
        logger.info('Existence assertion passed')
    
    def _assert_image(self, step: Dict[str, Any]):
        """图片断言：在屏幕上查找期望图片是否存在"""
        expected_image = step.get('expected', '')
        image_scope = step.get('expected_image_scope') or step.get('image_scope', 'common')
        threshold = step.get('image_threshold', 0.7)
        
        if not expected_image:
            raise ValueError("image 断言需要在 expected 字段填写图片文件名")
        
        image_path = self._safe_image_path(image_scope, expected_image)

        if not os.path.isfile(image_path):
            raise ValueError('Expected image does not exist')
        
        target = Template(image_path, threshold=threshold)
        lookup_timeout = _MAX_IMAGE_LOOKUP_TIMEOUT
        if step.get('_ui_flow_deadline') is not None:
            lookup_timeout = min(
                lookup_timeout, step['_ui_flow_deadline'] - time.monotonic(),
            )
        if lookup_timeout <= 0:
            raise TimeoutError('Image assertion exceeded its deadline')
        result = self._image_lookup_once(target, timeout=lookup_timeout) is not None
        
        if not result:
            raise AssertionError('未在屏幕上找到期望图片')

        logger.info('Image assertion passed')
    
    def _run_assertion_until(self, check, timeout, interval):
        deadline = time.monotonic() + (
            timeout if timeout > 0 else _MAX_NATIVE_STATE_PROBE_TIMEOUT
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError('Text assertion timed out')
            lookup_timeout = min(_MAX_IMAGE_LOOKUP_TIMEOUT, remaining)
            try:
                check(lookup_timeout, deadline)
                if time.monotonic() >= deadline:
                    raise AssertionError('Text assertion timed out')
                return
            except AssertionError:
                remaining = deadline - time.monotonic()
                if timeout == 0 or remaining <= 0:
                    raise
                time.sleep(min(interval, remaining))

    def _read_text_selector(self, step, timeout, *, deadline=None):
        if self._appium_definition(step) is not None:
            remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
            if deadline is not None and remaining <= 0:
                raise appium_locator.AppiumLocatorFailure(
                    'element_state_mismatch', 'The Appium text read exceeded its deadline', 0,
                )
            element, locator_deadline = self._wait_for_appium(
                step, 'visible', timeout=remaining, deadline=deadline, return_deadline=True,
            )
            return appium_locator.read_native_text(
                element, deadline=locator_deadline, guard=self.device_guard,
            )
        self._require_device()
        if deadline is None:
            deadline = step.get('_ui_flow_deadline')
        selector_type = step.get('selector_type', 'resource_id')
        if selector_type == 'region':
            if not OCR_AVAILABLE:
                raise RuntimeError('OCR is unavailable; region text requires EasyOCR and its local model files')
            region = self._parse_ocr_region(step)
            return self._ocr_recognize_text(region, deadline=deadline)
        if selector_type not in {'resource_id', 'text', 'content_desc', 'class'}:
            raise ValueError('Text checks support native selectors or an OCR region')
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError('Native text lookup exceeded its deadline')
            timeout = min(timeout, remaining)
        match = self._find_native_match(step, timeout=timeout)
        if deadline is not None and time.monotonic() >= deadline:
            raise AssertionError('Native text lookup exceeded its deadline')
        if match is None:
            raise AssertionError('Native text element was not found')
        if match.text:
            return match.text
        return match.content_desc or ''

    def _action_assert_text(self, step):
        timeout, interval = self._validated_element_poll(step)
        expected = step.get('expected', '')
        match_mode = step.get('match_mode', 'contains')
        if not isinstance(expected, str):
            raise ValueError('assert_text expected value must be a string')
        if match_mode in {'contains', 'regex'} and expected == '':
            raise ValueError('Text contains/regex assertions require a non-empty expected value')
        if match_mode not in {'exact', 'contains', 'regex'}:
            raise ValueError('Unsupported assert_text match mode')

        def check(lookup_timeout, deadline):
            actual = str(self._read_text_selector(step, lookup_timeout, deadline=deadline))
            if match_mode == 'exact':
                passed = actual == expected
            elif match_mode == 'contains':
                passed = expected in actual
            else:
                passed = _safe_regex_search(expected, actual)
            if not passed:
                raise AssertionError('Text assertion failed')

        self._run_assertion_until(check, timeout, interval)

    def _action_assert_value(self, step):
        config = step.get('config') if isinstance(step.get('config'), dict) else {}
        if not step.get('_ui_flow_raw_assert_value_left_validated'):
            self._validate_assert_value_left(step.get('left', config.get('left', _MISSING)))
        left = step.get('left', config.get('left', ''))
        operator = step.get('operator', config.get('operator', '=='))
        right = step.get('right', config.get('right', ''))
        if not self._eval_condition(left, operator, right):
            raise AssertionError('Value assertion failed')

    def _action_assert_response(self, step):
        config = step.get('config') if isinstance(step.get('config'), dict) else {}
        source = step.get('source', config.get('source', _MISSING))
        path = step.get('path', config.get('path', _MISSING))
        self._validate_assert_response_config(source, path)
        try:
            source = self._render_value(source)
            path = self._render_value(path)
        except ValueError:
            raise
        self._validate_assert_response_config(source, path)
        reference = f'{source}.{path}'
        actual = self._resolve_reference(reference)
        if actual is _MISSING:
            raise ValueError('assert_response source path was not found')
        if not self._eval_condition(actual, step.get('operator', '=='), step.get('expected', '')):
            raise AssertionError('Response assertion failed')

    def _action_extract_text(self, step):
        name = step.get('name')
        scope = step.get('scope', 'local')
        if not isinstance(name, str) or not name.strip():
            raise ValueError('extract_text requires a variable name')
        if scope not in {'local', 'global'}:
            raise ValueError('extract_text scope must be local or global')
        timeout, _interval = self._validated_element_poll(step)
        value = self._read_text_selector(
            step,
            timeout if timeout > 0 else _MAX_NATIVE_STATE_PROBE_TIMEOUT,
            deadline=time.monotonic() + (
                timeout if timeout > 0 else _MAX_NATIVE_STATE_PROBE_TIMEOUT
            ),
        )
        self._set_variable(name, value, scope)
        logger.info('Extracted UI text into a variable')

    # ============ 新增动作方法 ============
    
    def _action_click(self, step: Dict[str, Any]):
        """点击动作（重命名自 _action_touch）"""
        self._action_touch(step)
    
    def _action_input(self, step: Dict[str, Any]):
        """输入文本"""
        target = self._resolve_action_target(self._resolve_selector(step))
        if target is None and (step.get('element_id') or step.get('selector')):
            raise ValueError('输入步骤无法解析已配置的目标元素')
        value = step.get('value', '')
        send_enter = step.get('send_enter', False)

        if target is not None:
            self._visual_mutate(touch, target)
            time.sleep(0.3)
        if step.get('clear_first') is True:
            self._clear_focused_visual_input(self._require_device())

        logger.info('Executing text input; content is hidden')
        if self._is_sensitive_input(step):
            self._visual_mutate(self._enter_sensitive_text, value)
        else:
            from apps.core.variable_resolver import resolve_variables
            value = resolve_variables(value)
            self._visual_mutate(airtest_text, value)

        if send_enter:
            time.sleep(0.2)
            self._visual_mutate(device_actions.send_key, self._require_device(), 'ENTER')
    
    def _action_long_press(self, step: Dict[str, Any]):
        """长按"""
        target = self._resolve_action_target(self._resolve_selector(step))
        duration = step.get('duration', 2)
        
        if target is None:
            raise ValueError('长按步骤无法解析目标元素')
        logger.info('Long-press action')
        self._visual_mutate(touch, target, duration=duration)
    
    def _action_drag(self, step: Dict[str, Any]):
        """拖拽：从起点拖拽到终点"""
        # 解析起点
        start_config = {
            'selector': step.get('start_selector'),
            'selector_type': step.get('start_selector_type', 'image'),
            'selector_match': step.get('start_selector_match', 'exact'),
            'image_scope': step.get('start_image_scope') or step.get('image_scope', 'common')
        }
        start = self._resolve_action_target(self._resolve_selector(start_config))
        
        # 解析终点
        end_config = {
            'selector': step.get('end_selector'),
            'selector_type': step.get('end_selector_type', 'image'),
            'selector_match': step.get('end_selector_match', 'exact'),
            'image_scope': step.get('end_image_scope') or step.get('image_scope', 'common')
        }
        end = self._resolve_action_target(self._resolve_selector(end_config))
        
        duration = step.get('duration', 0.8)
        
        if start is None or end is None:
            raise ValueError('拖拽步骤无法解析起点或终点')
        logger.info('Drag action')
        self._visual_mutate(swipe, start, end, duration=duration)
    
    def _action_swipe_to(self, step: Dict[str, Any]):
        """滑动直到目标元素出现"""
        target_config = {
            'selector': step.get('target_selector'),
            'selector_type': step.get('target_selector_type', 'image'),
            'selector_match': step.get('target_selector_match', 'exact'),
            'image_scope': step.get('target_image_scope') or step.get('image_scope', 'common')
        }
        native_locator_config = self._inline_appium_locator(target_config)
        target = self._resolve_selector(target_config) if native_locator_config is None else None
        if target is None and native_locator_config is None:
            raise ValueError('滑动查找步骤无法解析目标元素')

        direction = str(step.get('direction', 'up')).lower()
        max_swipes = self._validated_loop_integer(step.get('max_swipes', 5), 'max_swipes', 1, _MAX_LOOP_COUNT)
        interval = self._validated_loop_interval(step.get('interval', 0.5))
        duration = self._validated_swipe_duration(step.get('duration', 0.5))

        for i in range(max_swipes + 1):
            if native_locator_config is None:
                found = self._resolve_action_target(target, required=False) is not None
            else:
                try:
                    self._wait_for_appium(
                        {**target_config, 'timeout': 0}, 'visible', locator=native_locator_config,
                    )
                    found = True
                except appium_locator.AppiumLocatorFailure as error:
                    if error.code not in ('element_not_found', 'element_state_mismatch'):
                        raise
                    found = False
            if found:
                logger.info('Target found; stopping swipe search')
                return
            if i == max_swipes:
                break
            logger.info('Directional swipe search')
            self._swipe_by_direction(direction, duration=duration)
            if interval:
                time.sleep(interval)

        raise AssertionError(f"滑动 {max_swipes} 次后仍未找到目标元素")
    
    def _action_image_exists_click(self, step: Dict[str, Any]):
        """主定位存在则点击主定位，否则点击备用定位"""
        # 主定位
        main_config = {
            'selector': step.get('selector'),
            'selector_type': step.get('selector_type', 'image'),
            'selector_match': step.get('selector_match', 'exact'),
            'image_scope': step.get('image_scope', 'common'),
            'image_threshold': step.get('image_threshold', 0.7)
        }
        main_target = self._resolve_selector(main_config)
        
        # 备用定位
        fallback_config = {
            'selector': step.get('fallback_selector'),
            'selector_type': step.get('fallback_selector_type', 'image'),
            'selector_match': step.get('fallback_selector_match', 'exact'),
            'image_scope': step.get('fallback_image_scope', 'common'),
            'image_threshold': step.get('fallback_image_threshold', 0.7)
        }
        fallback_target = self._resolve_selector(fallback_config)
        
        main_point = self._resolve_action_target(main_target, required=False)
        if main_point is not None:
            logger.info('Main target found; clicking main target')
            self._visual_mutate(touch, main_point)
        elif fallback_target is not None:
            logger.info('Main target missing; clicking fallback target')
            self._visual_mutate(touch, self._resolve_action_target(fallback_target))
        else:
            raise ValueError('主定位不存在且备用定位不可用')
    
    def _click_chain_target(self, config, target, locator, *, required):
        if locator is not None:
            try:
                element, deadline = self._wait_for_appium(config, return_deadline=True, locator=locator)
            except appium_locator.AppiumLocatorFailure as error:
                if required or error.code not in {'element_not_found', 'element_state_mismatch'}:
                    raise
                return False
            self._appium_mutate(element.click, deadline=deadline)
            return True

        point = self._resolve_action_target(target, required=required)
        if point is None:
            return False
        if self.appium_driver is not None:
            self._require_device()
        self._visual_mutate(touch, point)
        return True

    def _action_image_exists_click_chain(self, step: Dict[str, Any]):
        """Click the optional main target, then find the fallback on the current screen."""
        main_config = {
            'selector': step.get('selector'),
            'selector_type': step.get('selector_type', 'image'),
            'selector_match': step.get('selector_match', 'exact'),
            'image_scope': step.get('image_scope', 'common'),
            'image_threshold': step.get('image_threshold', 0.7),
            'timeout': step.get('timeout', 5),
        }
        fallback_config = {
            'selector': step.get('fallback_selector'),
            'selector_type': step.get('fallback_selector_type', 'image'),
            'selector_match': step.get('fallback_selector_match', 'exact'),
            'image_scope': step.get('fallback_image_scope', 'common'),
            'image_threshold': step.get('fallback_image_threshold', 0.7),
            'timeout': step.get('timeout', 5),
        }
        main_locator = self._inline_appium_locator(main_config)
        fallback_locator = self._inline_appium_locator(fallback_config)
        relative_fallback = fallback_config['selector_type'] == 'relative'
        main_target = self._resolve_selector(main_config) if main_locator is None else None
        fallback_target = None
        if relative_fallback:
            self._parse_ratio_pair(fallback_config['selector'], 'Relative selector')
        elif fallback_locator is None:
            fallback_target = self._resolve_selector(fallback_config)
        if (main_locator is None and main_target is None) or (
            not relative_fallback and fallback_locator is None and fallback_target is None
        ):
            raise ValueError('image_exists_click_chain requires both main and fallback targets')
        if fallback_locator is not None:
            # Validate the definition before the first click. Resolve its element after navigation.
            appium_locator._selector(fallback_locator)

        main_clicked = self._click_chain_target(main_config, main_target, main_locator, required=False)
        try:
            if main_clicked:
                time.sleep(0.5)
            if relative_fallback:
                fallback_target = self._resolve_selector(fallback_config)
            self._click_chain_target(fallback_config, fallback_target, fallback_locator, required=True)
        except Exception:
            if main_clicked:
                # A retry of any enclosing step would replay the completed first click.
                self._partial_click_chain_failures += 1
            raise
    
    def _action_unset_variable(self, step: Dict[str, Any]):
        """删除变量"""
        name = step.get('name')
        scope = step.get('scope', 'local')
        
        if name and scope in self.context and name in self.context[scope]:
            del self.context[scope][name]
            logger.info('Variable removed')
    
    def _action_extract_output(self, step: Dict[str, Any]):
        """
        从变量中按路径提取字段并保存为新变量。
        
        配置示例:
            source: "local.response"      # 来源变量，格式: scope.var_name
            path: "body.data.token"       # 提取路径，支持多级 key 和列表索引 [0]
            name: "token"                 # 保存为新变量名
            scope: "local"                # 保存到哪个作用域
        """
        source = step.get('source', '')
        path = step.get('path', '')
        name = step.get('name')
        scope = step.get('scope', 'local')
        
        if not name:
            raise ValueError("extract_output 需要 name 参数（保存的变量名）")
        if not source:
            raise ValueError("extract_output 需要 source 参数（来源变量）")
        
        source_value = self._resolve_reference(source)
        if source_value is _MISSING:
            raise ValueError('extract_output source variable was not found')

        source_paths = self._sensitive_paths_for_reference(source)
        if path:
            current = source_value
            for key in self._parse_path(path):
                current = self._lookup_path_value(current, key)
                if current is _MISSING:
                    raise ValueError('extract_output path value was not found')
            extracted = current
            selected_path = self._parse_path(path)
        else:
            extracted = source_value
            selected_path = ()

        sensitive_paths = step.get('_ui_flow_sensitive_extract_paths')
        if sensitive_paths is None:
            sensitive_paths = self._project_sensitive_paths(source_paths, selected_path)
        self._set_variable(name, extracted, scope, sensitive_paths=sensitive_paths)
        logger.info('Output extracted into a variable')
    
    @staticmethod
    def _parse_path(path: str) -> List[str]:
        """解析提取路径，支持 'a.b.c' 和 'a[0].b' 格式"""
        keys = []
        for part in path.split('.'):
            if '[' in part:
                # 处理 "items[0]" -> ["items", "0"]
                base, rest = part.split('[', 1)
                if base:
                    keys.append(base)
                for idx_part in rest.split('['):
                    idx_part = idx_part.rstrip(']')
                    if idx_part:
                        keys.append(idx_part)
            else:
                if part:
                    keys.append(part)
        return keys
    
    def _action_screenshot(self, step: Dict[str, Any]):
        """截图（重命名自 _action_snapshot）"""
        self._action_snapshot(step)
    
    def _action_api_request(self, step: Dict[str, Any]):
        """
        执行HTTP请求，支持状态码校验、自动/手动响应解析和字段提取。
        
        配置项:
            method: GET / POST / PUT / DELETE / PATCH
            url: 请求地址（支持变量渲染）
            headers / params / json / data: 请求参数
            timeout: 超时秒数，默认10
            response_type: auto(自动判断) / json / text / binary
            expected_status: 期望的 HTTP 状态码，如 200
            save_as: 将完整响应结果保存为变量名
            scope: 变量保存的作用域，默认 local
            extracts: 字段提取列表
                - path: "body.data.token"
                  name: "token"
                  scope: "local"
        """
        method = step.get('method', 'GET').upper()
        url = self._render_value(step.get('url', ''))
        headers = step.get('headers', {})
        params = step.get('params', {})
        json_data = step.get('json', {})
        data = step.get('data', {})
        timeout = step.get('timeout', 10)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= APP_API_REQUEST_MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f'api_request timeout must be greater than 0 and at most '
                f'{APP_API_REQUEST_MAX_TIMEOUT_SECONDS:g} seconds'
            )
        save_as = step.get('save_as', '')
        scope = step.get('scope', 'local')
        response_type = step.get('response_type', 'auto')
        expected_status = step.get('expected_status')
        extracts = step.get('extracts', [])
        
        if not isinstance(url, str) or not url.strip():
            raise ValueError("api_request 缺少 url 参数")
        base_url = self.runtime.get('base_url', '')
        if base_url and not urlsplit(url).scheme and not url.startswith('//'):
            url = urljoin(base_url.rstrip('/') + '/', url)
        try:
            url = validate_outbound_url(url, allow_private=False)
        except UnsafeOutboundURL:
            raise ValueError('api_request URL is invalid or disallowed') from None
        
        # 渲染 headers/params 中的变量
        if isinstance(headers, dict):
            headers = {k: self._render_value(v) if isinstance(v, str) else v
                       for k, v in headers.items()}
        if isinstance(params, dict):
            params = {k: self._render_value(v) if isinstance(v, str) else v
                      for k, v in params.items()}
        
        logger.info('HTTP request started')
        
        try:
            response = guarded_request(
                method=method,
                url=url,
                allow_private=False,
                headers=headers,
                params=params,
                json=json_data if json_data else None,
                data=data if data else None,
                timeout=timeout,
                allow_redirects=False,
            )
        except Exception as exc:
            logger.warning('HTTP request failed (%s)', type(exc).__name__)
            raise RuntimeError(f'HTTP request failed ({type(exc).__name__})') from None
        
        # 解析响应体
        body = self._parse_response_body(response, response_type)
        
        result = {
            'status_code': response.status_code,
            'headers': dict(response.headers),
            'body': body
        }
        
        logger.info('HTTP response received')
        
        # 状态码校验
        if expected_status is not None:
            expected_status = int(expected_status)
            if response.status_code != expected_status:
                raise AssertionError(
                    f"HTTP状态码断言失败: 期望 {expected_status}, "
                    f"实际 {response.status_code}"
                )
        
        result_sensitive_paths = self._sensitive_output_paths(result)
        if save_as:
            self._set_variable(
                save_as, result, scope, sensitive_paths=result_sensitive_paths,
            )
        
        # 字段提取
        if extracts and isinstance(extracts, list):
            for extract in extracts:
                if not isinstance(extract, dict):
                    continue
                e_path = extract.get('path', '')
                e_name = extract.get('name', '')
                e_scope = extract.get('scope', scope)
                if not e_name:
                    logger.warning('api_request extraction is missing a target variable name')
                    continue

                current = result
                key = ''
                for key in self._parse_path(e_path):
                    current = self._lookup_path_value(current, key)
                    if current is _MISSING:
                        raise ValueError(
                            f"api_request extracts path '{e_path}' could not be resolved at '{key}'"
                        )
                selected_paths = self._project_sensitive_paths(
                    result_sensitive_paths,
                    self._parse_path(e_path) if isinstance(e_path, str) else (),
                )
                self._set_variable(
                    e_name, current, e_scope, sensitive_paths=selected_paths,
                )
                logger.info('API response field extracted')
        return result
    
    @staticmethod
    def _parse_response_body(response, response_type: str = 'auto'):
        """根据 response_type 解析响应体"""
        if response_type == 'json':
            try:
                return response.json()
            except Exception:
                return response.text
        elif response_type == 'text':
            return response.text
        elif response_type == 'binary':
            import base64
            return base64.b64encode(response.content).decode('ascii')
        else:
            # auto: 根据 Content-Type 自动判断
            content_type = response.headers.get('Content-Type', '')
            if 'json' in content_type or 'javascript' in content_type:
                try:
                    return response.json()
                except Exception:
                    return response.text
            return response.text
    
    def _action_if(self, step: Dict[str, Any]):
        """条件分支，支持丰富的操作符"""
        left = self._render_value(step.get('left', ''))
        right = self._render_value(step.get('right', ''))
        operator = step.get('operator', '==')
        then_steps = step.get('then_steps', [])
        else_steps = step.get('else_steps', [])
        
        condition = self._eval_condition(left, operator, right)
        
        logger.info('Evaluating conditional branch')
        
        # 执行分支
        if condition:
            self._execute_children(then_steps, 'then_steps')
        else:
            self._execute_children(else_steps, 'else_steps')
    
    @staticmethod
    def _eval_condition(left, operator: str, right) -> bool:
        """
        评估条件表达式。
        
        支持的操作符:
            ==, !=, >, >=, <, <=,
            in, not in, not_in,
            contains, notcontains, not_contains,
            regex, match,
            truthy, exists,
            falsy, not_exists,
            startswith, endswith
        """
        op = str(operator).strip().lower()
        
        # 相等判断
        if op == '==':
            return str(left) == str(right)
        if op == '!=':
            return str(left) != str(right)
        
        # 数值比较
        if op in ('>', '>=', '<', '<='):
            try:
                l_val, r_val = float(left), float(right)
            except (ValueError, TypeError):
                return False
            if op == '>':
                return l_val > r_val
            if op == '>=':
                return l_val >= r_val
            if op == '<':
                return l_val < r_val
            return l_val <= r_val
        
        # 包含判断
        if op == 'in':
            return str(left) in str(right)
        if op in ('not in', 'not_in'):
            return str(left) not in str(right)
        if op == 'contains':
            return str(right) in str(left)
        if op in ('notcontains', 'not_contains'):
            return str(right) not in str(left)
        
        # 正则匹配
        if op in ('regex', 'match'):
            return _safe_regex_search(right, left)
        
        # 真值 / 假值
        if op in ('truthy', 'exists'):
            return bool(left)
        if op in ('falsy', 'not_exists'):
            return not bool(left)
        
        # 前缀 / 后缀
        if op == 'startswith':
            return str(left).startswith(str(right))
        if op == 'endswith':
            return str(left).endswith(str(right))
        
        raise ValueError(f'Unsupported condition operator: {operator}')
    
    @staticmethod
    def _validated_loop_integer(value, name, minimum, maximum):
        if isinstance(value, bool):
            raise ValueError(f'{name} must be an integer in the supported range')
        try:
            integer = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f'{name} must be an integer in the supported range') from exc
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f'{name} must be an integer in the supported range')
        if integer < minimum or integer > maximum:
            raise ValueError(f'{name} must be an integer in the supported range')
        return integer

    @staticmethod
    def _validated_loop_interval(value):
        try:
            interval = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Loop interval must be a finite non-negative number') from exc
        if not math.isfinite(interval) or not 0 <= interval <= _MAX_LOOP_INTERVAL:
            raise ValueError('Loop interval must be a finite non-negative number')
        return interval

    def _action_loop(self, step: Dict[str, Any]):
        """循环：支持计数/条件/遍历三种模式"""
        mode = str(self._render_value(step.get('mode', 'count'))).strip().lower()
        steps = step.get('steps', [])
        if not isinstance(steps, list):
            raise ValueError('Loop steps must be a list')
        interval = self._validated_loop_interval(step.get('interval', 0))

        if mode == 'count':
            # 计数循环
            times = self._validated_loop_integer(step.get('times', 1), 'times', 0, _MAX_LOOP_COUNT)
            logger.info('Starting count loop')
            for i in range(times):
                logger.info('Count loop iteration')
                self._execute_children(steps, 'steps')
                if interval > 0:
                    time.sleep(interval)
        
        elif mode == 'foreach':
            # 遍历循环
            items = step.get('items', [])
            if not isinstance(items, (list, tuple)):
                raise ValueError('foreach loop items must be a list')
            item_var = step.get('item_var', 'item')
            item_scope = step.get('item_scope', 'local')
            
            logger.info('Starting foreach loop')
            for idx, item in enumerate(items):
                logger.info('Foreach loop iteration')
                self._set_variable(item_var, item, item_scope)
                self._execute_children(steps, 'steps')
                if interval > 0:
                    time.sleep(interval)
        
        elif mode == 'condition':
            # 条件循环
            left = step.get('left', '')
            operator = step.get('operator', '==')
            right = step.get('right', '')
            
            max_loops = self._validated_loop_integer(
                step.get('max_loops', 10), 'max_loops', 0, _MAX_LOOP_COUNT,
            )
            logger.info('Starting conditional loop')
            loop_count = 0
            while loop_count < max_loops:
                left_val = self._render_value(left)
                right_val = self._render_value(right)
                if not self._eval_condition(left_val, operator, right_val):
                    break

                loop_count += 1
                logger.info('Conditional loop iteration')
                self._execute_children(steps, 'steps')
                if interval > 0:
                    time.sleep(interval)
        else:
            raise ValueError(f'Unsupported loop mode: {mode}')
    
    def _action_sequence(self, step: Dict[str, Any]):
        """顺序执行子步骤"""
        steps = step.get('steps', [])
        logger.info('Executing sequence')
        self._execute_children(steps, 'steps')
    
    def _action_try(self, step: Dict[str, Any]):
        """异常处理：try/catch/finally"""
        try_steps = step.get('try_steps', [])
        catch_steps = step.get('catch_steps', [])
        finally_steps = step.get('finally_steps', [])
        error_var = step.get('error_var', 'error')
        error_scope = step.get('error_scope', 'local')
        skip_finally = False

        logger.info("执行 try 块")
        try:
            try:
                self._execute_children(try_steps, 'try_steps')
            except Exception as e:
                self._ui_flow_unsafe_failure_seen = True
                if self._is_uncertain_device_error(e):
                    skip_finally = True
                    raise
                logger.warning('Caught error in try block (%s)', type(e).__name__)
                self._sanitize_exception_for_reporting(e)
                self._set_variable(error_var, str(e), error_scope)
                try:
                    self._execute_children(catch_steps, 'catch_steps')
                except Exception as catch_error:
                    self._ui_flow_unsafe_failure_seen = True
                    if self._is_uncertain_device_error(catch_error):
                        skip_finally = True
                    raise
        finally:
            if not skip_finally:
                logger.info("执行 finally 块")
                self._execute_children(finally_steps, 'finally_steps')

    def _get_ocr_helper(self):
        """Return the OCR helper configured for this worker runtime."""
        if self._ocr_helper is None:
            if not OCR_AVAILABLE:
                raise RuntimeError('OCR is unavailable; install EasyOCR and its local model files')
            self._ocr_helper = get_ocr_helper(
                languages=self.runtime.get('ocr_languages', ['en']),
                use_gpu=self.runtime.get('ocr_use_gpu', False),
            )
        return self._ocr_helper

    def _action_foreach_assert(self, step: Dict[str, Any]):
        """Click repeatedly and match OCR text, numbers, or reference images."""
        config = step.get('config') if isinstance(step.get('config'), dict) else {}
        options = {**config, **{key: value for key, value in step.items() if key != 'config'}}
        expected_list = options.get('expected_list', [])
        assert_type = options.get('assert_type', 'text')
        match_mode = options.get('match_mode', 'contains')

        if assert_type not in {'image', 'number', 'text'}:
            raise ValueError(f'Unsupported foreach_assert type: {assert_type}')
        if assert_type != 'image' and not OCR_AVAILABLE:
            raise RuntimeError('foreach_assert 需要 OCR 支持，请安装 easyocr')
        if not isinstance(expected_list, list) or not expected_list:
            raise ValueError('foreach_assert requires a non-empty expected_list')
        max_loops = self._validated_loop_integer(
            options.get('max_loops', 5), 'max_loops', 1, _MAX_LOOP_COUNT,
        )
        min_match_value = options.get('min_match', 1)
        if min_match_value in (None, '', False):
            min_match_value = 0
        min_match = self._validated_loop_integer(
            min_match_value, 'min_match', 0, max_loops,
        )
        try:
            interval = float(options.get('interval', 0.5))
            timeout = float(options.get('timeout', 5))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('foreach_assert interval and timeout must be numeric') from exc
        if (
            not math.isfinite(interval)
            or not 0 <= interval <= _MAX_FOREACH_ASSERT_WAIT
            or not math.isfinite(timeout)
            or not 0 <= timeout <= _MAX_FOREACH_ASSERT_WAIT
        ):
            raise ValueError(
                f'foreach_assert interval and timeout must be from 0 to {_MAX_FOREACH_ASSERT_WAIT} seconds'
            )
        if assert_type == 'text' and match_mode not in {'exact', 'contains'}:
            raise ValueError(f'Unsupported text match mode: {match_mode}')
        if assert_type == 'text' and match_mode == 'contains' and any(
            expected is None or (isinstance(expected, str) and expected == '')
            for expected in expected_list
        ):
            raise ValueError('foreach_assert text contains expected entries must be non-empty')

        click_config = {
            'selector_type': options.get('click_selector_type', 'image'),
            'selector': options.get('click_selector'),
            'selector_match': options.get('click_selector_match', 'exact'),
            'image_scope': options.get('click_image_scope') or options.get('image_scope', 'common'),
            'image_threshold': options.get('image_threshold', 0.7),
        }
        click_target = self._resolve_selector(click_config)
        if click_target is None:
            raise ValueError('foreach_assert requires a valid click_selector')

        matcher = None
        ocr_region = None
        if assert_type == 'image':
            image_scope = options.get('expected_image_scope') or options.get('image_scope', 'common')
            threshold = options.get('expected_image_threshold', options.get('image_threshold', 0.7))
            expected_images = []
            for expected in expected_list:
                if not isinstance(expected, str) or not expected:
                    raise ValueError('Image expected_list entries must be non-empty filenames')
                image_path = self._safe_image_path(image_scope, expected)
                if not os.path.isfile(image_path):
                    raise ValueError('Expected image does not exist')
                expected_images.append(Template(image_path, threshold=threshold))

            def match_image(deadline):
                for target in expected_images:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    if self._image_lookup_once(
                        target, timeout=min(_MAX_IMAGE_LOOKUP_TIMEOUT, remaining),
                    ) is not None:
                        return True
                return False

            matcher = match_image
        else:
            if not OCR_AVAILABLE:
                raise RuntimeError('Text/number foreach_assert requires OCR support')
            selector = options.get('ocr_selector')
            if options.get('ocr_selector_type', 'region') != 'region' or not selector:
                raise ValueError('Text/number foreach_assert requires an OCR region selector')
            if isinstance(selector, str):
                parts = [int(part.strip()) for part in selector.split(',')]
                if len(parts) != 4:
                    raise ValueError(f'Invalid OCR region: {selector}')
                ocr_region = tuple(parts)
            elif isinstance(selector, (list, tuple)) and len(selector) >= 4:
                ocr_region = tuple(int(value) for value in selector[:4])
            else:
                raise ValueError(f'Invalid OCR region: {selector}')

            if assert_type == 'text' and match_mode not in {'exact', 'contains'}:
                raise ValueError(f'Unsupported text match mode: {match_mode}')
            ocr = self._get_ocr_helper()

            def match_ocr_value(deadline):
                actual = (
                    ocr.recognize_region_number(ocr_region, deadline=deadline)
                    if assert_type == 'number'
                    else ocr.recognize_region_text(ocr_region, deadline=deadline)
                )
                for expected in expected_list:
                    if assert_type == 'number':
                        try:
                            expected_value = int(str(expected).replace(',', '').replace(' ', ''))
                        except (TypeError, ValueError) as exc:
                            raise ValueError('Number expected_list entries must be integers') from exc
                        if actual == expected_value:
                            return True
                    elif (
                        (match_mode == 'exact' and str(actual) == str(expected))
                        or (match_mode == 'contains' and str(expected) in str(actual))
                    ):
                        return True
                return False

            matcher = match_ocr_value

        matched_count = 0
        # A zero timeout keeps count-driven behavior under the same hard deadline.
        deadline = time.monotonic() + (
            timeout if timeout > 0 else _MAX_FOREACH_ASSERT_WAIT
        )
        for index in range(max_loops):
            remaining = deadline - time.monotonic()
            # Avoid another click when its settling interval cannot fit.
            if remaining <= 0 or (index > 0 and remaining < interval):
                break
            logger.info('foreach_assert iteration')
            click_position = self._resolve_action_target(
                click_target, timeout=min(_MAX_IMAGE_LOOKUP_TIMEOUT, remaining),
            )
            if time.monotonic() >= deadline:
                break
            self._visual_mutate(touch, click_position)

            remaining = deadline - time.monotonic()
            if interval > 0 and remaining > 0:
                time.sleep(min(interval, remaining))
            if time.monotonic() >= deadline:
                break

            if matcher(deadline) and time.monotonic() < deadline:
                matched_count += 1

        logger.info('foreach_assert completed')
        if matched_count < min_match:
            raise AssertionError(
                f'foreach_assert failed: expected at least {min_match} matches, got {matched_count}'
            )
