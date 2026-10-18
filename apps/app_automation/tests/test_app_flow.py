# -*- coding: utf-8 -*-
"""
APP UI Flow 测试
"""
import os
import logging
import pytest
import allure
from apps.app_automation.models import AppTestCase, AppTestExecution
from apps.app_automation.tasks import send_execution_update
from apps.app_automation.utils.airtest_base import AirtestBase
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner


# The production worker selects this marker explicitly; security boundary tests
# stay outside the worker entry point and run in their own CI job. Keep this
# module free of pytest-django's django_db marker so the worker never creates a
# test database against the configured application database.
pytestmark = [
    pytest.mark.app_ui_flow,
    pytest.mark.skipif(
        not all(os.environ.get(name) for name in (
            'APP_DEVICE_ID', 'APP_PACKAGE_NAME', 'APP_TEST_CASE_ID', 'APP_EXECUTION_ID'
        )),
        reason='APP UI flow requires an explicitly provisioned device and execution fixture',
    ),
]


logger = logging.getLogger(__name__)


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
            logger.debug(f"更新执行进度失败: {e}")
        
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
        # 获取测试用例
        test_case = AppTestCase.objects.get(id=test_case_id)
        allure.dynamic.title(f"用例名称: {test_case.name}")
        allure.dynamic.suite("APP自动化测试")
        
        if package_name:
            with allure.step(f"启动应用: {package_name}"):
                assert airtest.open_app(package_name), f"应用启动失败: {package_name}"
        else:
            allure.attach(
                "未配置应用包名，跳过启动应用步骤",
                name="启动应用",
                attachment_type=allure.attachment_type.TEXT
            )
        
        # 执行 UI Flow
        runner = UiFlowRunner(username=username)
        
        if isinstance(test_case.ui_flow, list):
            ui_flow = test_case.ui_flow
        elif isinstance(test_case.ui_flow, dict):
            ui_flow = test_case.ui_flow.get('steps', [])
        else:
            ui_flow = []
        variables = test_case.variables or []
        
        # 创建进度回调
        progress_callback = _make_progress_callback(execution_id)
        
        with allure.step("执行 UI Flow"):
            result = runner.run(
                ui_flow=ui_flow,
                variables=variables,
                runtime={'stop_on_error': True},
                progress_callback=progress_callback
            )
        
        # 验证结果
        with allure.step("验证执行结果"):
            assert result['failed'] == 0, f"UI Flow 执行失败，失败步骤: {result['failed']}"
            assert result['passed'] > 0, "没有执行任何步骤"
        
        allure.attach(
            f"总步骤: {result['total']}\n"
            f"通过: {result['passed']}\n"
            f"失败: {result['failed']}",
            name="执行统计",
            attachment_type=allure.attachment_type.TEXT
        )
