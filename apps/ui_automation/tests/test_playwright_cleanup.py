import asyncio

import pytest

from apps.ui_automation import playwright_engine
from apps.ui_automation.playwright_engine import PlaywrightTestEngine
from apps.ui_automation.selenium_engine import SeleniumTestEngine


class _CloseSpy:
    def __init__(self, error=None):
        self.error = error
        self.closed = False

    async def close(self):
        self.closed = True
        if self.error is not None:
            raise self.error

    async def stop(self):
        await self.close()


class _ProxySpy:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_stop_propagates_cancellation_after_closing_every_resource(monkeypatch):
    started = asyncio.Event()
    never = asyncio.Event()
    page_cancelled = asyncio.Event()

    class BlockingPage:
        async def close(self):
            started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                page_cancelled.set()
                raise

    page = BlockingPage()
    context = _CloseSpy()
    browser = _CloseSpy()
    playwright = _CloseSpy()
    proxy = _ProxySpy()
    engine = PlaywrightTestEngine()
    engine.page = page
    engine.context = context
    engine.browser = browser
    engine.playwright = playwright
    engine.network_proxy = proxy

    task = asyncio.create_task(engine.stop())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert page_cancelled.is_set()
    assert context.closed and browser.closed and playwright.closed and proxy.closed
    assert engine.page is None
    assert engine.context is None
    assert engine.browser is None
    assert engine.playwright is None
    assert engine.network_proxy is None


@pytest.mark.asyncio
async def test_stop_bounds_a_hung_close_and_continues_cleanup(monkeypatch):
    started = asyncio.Event()
    never = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingPage:
        async def close(self):
            started.set()
            try:
                await never.wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(playwright_engine, "_ASYNC_RESOURCE_CLOSE_TIMEOUT", 0.01)
    page = BlockingPage()
    context = _CloseSpy()
    browser = _CloseSpy()
    playwright = _CloseSpy()
    proxy = _ProxySpy()
    engine = PlaywrightTestEngine()
    engine.page = page
    engine.context = context
    engine.browser = browser
    engine.playwright = playwright
    engine.network_proxy = proxy

    await asyncio.wait_for(engine.stop(), timeout=1)

    assert started.is_set() and cancelled.is_set()
    assert context.closed and browser.closed and playwright.closed and proxy.closed
    assert engine.page is None
    assert engine.context is None
    assert engine.browser is None
    assert engine.playwright is None
    assert engine.network_proxy is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
async def test_stop_propagates_process_signals_after_closing_every_resource(error_type):
    page = _CloseSpy(error_type("close interrupted"))
    context = _CloseSpy()
    browser = _CloseSpy()
    playwright = _CloseSpy()
    proxy = _ProxySpy()
    engine = PlaywrightTestEngine()
    engine.page = page
    engine.context = context
    engine.browser = browser
    engine.playwright = playwright
    engine.network_proxy = proxy

    with pytest.raises(error_type, match="close interrupted"):
        await engine.stop()

    assert page.closed and context.closed and browser.closed and playwright.closed and proxy.closed
    assert engine.page is None
    assert engine.context is None
    assert engine.browser is None
    assert engine.playwright is None
    assert engine.network_proxy is None


def _install_environment_mocks(
    monkeypatch,
    *,
    context_close=None,
    launch_error=None,
    playwright_stop_error=None,
    playwright_stop=None,
):
    class FakeProxy:
        instance = None

        def __init__(self, *, allowed_private_cidrs):
            self.allowed_private_cidrs = allowed_private_cidrs
            self.url = "http://127.0.0.1:43000"
            self.closed = False
            type(self).instance = self

        def start(self):
            return self

        def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True
            if context_close is not None:
                await context_close()

    class FakeBrowser:
        def __init__(self):
            self.context = FakeContext()
            self.closed = False

        async def new_context(self, **_kwargs):
            return self.context

        async def close(self):
            self.closed = True

    class Launcher:
        def __init__(self):
            self.browser = FakeBrowser()

        async def launch(self, **_kwargs):
            if launch_error is not None:
                raise launch_error
            return self.browser

    class FakePlaywright:
        def __init__(self):
            self.chromium = Launcher()
            self.stopped = False

        async def stop(self):
            self.stopped = True
            if playwright_stop is not None:
                await playwright_stop()
            if playwright_stop_error is not None:
                raise playwright_stop_error

    class Manager:
        def __init__(self):
            self.playwright = FakePlaywright()

        async def start(self):
            return self.playwright

    manager = Manager()
    monkeypatch.setattr(playwright_engine, "async_playwright", lambda: manager)
    monkeypatch.setattr(playwright_engine, "GuardedBrowserProxy", FakeProxy)
    monkeypatch.setattr(
        playwright_engine,
        "get_ui_network_policy",
        lambda: {"allowed_private_cidrs": ()},
    )
    monkeypatch.setattr(
        playwright_engine,
        "playwright_launch_network_options",
        lambda *_args: {},
    )
    return manager.playwright, FakeProxy


@pytest.mark.asyncio
async def test_environment_check_propagates_cleanup_cancellation_and_closes_rest(monkeypatch):
    close_started = asyncio.Event()
    never = asyncio.Event()
    close_cancelled = asyncio.Event()

    async def block_context_close():
        close_started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            close_cancelled.set()
            raise

    fake_playwright, proxy_type = _install_environment_mocks(
        monkeypatch,
        context_close=block_context_close,
    )
    task = asyncio.create_task(PlaywrightTestEngine.check_execution_environment())
    await asyncio.wait_for(close_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert close_cancelled.is_set()
    assert fake_playwright.chromium.browser.context.closed
    assert fake_playwright.chromium.browser.closed
    assert fake_playwright.stopped
    assert proxy_type.instance.closed


@pytest.mark.asyncio
async def test_environment_check_preserves_original_cancellation_over_cleanup_signal(monkeypatch):
    original_cancel = asyncio.CancelledError("launch cancelled")
    fake_playwright, proxy_type = _install_environment_mocks(
        monkeypatch,
        launch_error=original_cancel,
        playwright_stop_error=SystemExit("cleanup signal"),
    )

    with pytest.raises(asyncio.CancelledError, match="launch cancelled"):
        await PlaywrightTestEngine.check_execution_environment()

    assert fake_playwright.stopped
    assert proxy_type.instance.closed


@pytest.mark.asyncio
async def test_environment_check_preserves_launch_failure_over_cleanup_signal(monkeypatch):
    fake_playwright, proxy_type = _install_environment_mocks(
        monkeypatch,
        launch_error=RuntimeError("launch failed"),
        playwright_stop_error=SystemExit("cleanup signal"),
    )

    success, message = await PlaywrightTestEngine.check_execution_environment()

    assert success is False
    assert "launch failed" in message
    assert "cleanup signal" not in message
    assert fake_playwright.stopped
    assert proxy_type.instance.closed


@pytest.mark.asyncio
async def test_environment_check_cleanup_cancellation_overrides_launch_failure(monkeypatch):
    cleanup_started = asyncio.Event()
    never = asyncio.Event()
    cleanup_cancelled = asyncio.Event()

    async def block_playwright_stop():
        cleanup_started.set()
        try:
            await never.wait()
        finally:
            cleanup_cancelled.set()

    fake_playwright, proxy_type = _install_environment_mocks(
        monkeypatch,
        launch_error=RuntimeError("launch failed"),
        playwright_stop=block_playwright_stop,
    )
    task = asyncio.create_task(PlaywrightTestEngine.check_execution_environment())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert cleanup_cancelled.is_set()
    assert fake_playwright.stopped
    assert proxy_type.instance.closed


@pytest.mark.asyncio
async def test_start_cleanup_cancellation_overrides_launch_failure(monkeypatch):
    cleanup_started = asyncio.Event()
    never = asyncio.Event()
    cleanup_cancelled = asyncio.Event()

    async def block_playwright_stop():
        cleanup_started.set()
        try:
            await never.wait()
        finally:
            cleanup_cancelled.set()

    fake_playwright, proxy_type = _install_environment_mocks(
        monkeypatch,
        launch_error=RuntimeError("launch failed"),
        playwright_stop=block_playwright_stop,
    )
    engine = PlaywrightTestEngine()
    task = asyncio.create_task(engine.start())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert cleanup_cancelled.is_set()
    assert fake_playwright.stopped
    assert proxy_type.instance.closed
    assert engine.page is None
    assert engine.context is None
    assert engine.browser is None
    assert engine.playwright is None
    assert engine.network_proxy is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
async def test_start_preserves_process_signal_over_cleanup_cancellation(monkeypatch, error_type):
    fake_playwright, proxy_type = _install_environment_mocks(
        monkeypatch,
        launch_error=error_type("launch interrupted"),
        playwright_stop_error=asyncio.CancelledError("cleanup cancelled"),
    )
    engine = PlaywrightTestEngine()

    with pytest.raises(error_type, match="launch interrupted"):
        await engine.start()

    assert fake_playwright.stopped
    assert proxy_type.instance.closed
    assert engine.playwright is None
    assert engine.network_proxy is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
async def test_start_preserves_original_process_signal_over_cleanup_cancellation(monkeypatch, error_type):
    cleanup_started = asyncio.Event()
    never = asyncio.Event()
    cleanup_cancelled = asyncio.Event()

    async def block_playwright_stop():
        cleanup_started.set()
        try:
            await never.wait()
        finally:
            cleanup_cancelled.set()

    fake_playwright, proxy_type = _install_environment_mocks(
        monkeypatch,
        launch_error=error_type("startup interrupted"),
        playwright_stop=block_playwright_stop,
    )
    startup_task = asyncio.current_task()

    async def cancel_during_cleanup():
        await cleanup_started.wait()
        startup_task.cancel()

    cancellation_task = asyncio.create_task(cancel_during_cleanup())
    engine = PlaywrightTestEngine()

    with pytest.raises(error_type, match="startup interrupted"):
        await engine.start()

    await asyncio.wait_for(cleanup_cancelled.wait(), timeout=1)
    await asyncio.wait_for(cancellation_task, timeout=1)
    assert fake_playwright.stopped
    assert proxy_type.instance.closed
    assert engine.page is None
    assert engine.context is None
    assert engine.browser is None
    assert engine.playwright is None
    assert engine.network_proxy is None


def _install_sync_environment_mocks(
    monkeypatch,
    *,
    context_close_error=None,
    launch_error=None,
):
    from playwright import sync_api

    class FakeProxy:
        instance = None

        def __init__(self, *, allowed_private_cidrs):
            self.allowed_private_cidrs = allowed_private_cidrs
            self.url = "http://127.0.0.1:43000"
            self.closed = False
            type(self).instance = self

        def start(self):
            return self

        def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True
            if context_close_error is not None:
                raise context_close_error

    class FakeBrowser:
        def __init__(self):
            self.context = FakeContext()
            self.closed = False

        def new_context(self, **_kwargs):
            return self.context

        def close(self):
            self.closed = True

    class Launcher:
        def __init__(self):
            self.browser = FakeBrowser()

        def launch(self, **_kwargs):
            if launch_error is not None:
                raise launch_error
            return self.browser

    class FakePlaywright:
        def __init__(self):
            self.chromium = Launcher()
            self.stopped = False

        def stop(self):
            self.stopped = True

    class Manager:
        def __init__(self):
            self.playwright = FakePlaywright()

        def start(self):
            return self.playwright

    manager = Manager()
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: manager)
    monkeypatch.setattr(playwright_engine, "GuardedBrowserProxy", FakeProxy)
    monkeypatch.setattr(
        playwright_engine,
        "get_ui_network_policy",
        lambda: {"allowed_private_cidrs": ()},
    )
    monkeypatch.setattr(
        playwright_engine,
        "playwright_launch_network_options",
        lambda *_args: {},
    )
    return manager.playwright, FakeProxy


def test_sync_environment_check_reports_context_close_failure_and_closes_rest(monkeypatch):
    fake_playwright, proxy_type = _install_sync_environment_mocks(
        monkeypatch,
        context_close_error=RuntimeError("context close failed"),
    )

    success, message = PlaywrightTestEngine.check_execution_environment_sync()

    assert success is False
    assert "context close failed" in message
    assert fake_playwright.chromium.browser.context.closed
    assert fake_playwright.chromium.browser.closed
    assert fake_playwright.stopped
    assert proxy_type.instance.closed


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_sync_environment_check_propagates_control_signal_after_cleanup(monkeypatch, error_type):
    fake_playwright, proxy_type = _install_sync_environment_mocks(
        monkeypatch,
        context_close_error=error_type("cleanup interrupted"),
    )

    with pytest.raises(error_type, match="cleanup interrupted"):
        PlaywrightTestEngine.check_execution_environment_sync()

    assert fake_playwright.chromium.browser.context.closed
    assert fake_playwright.chromium.browser.closed
    assert fake_playwright.stopped
    assert proxy_type.instance.closed


def test_sync_environment_check_keeps_successful_result_and_closes_resources(monkeypatch):
    fake_playwright, proxy_type = _install_sync_environment_mocks(monkeypatch)

    assert PlaywrightTestEngine.check_execution_environment_sync() == (True, None)
    assert fake_playwright.chromium.browser.context.closed
    assert fake_playwright.chromium.browser.closed
    assert fake_playwright.stopped
    assert proxy_type.instance.closed


def test_sync_environment_check_preserves_launch_failure(monkeypatch):
    fake_playwright, proxy_type = _install_sync_environment_mocks(
        monkeypatch,
        launch_error=RuntimeError("launch failed"),
    )

    success, message = PlaywrightTestEngine.check_execution_environment_sync()

    assert success is False
    assert "launch failed" in message
    assert fake_playwright.stopped
    assert proxy_type.instance.closed


def test_playwright_safari_sync_preflight_reports_network_restriction():
    success, message = PlaywrightTestEngine.check_execution_environment_sync("safari")

    assert success is False
    assert "Safari" in message
    assert "代理" in message
    assert "playwright install" not in message.lower()


@pytest.mark.asyncio
async def test_playwright_safari_async_preflight_reports_network_restriction():
    success, message = await PlaywrightTestEngine.check_execution_environment("webkit")

    assert success is False
    assert "WebKit/Safari" in message
    assert "代理" in message
    assert "playwright install" not in message.lower()


def test_selenium_safari_preflight_rejects_before_browser_and_driver_checks(monkeypatch):
    calls = []
    monkeypatch.setattr(
        SeleniumTestEngine,
        "check_browser_available",
        staticmethod(lambda _browser: calls.append("browser") or (True, None)),
    )
    monkeypatch.setattr(
        SeleniumTestEngine,
        "check_webdriver_available",
        staticmethod(lambda _browser: calls.append("driver") or (True, None)),
    )

    success, message = SeleniumTestEngine.check_execution_environment("safari")

    assert success is False
    assert "Safari" in message
    assert "代理" in message
    assert calls == []
