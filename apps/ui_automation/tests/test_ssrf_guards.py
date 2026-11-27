import asyncio
import ipaddress
import socket
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from decouple import config
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.utils import timezone
from rest_framework import serializers

from apps.core.network import UnsafeOutboundURL
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
from apps.ui_automation.network_policy import (
    configure_selenium_network_guard,
    configure_selenium_options,
    create_ui_guarded_selenium_driver,
    get_ui_network_policy,
    playwright_launch_network_options,
    validate_ui_outbound_url,
)
from apps.ui_automation.serializers import UiProjectCreateSerializer, UiProjectUpdateSerializer


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


class _SyncRoute:
    def __init__(self, url):
        self.request = SimpleNamespace(url=url)
        self.aborted = False
        self.continued = False

    def abort(self):
        self.aborted = True

    def continue_(self):
        self.continued = True


class _AsyncRoute(_SyncRoute):
    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


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


def test_ui_private_target_settings_parse_scoped_configuration():
    from backend import settings as base_settings

    assert base_settings.UI_ALLOW_PRIVATE_TARGETS == config(
        'UI_ALLOW_PRIVATE_TARGETS', default=False, cast=bool,
    )
    assert django_settings.PERF_ALLOW_PRIVATE_TARGETS is True
    assert django_settings.UI_ALLOW_PRIVATE_TARGETS == config(
        'UI_ALLOW_PRIVATE_TARGETS', default=False, cast=bool,
    )
    assert django_settings.UI_ALLOWED_PRIVATE_CIDRS == config(
        'UI_ALLOWED_PRIVATE_CIDRS', default='', cast=lambda value: [
            item.strip() for item in value.split(',') if item.strip()
        ],
    )


@pytest.mark.django_db
@pytest.mark.parametrize('serializer_type', ['create', 'update'])
@pytest.mark.parametrize(
    ('ui_allow_private', 'cidrs', 'expected_valid'),
    [
        (True, [], False),
        (False, ['192.168.1.111/32'], False),
        (True, ['192.168.1.111/32'], True),
    ],
)
def test_ui_project_serializers_require_enabled_scoped_private_target(
    serializer_type, ui_allow_private, cidrs, expected_valid,
):
    user = get_user_model().objects.create_user(
        username=f'private-target-{serializer_type}-{ui_allow_private}-{len(cidrs)}'
    )
    private_url = 'http://192.168.1.111/admin/'
    if serializer_type == 'create':
        serializer = UiProjectCreateSerializer(data={
            'name': 'Private target project',
            'base_url': private_url,
            'owner': user.pk,
        })
    else:
        project = UiProject(name='Existing project', base_url='https://public.example.test', owner=user)
        serializer = UiProjectUpdateSerializer(
            instance=project, data={'base_url': private_url}, partial=True,
        )

    with override_settings(
        UI_ALLOW_PRIVATE_TARGETS=ui_allow_private,
        UI_ALLOWED_PRIVATE_CIDRS=cidrs,
        PERF_ALLOW_PRIVATE_TARGETS=True,
    ):
        is_valid = serializer.is_valid()

    assert is_valid is expected_valid, serializer.errors
    if expected_valid:
        assert serializer.validated_data['base_url'] == private_url
    else:
        assert '受限网络' in str(serializer.errors)


def test_ui_private_target_flag_alone_does_not_inherit_perf_exemption():
    with override_settings(
        UI_ALLOW_PRIVATE_TARGETS=True,
        UI_ALLOWED_PRIVATE_CIDRS=[],
        PERF_ALLOW_PRIVATE_TARGETS=True,
    ):
        policy = get_ui_network_policy()

    assert policy == {'allow_private': False, 'allowed_private_cidrs': ()}
    with pytest.raises(UnsafeOutboundURL, match='受限网络'):
        validate_ui_outbound_url('http://192.168.1.111/admin')


def test_ui_policy_rejects_invalid_or_broad_cidrs():
    for cidr in ('0.0.0.0/0', '10.0.0.0/7', '127.0.0.0/8', '169.254.0.0/16', '8.8.8.0/24'):
        with override_settings(
            UI_ALLOW_PRIVATE_TARGETS=True,
            UI_ALLOWED_PRIVATE_CIDRS=[cidr],
        ), pytest.raises(ImproperlyConfigured, match='UI_ALLOWED_PRIVATE_CIDRS'):
            get_ui_network_policy()


def test_ui_policy_blocks_loopback_and_metadata_even_when_private_cidr_is_allowed():
    with override_settings(
        UI_ALLOW_PRIVATE_TARGETS=True,
        UI_ALLOWED_PRIVATE_CIDRS=['10.0.0.0/8'],
    ):
        for url in (
            'http://127.0.0.1/admin',
            'http://169.254.169.254/latest/meta-data',
        ):
            with pytest.raises(UnsafeOutboundURL) as error:
                validate_ui_outbound_url(url)
            assert '受限网络' in str(error.value)


def test_ui_policy_checks_every_dns_answer(monkeypatch):
    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.112', port)),
        ],
    )
    with override_settings(
        UI_ALLOW_PRIVATE_TARGETS=True,
        UI_ALLOWED_PRIVATE_CIDRS=['192.168.1.111/32'],
    ), pytest.raises(UnsafeOutboundURL, match='受限网络'):
        validate_ui_outbound_url('https://mixed.example.test')


def test_ui_project_serializer_allows_public_base_url(monkeypatch):
    _public_dns(monkeypatch)
    serializer = UiProjectCreateSerializer()
    with override_settings(
        UI_ALLOW_PRIVATE_TARGETS=False,
        UI_ALLOWED_PRIVATE_CIDRS=[],
        PERF_ALLOW_PRIVATE_TARGETS=True,
    ):
        assert serializer.validate_base_url('https://public.example.test') == 'https://public.example.test'


def test_playwright_route_guard_blocks_redirects_subresources_and_websockets():
    from apps.core.network import guard_playwright_route_sync

    with override_settings(
        UI_ALLOW_PRIVATE_TARGETS=True,
        UI_ALLOWED_PRIVATE_CIDRS=['192.168.1.111/32'],
    ):
        policy = get_ui_network_policy()
        for url in (
            'http://192.168.1.112/admin',
            'https://10.0.0.1/asset.js',
            'ws://192.168.1.112/socket',
        ):
            route = _SyncRoute(url)
            guard_playwright_route_sync(route, **policy)
            assert route.aborted is True
            assert route.continued is False


@pytest.mark.asyncio
async def test_playwright_route_guard_allows_whitelisted_target_and_aborts_unlisted_target():
    from apps.core.network import guard_playwright_route

    with override_settings(
        UI_ALLOW_PRIVATE_TARGETS=True,
        UI_ALLOWED_PRIVATE_CIDRS=['192.168.1.111/32'],
    ):
        policy = get_ui_network_policy()
        allowed = _AsyncRoute('http://192.168.1.111/admin')
        rejected = _AsyncRoute('http://192.168.1.112/admin')
        await guard_playwright_route(allowed, **policy)
        await guard_playwright_route(rejected, **policy)

    assert allowed.continued is True
    assert rejected.aborted is True


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
    engine.network_proxy = object()
    with override_settings(
        UI_ALLOW_PRIVATE_TARGETS=False,
        UI_ALLOWED_PRIVATE_CIDRS=[],
        PERF_ALLOW_PRIVATE_TARGETS=True,
    ):
        success, _message = await engine.navigate('https://public.example.test/home')
    assert success is True
    assert engine.page.urls == ['https://public.example.test/home']


@pytest.mark.asyncio
async def test_playwright_navigation_allows_only_whitelisted_private_target(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr('apps.ui_automation.playwright_engine.asyncio.sleep', no_sleep)
    engine = PlaywrightTestEngine()
    engine.page = _AsyncPage()
    engine.network_proxy = object()
    with override_settings(
        UI_ALLOW_PRIVATE_TARGETS=True,
        UI_ALLOWED_PRIVATE_CIDRS=['192.168.1.111/32'],
        PERF_ALLOW_PRIVATE_TARGETS=True,
    ):
        success, _message = await engine.navigate('http://192.168.1.111/admin/')
    assert success is True
    assert engine.page.urls == ['http://192.168.1.111/admin/']


def test_playwright_network_configuration_is_fail_closed_by_browser():
    options = playwright_launch_network_options('chromium', 'http://127.0.0.1:43000')
    assert options['proxy'] == {'server': 'http://127.0.0.1:43000'}
    assert '--proxy-bypass-list=<-loopback>' in options['args']
    assert '--disable-quic' in options['args']
    assert '--dns-prefetch-disable' in options['args']
    assert '--force-webrtc-ip-handling-policy=disable_non_proxied_udp' in options['args']

    firefox = playwright_launch_network_options('firefox', 'http://127.0.0.1:43000')
    assert firefox['proxy'] == {'server': 'http://127.0.0.1:43000'}
    assert firefox['firefox_user_prefs']['network.proxy.allow_hijacking_localhost'] is True
    assert firefox['firefox_user_prefs']['network.proxy.no_proxies_on'] == ''
    assert firefox['firefox_user_prefs']['network.dns.disablePrefetch'] is True
    assert firefox['firefox_user_prefs']['network.http.http3.enable'] is False
    assert firefox['firefox_user_prefs']['media.peerconnection.enabled'] is False
    assert firefox['firefox_user_prefs']['dom.serviceWorkers.enabled'] is False

    with pytest.raises(RuntimeError, match='受限网络'):
        playwright_launch_network_options('webkit', 'http://127.0.0.1:43000')


def test_selenium_scoped_driver_ignores_legacy_allow_private_argument():
    options = SimpleNamespace(set_preference=Mock(), add_argument=Mock())
    driver = SimpleNamespace()
    with (
        override_settings(
            UI_ALLOW_PRIVATE_TARGETS=True,
            UI_ALLOWED_PRIVATE_CIDRS=['192.168.1.111/32'],
            PERF_ALLOW_PRIVATE_TARGETS=True,
        ),
        patch(
            'apps.ui_automation.network_policy.create_core_guarded_selenium_driver',
            return_value=driver,
        ) as create_driver,
        patch('apps.ui_automation.network_policy.configure_selenium_network_guard'),
    ):
        result = create_ui_guarded_selenium_driver(
            lambda **_kwargs: None,
            options,
            browser_type='chrome',
            allow_private=True,
        )

    assert result is driver
    assert create_driver.call_args.kwargs['allow_private'] is False
    assert create_driver.call_args.kwargs['allowed_private_cidrs'] == (
        ipaddress.ip_network('192.168.1.111/32'),
    )


def test_selenium_browser_configuration_blocks_service_worker_and_firefox_webrtc():
    class ChromeDriver:
        capabilities = {'browserName': 'chrome'}
        _testhub_guarded_proxy = SimpleNamespace(allowed_private_cidrs=())

        def __init__(self):
            self.commands = []

        def execute_cdp_cmd(self, command, params):
            self.commands.append((command, params))

    driver = ChromeDriver()
    with override_settings(UI_ALLOW_PRIVATE_TARGETS=False, UI_ALLOWED_PRIVATE_CIDRS=[]):
        configure_selenium_network_guard(driver)

    assert ('Network.setBypassServiceWorker', {'bypass': True}) in driver.commands

    firefox = SimpleNamespace(set_preference=Mock())
    configure_selenium_options(firefox, 'firefox')
    preferences = {call.args[0]: call.args[1] for call in firefox.set_preference.call_args_list}
    assert preferences['dom.serviceWorkers.enabled'] is False
    assert preferences['network.dns.disablePrefetch'] is True
    assert preferences['network.http.http3.enable'] is False
    assert preferences['media.peerconnection.enabled'] is False
    assert preferences['network.proxy.allow_hijacking_localhost'] is True


@pytest.mark.asyncio
async def test_playwright_start_sets_scoped_proxy_and_blocks_service_workers(monkeypatch):
    class FakeProxy:
        instance = None

        def __init__(self, *, allowed_private_cidrs):
            self.allowed_private_cidrs = allowed_private_cidrs
            self.url = 'http://127.0.0.1:43000'
            self.closed = False
            type(self).instance = self

        def start(self):
            return self

        def close(self):
            self.closed = True

    class FakePage:
        async def close(self):
            pass

    class FakeContext:
        def __init__(self):
            self.options = None
            self.route_args = None

        async def route(self, *args):
            self.route_args = args

        async def new_page(self):
            return FakePage()

        async def close(self):
            pass

    class FakeBrowser:
        def __init__(self):
            self.context = FakeContext()
            self.launch_options = None

        async def new_context(self, **kwargs):
            self.context.options = kwargs
            return self.context

        async def close(self):
            pass

    class Launcher:
        def __init__(self):
            self.browser = FakeBrowser()

        async def launch(self, **kwargs):
            self.browser.launch_options = kwargs
            return self.browser

    class FakePlaywright:
        def __init__(self):
            self.chromium = Launcher()

        async def stop(self):
            pass

    class Manager:
        async def start(self):
            self.playwright = FakePlaywright()
            return self.playwright

    monkeypatch.setattr('apps.ui_automation.playwright_engine.GuardedBrowserProxy', FakeProxy)
    monkeypatch.setattr('apps.ui_automation.playwright_engine.async_playwright', Manager)
    engine = PlaywrightTestEngine()
    with override_settings(
        UI_ALLOW_PRIVATE_TARGETS=True,
        UI_ALLOWED_PRIVATE_CIDRS=['192.168.1.111/32'],
    ):
        await engine.start()

    browser = engine.browser
    assert FakeProxy.instance.allowed_private_cidrs == (ipaddress.ip_network('192.168.1.111/32'),)
    assert browser.launch_options['proxy'] == {'server': FakeProxy.instance.url}
    assert '--proxy-bypass-list=<-loopback>' in browser.launch_options['args']
    assert '--disable-quic' in browser.launch_options['args']
    assert '--force-webrtc-ip-handling-policy=disable_non_proxied_udp' in browser.launch_options['args']
    assert browser.context.options['service_workers'] == 'block'
    await engine.stop()
    assert FakeProxy.instance.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [RuntimeError, asyncio.CancelledError])
async def test_playwright_start_closes_proxy_after_launch_failure_or_cancellation(monkeypatch, error_type):
    class FakeProxy:
        instance = None

        def __init__(self, *, allowed_private_cidrs):
            self.allowed_private_cidrs = allowed_private_cidrs
            self.url = 'http://127.0.0.1:43000'
            self.closed = False
            type(self).instance = self

        def start(self):
            return self

        def close(self):
            self.closed = True

    class Launcher:
        async def launch(self, **_kwargs):
            raise error_type('browser launch failed')

    class FakePlaywright:
        chromium = Launcher()

        async def stop(self):
            self.stopped = True

    class Manager:
        async def start(self):
            self.playwright = FakePlaywright()
            return self.playwright

    monkeypatch.setattr('apps.ui_automation.playwright_engine.GuardedBrowserProxy', FakeProxy)
    monkeypatch.setattr('apps.ui_automation.playwright_engine.async_playwright', Manager)
    engine = PlaywrightTestEngine()

    with pytest.raises(error_type, match='browser launch failed'):
        await engine.start()

    assert FakeProxy.instance.closed is True
    assert engine.network_proxy is None
    assert engine.playwright is None


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
        'apps.ui_automation.test_executor.validate_ui_outbound_url',
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
