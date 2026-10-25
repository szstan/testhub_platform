import socket

import pytest
from django.test import override_settings
from rest_framework import serializers

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


def test_selenium_navigation_rejects_private_redirect(monkeypatch):
    _public_dns(monkeypatch)
    engine = SeleniumTestEngine()
    engine.driver = _Driver(current_url='http://127.0.0.1:8080/redirected')
    with override_settings(UI_ALLOW_PRIVATE_TARGETS=False, PERF_ALLOW_PRIVATE_TARGETS=False):
        success, message = engine.navigate('https://public.example.test/start')
    assert success is False
    assert '受限网络' in message
    assert engine.driver.urls == ['https://public.example.test/start']
