# -*- coding: utf-8 -*-
"""
测试执行器 - pytest + Allure 集成
"""
import os
import sys
import subprocess
import glob
import importlib
import json
import shutil
from pathlib import Path
from typing import Optional, Dict, Any
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


class AppTestWorkerError(RuntimeError):
    """APP worker runtime is not safe to use for an execution."""


class AppTestProcessTerminationError(AppTestWorkerError):
    """The child process could not be confirmed stopped; keep its device busy."""


def _check_cancel(cancel_check):
    if cancel_check is None:
        return
    try:
        cancel_check()
    except AppTestProcessTerminationError:
        raise AppTestProcessTerminationError(
            '无法确认测试进程已退出，请检查设备和执行进程'
        ) from None
    except Exception as exc:
        if (
            type(exc) is RuntimeError
            and len(exc.args) == 1
            and isinstance(exc.args[0], str)
            and exc.args[0] in {
                'device lease lost',
                'APP执行已停止或不再处于运行状态',
                'APP执行批次已停止或已结算',
            }
        ):
            raise RuntimeError(exc.args[0]) from None
        raise AppTestWorkerError(
            'APP execution cancelled or device lease validation failed'
        ) from None


def _safe_worker_error_message(error):
    if type(error) is AppTestWorkerError and error.args == ('worker preflight failed',):
        return 'worker preflight failed'
    if error.args == ('Allure report generation failed after pytest completed',):
        return 'Allure report generation failed after pytest completed'
    if type(error) is AppTestWorkerError and error.args in (
        ('Appium execution is missing a bound device, application, or lease',),
        ('Appium worker configuration or client is unavailable',),
        ('Appium requires an admitted device execution',),
    ):
        return error.args[0]
    return (
        'APP worker failed; check Python dependencies, ADB and Allure configuration, '
        'then retry'
    )


class AppTestExecutor:
    """APP测试执行器，封装 pytest 执行逻辑"""

    _REQUIRED_WORKER_MODULES = (
        ('airtest.core.api', 'airtest'),
        ('allure', 'allure-python-commons'),
        ('allure_pytest', 'allure-pytest'),
    )
    
    def __init__(self, base_path: Optional[str] = None):
        """
        初始化测试执行器
        
        Args:
            base_path: 基础路径，默认使用 Django BASE_DIR
        """
        if base_path is None:
            self.base_path = settings.BASE_DIR
        else:
            self.base_path = base_path
        
        if not os.path.exists(self.base_path):
            raise ValueError("测试项目路径不存在")
        
        self._current_process: Optional[subprocess.Popen] = None
        
        logger.info("初始化 AppTestExecutor")
    
    def run_tests(
        self,
        test_case_id: int,
        device_id: str,
        package_name: str,
        execution_id: Optional[int] = None,
        username: Optional[str] = None,
        cancel_check=None,
        step_index: Optional[int] = None,
        expected_case_updated_at: Optional[str] = None,
        before_device_io=None,
        engine='legacy',
        batch_dispatch_id=None,
        batch_execution_ids=None,
    ) -> Dict[str, Any]:
        """
        运行APP测试用例并生成报告
        
        Args:
            test_case_id: 测试用例ID
            device_id: 设备ID
            package_name: 应用包名
            execution_id: 执行记录ID
            username: 执行用户名，用于日志目录分组
            
        Returns:
            执行结果字典
        """
        logger.info("开始执行APP测试")
        diagnostic_requested = step_index is not None or expected_case_updated_at is not None
        if diagnostic_requested and (
            type(step_index) is not int
            or step_index < 0
            or not isinstance(expected_case_updated_at, str)
            or not expected_case_updated_at
            or len(expected_case_updated_at) > 64
        ):
            raise AppTestWorkerError('Invalid saved-step diagnostic selection')
        _check_cancel(cancel_check)

        # Fail before starting pytest so a broken worker is reported as an execution
        # error instead of a misleading completed/skipped test run.
        if engine not in {'legacy', 'appium'}:
            raise AppTestWorkerError('Unknown APP execution engine')
        using_appium = engine == 'appium'
        if using_appium and (not execution_id or not callable(before_device_io)):
            raise AppTestWorkerError('Appium requires an admitted device execution')
        try:
            worker_runtime = self._preflight_worker()
            if using_appium:
                from ..appium_execution import preflight_execution_session
                try:
                    preflight_execution_session(execution_id, device_id, package_name)
                except Exception:
                    raise AppTestWorkerError('Appium worker configuration or client is unavailable') from None
        except AppTestWorkerError as exc:
            logger.error(
                "APP worker preflight failed (error_type=%s)", type(exc).__name__
            )
            raise AppTestWorkerError(_safe_worker_error_message(exc)) from None
        except Exception as exc:
            logger.error(
                "APP worker preflight failed (error_type=%s)", type(exc).__name__
            )
            raise AppTestWorkerError(
                'APP worker preflight failed; check Python dependencies, ADB and Allure configuration'
            ) from None
        original_cwd = os.getcwd()
        device_io_admitted = False
        pytest_process_started = False
        pytest_process_completed = False
        exit_code = None
        test_results = None
        report_path = None
        owned_session = None
        appium_session_closed = False
        
        try:
            # 切换到项目根目录
            os.chdir(self.base_path)
            
            # 准备环境变量
            env = os.environ.copy()
            env.pop('APP_DEBUG_STEP_INDEX', None)
            env.pop('APP_DEBUG_CASE_UPDATED_AT', None)
            env.pop('APP_APPIUM_EXECUTION_SESSION', None)
            if diagnostic_requested:
                env['APP_DEBUG_STEP_INDEX'] = str(step_index)
                env['APP_DEBUG_CASE_UPDATED_AT'] = expected_case_updated_at
            env['PYTHONPATH'] = self._build_pythonpath()
            env['DJANGO_SETTINGS_MODULE'] = 'backend.settings'
            # 强制子进程使用 UTF-8，避免 Windows 下 gbk 解码错误
            env['PYTHONUTF8'] = '1'
            env['PYTHONIOENCODING'] = 'utf-8'
            env['APP_ADB_PATH'] = worker_runtime['adb_path']
            adb_dir = str(Path(worker_runtime['adb_path']).parent)
            env['PATH'] = os.pathsep.join(
                part for part in (adb_dir, env.get('PATH', '')) if part
            )
            env['APP_TEST_CASE_ID'] = str(test_case_id)
            env['APP_DEVICE_ID'] = device_id
            env['APP_PACKAGE_NAME'] = package_name
            if execution_id:
                env['APP_EXECUTION_ID'] = str(execution_id)
            if username:
                env['APP_USERNAME'] = username
            
            # Allure 结果目录
            allure_results_dir = self._get_allure_results_dir(execution_id)
            os.makedirs(allure_results_dir, exist_ok=True)
            
            # 构建 pytest 参数
            pytest_args = self._build_pytest_args(allure_results_dir)
            
            logger.info("启动 pytest worker")
            _check_cancel(cancel_check)
            if before_device_io is not None:
                if not callable(before_device_io):
                    raise AppTestWorkerError('Invalid device action fence callback')
                before_device_io()
                device_io_admitted = True
            if using_appium:
                from ..appium_execution import start_execution_session
                batch_context = {}
                if batch_dispatch_id is not None or batch_execution_ids is not None:
                    batch_context = {
                        'batch_dispatch_id': batch_dispatch_id,
                        'batch_execution_ids': batch_execution_ids,
                    }
                owned_session = start_execution_session(execution_id, device_id, package_name, **batch_context)
                env['APP_APPIUM_EXECUTION_SESSION'] = str(owned_session.id)
                _check_cancel(cancel_check)
            process = subprocess.Popen(
                pytest_args,
                cwd=self.base_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding='utf-8',
                errors='ignore',
                env=env,
            )
            pytest_process_started = True
            self._current_process = process
            while True:
                try:
                    _check_cancel(cancel_check)
                except (AppTestWorkerError, RuntimeError):
                    if owned_session is not None:
                        try:
                            exit_code = process.wait(timeout=4)
                        except subprocess.TimeoutExpired:
                            try:
                                exit_code = process.poll()
                            except Exception:
                                exit_code = None
                            if type(exit_code) is int:
                                pytest_process_completed = True
                                self._current_process = None
                        else:
                            pytest_process_completed = True
                            self._current_process = None
                    raise
                try:
                    exit_code = process.wait(timeout=0.25)
                    pytest_process_completed = True
                    break
                except subprocess.TimeoutExpired:
                    continue

            self._current_process = None
            logger.info('pytest worker finished, exit_code=%s', exit_code)
            if owned_session is not None:
                from ..appium_execution import finish_execution_session
                session_to_close, owned_session = owned_session, None
                appium_session_closed = finish_execution_session(session_to_close, child_exit_confirmed=True)
                if not appium_session_closed:
                    raise RuntimeError('Appium session requires device recovery')

            _check_cancel(cancel_check)
            test_results = self._parse_allure_results(allure_results_dir)
            _check_cancel(cancel_check)
            report_path = self._generate_allure_report(execution_id)
            if not report_path:
                if using_appium:
                    try:
                        from ..app_flow_diagnostics import record_flow_failure
                        record_flow_failure(
                            execution_id, step_name='', step_path='', code='report_generation_failed', duration_ms=0,
                        )
                    except Exception as exc:
                        logger.warning('Appium failure details could not be recorded (%s)', type(exc).__name__)
                return {
                    'success': False,
                    'exit_code': exit_code,
                    'report_path': None,
                    'test_results': test_results,
                    'error': 'Allure report generation failed after pytest completed',
                    'device_io_admitted': device_io_admitted,
                    'pytest_process_started': pytest_process_started,
                    'pytest_process_completed': pytest_process_completed,
                    **({'appium_session_closed': appium_session_closed} if using_appium else {}),
                }

            return {
                'success': exit_code == 0,
                'exit_code': exit_code,
                'report_path': report_path,
                'test_results': test_results,
                'device_io_admitted': device_io_admitted,
                'pytest_process_started': pytest_process_started,
                'pytest_process_completed': pytest_process_completed,
                **({'appium_session_closed': appium_session_closed} if using_appium else {}),
            }
            
        except AppTestProcessTerminationError:
            logger.error("APP worker could not confirm process termination")
            raise AppTestProcessTerminationError(
                '无法确认测试进程已退出，请检查设备和执行进程'
            ) from None
        except AppTestWorkerError as exc:
            logger.error("APP worker execution failed (error_type=%s)", type(exc).__name__)
            raise AppTestWorkerError(_safe_worker_error_message(exc)) from None
        except Exception as exc:
            if cancel_check is not None:
                try:
                    _check_cancel(cancel_check)
                except Exception:
                    raise
            logger.error("APP test execution failed (error_type=%s)", type(exc).__name__)
            result = {
                'success': False,
                'exit_code': exit_code if pytest_process_completed else None,
                'error': 'APP test execution failed; check worker configuration and retry',
                'device_io_admitted': device_io_admitted,
                'pytest_process_started': pytest_process_started,
                'pytest_process_completed': pytest_process_completed,
                **({'appium_session_closed': appium_session_closed} if using_appium else {}),
            }
            if test_results is not None:
                result['test_results'] = test_results
            if report_path:
                result['report_path'] = report_path
            return result
        finally:
            try:
                if self._current_process is not None:
                    if self.stop():
                        pytest_process_completed = True
            finally:
                try:
                    if owned_session is not None:
                        from ..appium_execution import finish_execution_session
                        finish_execution_session(
                            owned_session,
                            child_exit_confirmed=pytest_process_completed or not pytest_process_started,
                        )
                finally:
                    os.chdir(original_cwd)
    
    def _build_pytest_args(self, allure_results_dir: str) -> list:
        """构建只执行 APP UI Flow 的 pytest 参数。"""
        return [
            sys.executable, '-m', 'pytest',
            'apps/app_automation/tests/test_app_flow.py',
            '-m', 'app_ui_flow',
            # UI Flow uses the configured application database. Do not let
            # pytest-django create a production test database in the worker.
            '-p', 'no:django',
            '-o', 'addopts=',
            '-s', '-v',
            '--alluredir', allure_results_dir,
            '--tb=short',
        ]

    def _preflight_worker(self) -> Dict[str, str]:
        """校验 APP worker 的 Python、Allure 和 ADB 运行时依赖。"""
        failures = []

        for module_name, distribution_name in self._REQUIRED_WORKER_MODULES:
            try:
                importlib.import_module(module_name)
            except Exception as exc:
                failures.append(
                    f"unable to import {distribution_name} (module {module_name}; "
                    f"{type(exc).__name__})"
                )

        configured_adb = os.environ.get('APP_ADB_PATH', 'adb').strip() or 'adb'
        adb_path = shutil.which(configured_adb)
        if not adb_path:
            failures.append(
                "ADB executable was not found; install android-tools or set APP_ADB_PATH"
            )
        else:
            try:
                adb_result = subprocess.run(
                    [adb_path, 'version'],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if adb_result.returncode != 0:
                    failures.append(
                        f"ADB executable failed `adb version` (exit {adb_result.returncode})"
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(
                    f"ADB executable could not run ({type(exc).__name__})"
                )

        allure_path = self._find_allure_command()
        if not allure_path:
            failures.append(
                "Allure command is missing; provide the bundled "
                "allure/bin/allure executable"
            )
        elif os.name != 'nt' and not os.access(allure_path, os.X_OK):
            failures.append("Allure command is not executable; check the bundled installation")
        else:
            try:
                allure_result = subprocess.run(
                    [allure_path, '--version'],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if allure_result.returncode != 0:
                    failures.append(
                        f"Allure command failed `allure --version` "
                        f"(exit {allure_result.returncode})"
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(
                    f"Allure command could not run ({type(exc).__name__})"
                )

        if failures:
            raise AppTestWorkerError(
                "APP automation worker preflight failed: " + '; '.join(failures)
            )

        logger.info("APP automation worker preflight passed")
        return {'adb_path': adb_path, 'allure_path': allure_path}

    def _build_pythonpath(self) -> str:
        """构建 PYTHONPATH 环境变量"""
        python_path_parts = [
            str(self.base_path),
            os.path.join(str(self.base_path), 'apps'),
        ]
        
        # 添加 sys.path 中的路径
        for p in sys.path:
            if p and os.path.exists(str(p)) and str(p) not in python_path_parts:
                p_str = str(p)
                if 'site-packages' in p_str or not p_str.endswith('.exe'):
                    python_path_parts.append(p_str)
        
        return os.pathsep.join(python_path_parts)
    
    def _get_allure_results_dir(self, execution_id: Optional[int] = None) -> str:
        """获取 Allure 结果目录: media/app-automation/allure-results/"""
        base_dir = os.path.join(settings.MEDIA_ROOT, 'app-automation', 'allure-results')
        
        if execution_id:
            return os.path.join(base_dir, f'execution_{execution_id}')
        
        return base_dir
    
    def _get_allure_report_dir(self, execution_id: Optional[int] = None) -> str:
        """获取 Allure 报告目录: media/app-automation/allure-reports/"""
        base_dir = os.path.join(settings.MEDIA_ROOT, 'app-automation', 'allure-reports')
        
        if execution_id:
            return os.path.join(base_dir, f'execution_{execution_id}')
        
        return base_dir
    
    def _generate_allure_report(self, execution_id: Optional[int] = None) -> Optional[str]:
        """
        生成 Allure 报告
        
        Args:
            execution_id: 执行记录ID
            
        Returns:
            报告目录路径，失败返回 None
        """
        try:
            allure_results_dir = self._get_allure_results_dir(execution_id)
            report_dir = self._get_allure_report_dir(execution_id)
            
            # 确保目录存在
            os.makedirs(report_dir, exist_ok=True)
            
            # 查找 allure 命令
            allure_path = self._find_allure_command()
            
            if not allure_path:
                logger.warning("未找到 Allure 命令，跳过报告生成")
                return None
            
            # 生成报告
            cmd = [allure_path, 'generate', allure_results_dir, '-o', report_dir, '--clean']
            
            logger.info("Generating Allure report")
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )

            if result.returncode == 0:
                logger.info("Allure report generation succeeded")
                return report_dir
            logger.error(
                "Allure report generation failed (exit_code=%s)", result.returncode
            )
            return None
                
        except subprocess.TimeoutExpired:
            logger.error("Allure 报告生成超时")
            return None
        except Exception as exc:
            logger.error(
                "Allure report generation failed (error_type=%s)", type(exc).__name__
            )
            return None
    
    def _find_allure_command(self) -> Optional[str]:
        """查找项目内置的 Allure 命令"""
        import platform
        
        base_dir = Path(__file__).resolve().parent.parent.parent.parent
        if platform.system() == 'Windows':
            builtin_allure = base_dir / 'allure' / 'bin' / 'allure.bat'
        else:
            builtin_allure = base_dir / 'allure' / 'bin' / 'allure'
        
        if builtin_allure.exists():
            logger.info("Using bundled Allure executable")
            return str(builtin_allure)
        
        logger.warning("未找到项目内置 Allure，请确认 allure 目录存在")
        return None
    
    def _parse_allure_results(self, results_dir: str) -> Dict[str, Any]:
        """
        解析 Allure 测试结果
        
        Args:
            results_dir: Allure 结果目录
            
        Returns:
            测试结果统计
        """
        try:
            # 查找所有测试结果文件
            result_files = glob.glob(os.path.join(results_dir, '*-result.json'))
            
            total = 0
            passed = 0
            failed = 0
            skipped = 0
            broken = 0
            unverified = 0

            logger.debug("Parsing Allure results")
            for result_file in result_files:
                try:
                    with open(result_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)

                        status = str(data.get('status', '')).lower()
                        labels = data.get('labels') or []
                        if isinstance(labels, list) and any(
                            isinstance(label, dict)
                            and label.get('name') == 'testhub_verification'
                            and label.get('value') == 'unverified'
                            for label in labels
                        ):
                            unverified += 1
                        total += 1

                        if status == 'passed':
                            passed += 1
                        elif status == 'broken':
                            # broken既计入broken也算作失败
                            broken += 1
                            failed += 1
                        elif status == 'failed':
                            failed += 1
                        elif status == 'skipped':
                            skipped += 1
                        else:
                            # 其他未知状态同样视为失败，保证不会被误判为通过
                            failed += 1
                            logger.debug("Unknown Allure status treated as failed")

                except Exception as exc:
                    total += 1
                    failed += 1
                    broken += 1
                    logger.warning(
                        "Failed to parse an Allure result (error_type=%s)",
                        type(exc).__name__,
                    )
            
            logger.info(f"测试结果统计: 总数={total}, 通过={passed}, 失败={failed}, 跳过={skipped}, broken={broken}")
            
            return {
                'total': total,
                'passed': passed,
                'failed': failed,
                'skipped': skipped,
                'broken': broken,
                'unverified': unverified,
            }
            
        except Exception as exc:
            logger.error(
                "Allure results parsing failed (error_type=%s)", type(exc).__name__
            )
            return {
                'total': 0,
                'passed': 0,
                'failed': 0,
                'skipped': 0,
                'broken': 0,
                'unverified': 0,
            }
    
    def calculate_progress(self, execution_id: Optional[int] = None) -> int:
        """
        计算测试进度
        
        Args:
            execution_id: 执行记录ID
            
        Returns:
            进度百分比 (0-100)
        """
        try:
            results_dir = self._get_allure_results_dir(execution_id)
            
            if not os.path.exists(results_dir):
                return 0
            
            result_files = glob.glob(os.path.join(results_dir, '*-result.json'))
            
            if not result_files:
                return 0
            
            # 简单估算：每有一个结果文件，进度增加
            # 实际应用中可以根据总步骤数来计算
            file_count = len(result_files)
            progress = min(file_count * 10, 100)  # 假设有10个步骤
            
            return progress
            
        except Exception as exc:
            logger.error("Progress calculation failed (error_type=%s)", type(exc).__name__)
            return 0
    
    def stop(self):
        """Stop the child; return true only when it exited before parent termination."""
        process = self._current_process
        if process is None:
            return False

        try:
            returncode = process.poll()
        except Exception:
            returncode = None
        if type(returncode) is int:
            self._current_process = None
            logger.info('pytest worker had already exited, exit_code=%s', returncode)
            return True

        termination_requested = False
        try:
            try:
                process.terminate()
                termination_requested = True
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                logger.warning("测试执行被强制终止")
            else:
                logger.info("测试执行已停止")
            self._current_process = None
            return False
        except Exception:
            try:
                exited = process.poll() is not None
            except Exception:
                exited = False
            if not exited:
                logger.error("无法确认测试进程已退出，保留设备占用")
                raise AppTestProcessTerminationError(
                    '无法确认测试进程已退出，请检查设备和执行进程'
                ) from None
            self._current_process = None
            return not termination_requested
