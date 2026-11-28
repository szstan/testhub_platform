# -*- coding: utf-8 -*-
"""
APP自动化测试常量定义
"""


class ExecutionEngine:
    """Persisted APP scene engine identifiers."""
    LEGACY = 'legacy'
    APPIUM = 'appium'


class DeviceStatus:
    """设备状态"""
    AVAILABLE = 'available'
    LOCKED = 'locked'
    OFFLINE = 'offline'
    ONLINE = 'online'


class ExecutionStatus:
    """执行状态（任务生命周期）"""
    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'
    ERROR = 'error'
    STOPPED = 'stopped'

    # 向后兼容旧值
    SUCCESS = 'success'
    FAILED = 'failed'


class ExecutionResult:
    """测试结果（用例真实通过/失败）"""
    PASSED = 'passed'
    FAILED = 'failed'
    SKIPPED = 'skipped'
    UNVERIFIED = 'unverified'


class ElementType:
    """元素类型"""
    IMAGE = 'image'      # 图片元素
    POS = 'pos'          # 坐标元素
    REGION = 'region'    # 区域元素
    NATIVE = 'native'    # Android native locator


ANDROID_PACKAGE_NAME_PATTERN = r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+'
NATIVE_ELEMENT_STRATEGIES = ('id', 'accessibility_id')
APP_TEST_CASE_MAX_TIMEOUT_SECONDS = 3600
APP_API_REQUEST_MAX_TIMEOUT_SECONDS = 60.0
