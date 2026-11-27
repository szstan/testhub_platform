# -*- coding: utf-8 -*-
"""
APP UI Flow 测试
"""
import os
import logging
from contextlib import ExitStack
from unittest.mock import patch

import pytest
import allure
from apps.app_automation.models import AppTestCase, AppTestExecution
from apps.app_automation.tasks import send_execution_update
from apps.app_automation import device_actions, secret_input
from apps.app_automation.utils.airtest_base import AirtestBase
from apps.app_automation.runners import ui_flow_runner as ui_flow_runner_module
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner
from apps.app_automation.scene_execution import (
    SCENE_CASE_CHANGED_MESSAGE,
    ScenePreparationError,
    prepare_scene_execution,
    validate_diagnostic_selection,
)


def _worker_environment_ready():
    required = ('APP_DEVICE_ID', 'APP_TEST_CASE_ID', 'APP_EXECUTION_ID')
    if not all(os.environ.get(name) for name in required):
        return False
    debug_requested = any(name in os.environ for name in (
        'APP_DEBUG_STEP_INDEX', 'APP_DEBUG_CASE_UPDATED_AT'
    ))
    return debug_requested or bool(os.environ.get('APP_PACKAGE_NAME'))


# The production worker selects this marker explicitly; security boundary tests
# stay outside the worker entry point and run in their own CI job. Keep this
# module free of pytest-django's django_db marker so the worker never creates a
# test database against the configured application database.
pytestmark = [
    pytest.mark.app_ui_flow,
    pytest.mark.skipif(
        not _worker_environment_ready(),
        reason='APP UI flow requires an explicitly provisioned device and execution fixture',
    ),
]


logger = logging.getLogger(__name__)


def _clear_device_action_marker(execution_id):
    # Appium keeps the device fence until the owning parent has closed the session.
    if not execution_id or os.environ.get('APP_APPIUM_EXECUTION_SESSION'):
        return
    from apps.app_automation.device_pool import DeviceLeaseError
    from apps.app_automation.execution_admission import clear_device_action_pending

    try:
        clear_device_action_pending(execution_id)
    except DeviceLeaseError:
        logger.debug('Device action marker was not clearable under the current lease fence')


def _clear_device_action_marker_on_safe_failure(execution_id):
    try:
        _clear_device_action_marker(execution_id)
    except Exception as exc:
        logger.warning('Unable to clear completed device marker (error_type=%s)', type(exc).__name__)


def _guard_airtest_mutation(call):
    def guarded(*args, **kwargs):
        try:
            return call(*args, **kwargs)
        except device_actions.DeviceActionUncertainError:
            raise
        except Exception:
            raise device_actions.DeviceActionUncertainError(
                'Airtest mutation result is uncertain'
            ) from None

    return guarded


def _guard_airtest_mutations():
    stack = ExitStack()
    for name in ('touch', 'double_click', 'swipe', 'airtest_text'):
        original = getattr(ui_flow_runner_module, name)
        stack.enter_context(
            patch.object(ui_flow_runner_module, name, _guard_airtest_mutation(original))
        )
    return stack


def _open_app_or_raise_uncertain(airtest, package_name):
    try:
        started = airtest.open_app(package_name)
    except Exception:
        raise device_actions.DeviceActionUncertainError(
            'App launch result is uncertain'
        ) from None
    if not started:
        raise device_actions.DeviceActionUncertainError(
            'App launch result is uncertain'
        )


def _diagnostic_selection():
    raw_index = os.environ.get('APP_DEBUG_STEP_INDEX')
    expected_case_updated_at = os.environ.get('APP_DEBUG_CASE_UPDATED_AT')
    if raw_index is None and expected_case_updated_at is None:
        return None
    if (
        raw_index is None
        or not raw_index.isascii()
        or not raw_index.isdecimal()
        or expected_case_updated_at is None
    ):
        raise ScenePreparationError(SCENE_CASE_CHANGED_MESSAGE)
    return int(raw_index), expected_case_updated_at


def _validate_diagnostic_case_before_device(test_case_id):
    selection = _diagnostic_selection()
    if selection is None:
        return None
    test_case = AppTestCase.objects.only('id', 'ui_flow', 'updated_at').get(pk=test_case_id)
    validate_diagnostic_selection(test_case, *selection)
    return selection


def _make_progress_callback(execution_id):
    """
    创建进度回调函数，在 pytest 子进程中直接更新数据库和 WebSocket。
    
    进度映射：步骤执行占整体的 10%~90%，前后留给环境准备和报告生成。
    """
    if not execution_id:
        return None
    
    def callback(current_step, total_steps, step_name, status):
        """
        进度回调：每完成一个步骤更新进度。
        
        进度计算：10 + (current_step / total_steps) * 80
        即步骤全部完成时进度为 90%，留 10% 给报告生成阶段。
        """
        if total_steps <= 0:
            return
        
        # 只在步骤完成（passed/failed）时更新，running 状态不更新避免频繁写库
        if status == 'running':
            return
        
        progress = int(10 + (current_step / total_steps) * 80)
        progress = min(progress, 90)  # 上限 90%，留给报告阶段
        
        message = f"步骤 {current_step}/{total_steps}: {step_name} - {'通过' if status == 'passed' else '失败'}"
        
        try:
            AppTestExecution.objects.filter(id=execution_id).update(progress=progress)
        except Exception as e:
            logger.debug('更新执行进度失败 (%s)', type(e).__name__)
        
        send_execution_update(
            execution_id,
            status='running',
            progress=progress,
            message=message,
        )
    
    return callback


@allure.feature("APP自动化测试")
class TestAppFlow:
    """APP UI Flow 测试类"""
    
    @pytest.fixture(scope="class")
    def airtest(self, device_id, username):
        """Airtest 基础环境"""
        try:
            _validate_diagnostic_case_before_device(os.environ.get('APP_TEST_CASE_ID'))
        except Exception:
            _clear_device_action_marker_on_safe_failure(os.environ.get('APP_EXECUTION_ID'))
            raise
        if os.environ.get('APP_APPIUM_EXECUTION_SESSION'):
            yield None
            return
        airtest_base = AirtestBase(
            device_id=device_id,
            username=username,
            adb_path=os.environ.get('APP_ADB_PATH'),
        )
        
        # 设置环境
        if not airtest_base.setup_airtest():
            pytest.fail("Airtest 环境设置失败")
        
        yield airtest_base
        
        # 清理
        airtest_base.teardown_airtest()
    
    @allure.story("执行UI Flow")
    def test_execute_ui_flow(self, test_case_id, package_name, execution_id, airtest, username):
        """执行 UI Flow 测试"""
        test_case = None
        execution = None
        try:
            test_case = AppTestCase.objects.get(id=test_case_id)
            if getattr(test_case, 'pk', None) is not None:
                execution = AppTestExecution.objects.select_related('dataset', 'user').get(pk=execution_id)
            # Resolve project authorization, data and secret handling before any app launch.
            selection = _diagnostic_selection()
            if selection is None:
                ui_flow, variables = prepare_scene_execution(test_case, execution)
            else:
                ui_flow, variables = prepare_scene_execution(
                    test_case,
                    execution,
                    step_index=selection[0],
                    expected_case_updated_at=selection[1],
                )
        except Exception:
            _clear_device_action_marker_on_safe_failure(execution_id)
            raise
        allure.dynamic.title(f"用例名称: {test_case.name}")
        allure.dynamic.suite("APP自动化测试")
        
        session_id = os.environ.get('APP_APPIUM_EXECUTION_SESSION')
        driver = None
        guard = None
        visual_device = None
        if getattr(execution, 'engine', 'legacy') == 'appium':
            from apps.app_automation.appium_execution import attach_execution_session, guard_execution_session

            if not session_id:
                raise RuntimeError('Appium worker session is unavailable')
            driver = attach_execution_session(execution.pk, session_id)

            def guard():
                guard_execution_session(execution.pk, session_id)

            def load_visual_device():
                nonlocal visual_device
                guard()
                visual_device = AirtestBase(
                    device_id=execution.device.device_id,
                    username=username,
                    adb_path=os.environ.get('APP_ADB_PATH'),
                )
                if not visual_device.setup_airtest():
                    raise RuntimeError('The Android visual input connection is unavailable')

            if selection is None and package_name:
                from apps.app_automation.appium_locator import apply_native_mutation
                from apps.app_automation.appium_runtime import AppiumOutcomeUnknown
                from apps.app_automation.appium_execution import _mark_session_unknown
                with allure.step('启动应用'):
                    try:
                        apply_native_mutation(lambda: driver.activate_app(package_name), guard=guard)
                    except AppiumOutcomeUnknown:
                        _mark_session_unknown(session_id, 'appium_activate_outcome_unknown')
                        raise
        elif selection is None and package_name:
            with allure.step(f"启动应用: {package_name}"):
                _open_app_or_raise_uncertain(airtest, package_name)
        elif selection is None:
            allure.attach(
                "未配置应用包名，跳过启动应用步骤",
                name="启动应用",
                attachment_type=allure.attachment_type.TEXT
            )
        
        # 执行 UI Flow
        try:
            driver_options = {} if driver is None else {
                'appium_driver': driver, 'device_guard': guard, 'load_visual_device': load_visual_device,
                'diagnostic_root_index': selection[0] if selection is not None else None,
                'diagnostic_case_revision': test_case.updated_at.isoformat(),
            }
            runner = UiFlowRunner(username=username, execution_id=execution_id, **driver_options)
        except Exception:
            _clear_device_action_marker_on_safe_failure(execution_id)
            raise
        
        progress_callback = _make_progress_callback(execution_id)

        runtime_config = getattr(test_case, 'runtime_config', None)
        runtime_config = runtime_config if isinstance(runtime_config, dict) else {}
        runner_runtime = {**runtime_config, 'stop_on_error': True}
        if package_name:
            runner_runtime['package_name'] = package_name

        try:
            with _guard_airtest_mutations():
                with allure.step("执行 UI Flow"):
                    result = runner.run(
                        ui_flow=ui_flow,
                        variables=variables,
                        runtime=runner_runtime,
                        progress_callback=progress_callback
                    )
        except ui_flow_runner_module.ReadOnlyBusinessAssertionFailure:
            _clear_device_action_marker_on_safe_failure(execution_id)
            raise
        except device_actions.DeviceActionUncertainError:
            raise
        except secret_input.SecretInputError as exc:
            if not isinstance(exc, (
                secret_input.SecretInputValidationError,
                secret_input.SecretInputDeviceError,
            )):
                raise
            _clear_device_action_marker_on_safe_failure(execution_id)
            raise
        except (ValueError, device_actions.DeviceActionError):
            _clear_device_action_marker_on_safe_failure(execution_id)
            raise
        finally:
            if visual_device is not None:
                visual_device.teardown_airtest()

        # Normal runner return proves that its device I/O has stopped, even when
        # the following business-result assertions fail or the run is unverified.
        _clear_device_action_marker(execution.pk if execution is not None else None)
        
        # 验证结果
        with allure.step("验证执行结果"):
            assert result['failed'] == 0, f"UI Flow 执行失败，失败步骤: {result['failed']}"
            assert result['passed'] > 0, "没有执行任何步骤"
        
        assertions = result.get('assertions', 0)
        verification = 'verified' if assertions > 0 else 'unverified'
        allure.dynamic.label('testhub_verification', verification)
        allure.attach(
            f"总步骤: {result['total']}\n"
            f"通过: {result['passed']}\n"
            f"失败: {result['failed']}\n"
            f"已执行断言: {assertions}",
            name="执行统计",
            attachment_type=allure.attachment_type.TEXT
        )
        if not assertions:
            pytest.skip('步骤已执行，但没有业务结果断言；请添加断言后再判断通过或失败')
