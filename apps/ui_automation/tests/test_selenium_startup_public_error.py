import logging

import pytest
from django.contrib.auth import get_user_model

from apps.core.scheduler import ScheduleClaimLost
from apps.ui_automation.models import (
    TestCase as UiTestCase,
    TestCaseExecution as UiTestCaseExecution,
    TestExecution as UiTestExecution,
    TestSuite as UiTestSuite,
    TestSuiteTestCase as UiTestSuiteTestCase,
    UiProject,
)
from apps.ui_automation.test_executor import TestExecutor as Executor


User = get_user_model()


@pytest.mark.django_db
@pytest.mark.parametrize('browser', ['chrome', 'safari'])
def test_suite_selenium_startup_failure_persists_public_error_and_logs_driver_details(
    monkeypatch, caplog, browser,
):
    user = User.objects.create_user(username=f'selenium-startup-{browser}')
    project = UiProject.objects.create(
        name=f'{browser} startup project',
        base_url='https://example.test',
        owner=user,
    )
    suite = UiTestSuite.objects.create(project=project, name=f'{browser} startup suite')
    cases = [
        UiTestCase.objects.create(project=project, name=f'{browser} startup case {index}', created_by=user)
        for index in range(2)
    ]
    for case in cases:
        UiTestSuiteTestCase.objects.create(test_suite=suite, test_case=case)
    execution = UiTestExecution.objects.create(
        project=project,
        test_suite=suite,
        status='RUNNING',
        engine='selenium',
        browser=browser,
        executed_by=user,
    )
    driver_details = 'fixture-only driver diagnostics at /private/driver/path'

    executor = Executor(
        suite, engine='selenium', browser=browser, executed_by=user, execution=execution,
    )
    executor.test_cases = cases

    def fail_startup():
        raise RuntimeError(driver_details)

    monkeypatch.setattr(executor, 'create_selenium_driver', fail_startup)
    caplog.set_level(logging.ERROR, logger='apps.ui_automation.test_executor')

    executor.run_with_selenium()

    public_error = executor._selenium_startup_error_message()
    assert '浏览器启动失败' in public_error
    assert ('safaridriver --enable' in public_error) if browser == 'safari' else (
        'Selenium WebDriver' in public_error and '驱动配置' in public_error
    )
    execution.refresh_from_db()
    assert execution.status == 'FAILED'
    assert execution.total_cases == 2
    assert execution.passed_cases == 0
    assert execution.failed_cases == 2
    assert execution.skipped_cases == 0
    assert execution.error_message == ''
    assert execution.result_data == {
        'test_cases': executor.results,
        'summary': {'total': 2, 'passed': 0, 'failed': 2, 'skipped': 0, 'pass_rate': 0.0},
    }
    assert len(executor.results) == 2
    assert all(result['status'] == 'failed' and result['error'] == public_error for result in executor.results)
    assert all(driver_details not in result['error'] for result in executor.results)

    case_executions = list(UiTestCaseExecution.objects.filter(test_suite=suite).order_by('test_case_id'))
    assert len(case_executions) == 2
    assert all(item.status == 'failed' and item.error_message == public_error for item in case_executions)
    records = [record for record in caplog.records if record.name == 'apps.ui_automation.test_executor']
    assert len(records) == (2 if browser == 'safari' else 1)
    assert all(record.exc_info and str(record.exc_info[1]) == driver_details for record in records)
    assert driver_details in caplog.text


@pytest.mark.django_db
@pytest.mark.parametrize('browser', ['chrome', 'safari'])
def test_suite_selenium_startup_cancellation_is_not_recorded_as_browser_failure(monkeypatch, browser):
    user = User.objects.create_user(username=f'selenium-cancel-{browser}')
    project = UiProject.objects.create(
        name=f'{browser} cancel project',
        base_url='https://example.test',
        owner=user,
    )
    suite = UiTestSuite.objects.create(project=project, name=f'{browser} cancel suite')
    case = UiTestCase.objects.create(project=project, name=f'{browser} cancel case', created_by=user)
    UiTestSuiteTestCase.objects.create(test_suite=suite, test_case=case)
    execution = UiTestExecution.objects.create(
        project=project, test_suite=suite, status='RUNNING', engine='selenium', browser=browser,
    )
    checks = {'count': 0}

    def cancel_during_startup():
        checks['count'] += 1
        if browser == 'chrome' or checks['count'] == 2:
            raise ScheduleClaimLost('suite claim lost')

    executor = Executor(
        suite, engine='selenium', browser=browser, executed_by=user, cancel_check=cancel_during_startup,
        execution=execution,
    )
    executor.test_cases = [case]
    monkeypatch.setattr(
        executor,
        'create_selenium_driver',
        lambda: pytest.fail('driver creation must not follow a lost suite claim'),
    )

    with pytest.raises(ScheduleClaimLost, match='suite claim lost'):
        executor.run_with_selenium()

    case_execution = UiTestCaseExecution.objects.get(test_suite=suite, test_case=case)
    assert case_execution.status == ('running' if browser == 'safari' else 'pending')
    assert not case_execution.error_message
    execution.refresh_from_db()
    assert execution.status == 'RUNNING'
    assert executor.results == []
