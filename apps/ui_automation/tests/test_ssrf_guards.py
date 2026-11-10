import socket
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework import serializers

from apps.ui_automation.models import (
    TestCase as UiTestCase,
    TestCaseExecution as UiTestCaseExecution,
    TestExecution as UiTestExecution,
    TestSuite as UiTestSuite,
    TestSuiteTestCase as UiTestSuiteTestCase,
    UiProject,
)

from apps.ui_automation.playwright_engine import PlaywrightTestEngine
from apps.ui_automation.selenium_engine import SeleniumTestEngine
from apps.ui_automation.serializers import UiProjectCreateSerializer


class _AsyncPage:
    def __init__(self):
        self.urls = []

    async def goto(self, url, **kwargs):
        self.urls.append(url)


class _Driver:
    def __init__(self, current_url=None):
        self.urls = []
        self.current_url = current_url

    def get(self, url):
        self.urls.append(url)
        if self.current_url is None:
            self.current_url = url


def _public_dns(monkeypatch):
    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port)),
        ],
    )

def test_ui_project_serializer_rejects_private_base_url():
    serializer = UiProjectCreateSerializer()
    with override_settings(UI_ALLOW_PRIVATE_TARGETS=False, PERF_ALLOW_PRIVATE_TARGETS=False), pytest.raises(serializers.ValidationError):
        serializer.validate_base_url('http://127.0.0.1:8000')


def test_ui_project_serializer_allows_public_base_url(monkeypatch):
    _public_dns(monkeypatch)
    serializer = UiProjectCreateSerializer()
    with override_settings(UI_ALLOW_PRIVATE_TARGETS=False, PERF_ALLOW_PRIVATE_TARGETS=False):
        assert serializer.validate_base_url('https://public.example.test') == 'https://public.example.test'


@pytest.mark.asyncio
async def test_playwright_navigation_rejects_private_target():
    engine = PlaywrightTestEngine()
    engine.page = _AsyncPage()
    with override_settings(UI_ALLOW_PRIVATE_TARGETS=False, PERF_ALLOW_PRIVATE_TARGETS=False):
        success, message = await engine.navigate('http://127.0.0.1:8000/admin')
    assert success is False
    assert '受限网络' in message
    assert engine.page.urls == []


@pytest.mark.asyncio
async def test_playwright_navigation_allows_public_target(monkeypatch):
    _public_dns(monkeypatch)
    engine = PlaywrightTestEngine()
    engine.page = _AsyncPage()
    with override_settings(UI_ALLOW_PRIVATE_TARGETS=False, PERF_ALLOW_PRIVATE_TARGETS=False):
        success, _message = await engine.navigate('https://public.example.test/home')
    assert success is True
    assert engine.page.urls == ['https://public.example.test/home']


def test_selenium_navigation_rejects_private_target():
    engine = SeleniumTestEngine()
    engine.driver = _Driver()
    with override_settings(UI_ALLOW_PRIVATE_TARGETS=False, PERF_ALLOW_PRIVATE_TARGETS=False):
        success, message = engine.navigate('http://169.254.169.254/latest/meta-data')
    assert success is False
    assert '受限网络' in message
    assert engine.driver.urls == []



def test_selenium_navigation_fails_closed_without_redirect_guard(monkeypatch):
    _public_dns(monkeypatch)
    engine = SeleniumTestEngine()
    engine.driver = _Driver()
    with override_settings(UI_ALLOW_PRIVATE_TARGETS=False, PERF_ALLOW_PRIVATE_TARGETS=False):
        success, message = engine.navigate('https://public.example.test/start')
    assert success is False
    assert 'network guard' in message
    assert engine.driver.urls == []


@pytest.mark.django_db
def test_ui_suite_cancellation_settles_running_and_pending_rows_and_parent():
    from apps.ui_automation.test_executor import TestExecutor
    from apps.core.scheduler import ScheduleClaimLost

    user = get_user_model().objects.create_user(username='ui-cancel-owner')
    project = UiProject.objects.create(name='cancel project', base_url='https://ui.example.test', owner=user)
    suite = UiTestSuite.objects.create(project=project, name='cancel suite', execution_status='running')
    cases = [
        UiTestCase.objects.create(project=project, name=f'case-{index}', created_by=user)
        for index in range(2)
    ]
    executions = [
        UiTestCaseExecution.objects.create(
            test_case=case,
            project=project,
            test_suite=suite,
            execution_source='suite',
            status='running' if index == 0 else 'pending',
            started_at=timezone.now() if index == 0 else None,
            created_by=user,
        )
        for index, case in enumerate(cases)
    ]
    parent = UiTestExecution.objects.create(
        project=project, test_suite=suite, status='RUNNING', executed_by=user,
    )
    lost = {'value': False}

    def cancel_check():
        if lost['value']:
            raise ScheduleClaimLost('suite claim lost')

    executor = TestExecutor(suite, engine='selenium', cancel_check=cancel_check)
    executor.execution = parent
    executor._case_executions = {case.pk: execution for case, execution in zip(cases, executions)}

    def run_suite():
        lost['value'] = True
        executor._check_cancelled()

    with (
        patch('apps.ui_automation.test_executor.connection.close'),
        patch.object(executor, 'create_execution_record', return_value=parent),
        patch.object(executor, 'get_test_cases', return_value=cases),
        patch.object(executor, 'run_with_selenium', side_effect=run_suite),
    ):
        with pytest.raises(ScheduleClaimLost):
            executor.run()

    for execution in executions:
        execution.refresh_from_db()
        assert execution.status == 'error'
        assert execution.finished_at is not None
        assert 'suite claim lost' in execution.error_message
    parent.refresh_from_db()
    suite.refresh_from_db()
    assert parent.status == 'FAILED'
    assert parent.total_cases == 2
    assert parent.failed_cases == 2
    assert suite.execution_status == 'failed'


@pytest.mark.django_db
def test_ui_cancellation_before_parent_record_still_settles_suite():
    from apps.ui_automation.test_executor import TestExecutor
    from apps.core.scheduler import ScheduleClaimLost

    user = get_user_model().objects.create_user(username='ui-cancel-before-parent')
    project = UiProject.objects.create(name='early cancel project', base_url='https://ui.example.test', owner=user)
    suite = UiTestSuite.objects.create(project=project, name='early cancel suite', execution_status='running')
    def cancel():
        raise ScheduleClaimLost('lease lost')

    executor = TestExecutor(suite, cancel_check=cancel)

    with patch('apps.ui_automation.test_executor.connection.close'):
        with pytest.raises(ScheduleClaimLost):
            executor.run()

    suite.refresh_from_db()
    assert suite.execution_status == 'failed'
    assert not UiTestExecution.objects.filter(test_suite=suite).exists()


@pytest.mark.django_db
def test_selenium_base_url_failure_settles_every_case_execution(monkeypatch):
    from apps.ui_automation.test_executor import TestExecutor

    user = get_user_model().objects.create_user(username='ui-navigation-owner')
    project = UiProject.objects.create(name='navigation project', base_url='https://ui.example.test', owner=user)
    suite = UiTestSuite.objects.create(project=project, name='navigation suite')
    cases = [
        UiTestCase.objects.create(project=project, name=f'navigation-case-{index}', created_by=user)
        for index in range(2)
    ]
    for case in cases:
        UiTestSuiteTestCase.objects.create(test_suite=suite, test_case=case)
    parent = UiTestExecution.objects.create(
        project=project, test_suite=suite, status='RUNNING', executed_by=user,
    )

    class BrokenDriver:
        def get(self, _url):
            raise RuntimeError('base navigation blocked')

        def quit(self):
            pass

    executor = TestExecutor(suite, engine='selenium', executed_by=user)
    executor.execution = parent
    executor.test_cases = cases
    monkeypatch.setattr(
        'apps.ui_automation.test_executor.validate_outbound_url',
        lambda url, **_kwargs: url,
    )
    monkeypatch.setattr(executor, 'create_selenium_driver', lambda: BrokenDriver())

    executor.run_with_selenium()

    case_executions = list(UiTestCaseExecution.objects.filter(test_suite=suite))
    assert len(case_executions) == 2
    assert all(item.status == 'failed' and item.finished_at is not None for item in case_executions)
    assert all('base navigation blocked' in item.error_message for item in case_executions)
    parent.refresh_from_db()
    suite.refresh_from_db()
    assert parent.status == 'FAILED'
    assert parent.failed_cases == 2
    assert suite.execution_status == 'failed'
