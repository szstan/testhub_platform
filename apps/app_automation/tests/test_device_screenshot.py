import base64
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.app_automation.models import AppDevice, AppTestConfig
from apps.app_automation.views.device_views import get_adb_path


@pytest.mark.django_db
@pytest.mark.parametrize('configured', [None, '', 'adb', 'adb.exe'])
def test_adb_path_uses_environment_when_database_has_no_explicit_path(monkeypatch, configured):
    monkeypatch.setenv('APP_ADB_PATH', '/opt/android/platform-tools/adb')
    if configured is not None:
        AppTestConfig.objects.create(adb_path=configured)

    assert get_adb_path() == '/opt/android/platform-tools/adb'


@pytest.mark.django_db
def test_adb_path_preserves_explicit_database_configuration(monkeypatch):
    monkeypatch.setenv('APP_ADB_PATH', '/opt/default/adb')
    AppTestConfig.objects.create(adb_path='/opt/custom/adb')

    assert get_adb_path() == '/opt/custom/adb'


@pytest.mark.django_db
def test_adb_path_falls_back_to_environment_if_configuration_lookup_fails(monkeypatch):
    monkeypatch.setenv('APP_ADB_PATH', '/opt/default/adb')
    with patch.object(AppTestConfig.objects, 'first', side_effect=RuntimeError('db unavailable')):
        assert get_adb_path() == '/opt/default/adb'


@pytest.fixture
def screenshot_client(db):
    user = get_user_model().objects.create_user(username='screenshot-admin', is_staff=True)
    device = AppDevice.objects.create(device_id='screenshot-device', status='online')
    client = APIClient()
    client.force_authenticate(user=user)
    return client, device


def test_screenshot_uses_configured_environment_adb_and_preserves_response(screenshot_client, monkeypatch):
    client, device = screenshot_client
    monkeypatch.setenv('APP_ADB_PATH', '/opt/android/adb')
    AppTestConfig.objects.create(adb_path='adb')
    image = b'\x89PNG\r\n\x1a\nimage-fixture'
    with patch('apps.app_automation.views.device_views.subprocess.run', return_value=SimpleNamespace(stdout=image)) as run:
        response = client.post(f'/api/app-automation/devices/{device.pk}/screenshot/')

    assert response.status_code == 200
    assert run.call_args.args[0] == ['/opt/android/adb', '-s', device.device_id, 'exec-out', 'screencap', '-p']
    assert response.data['data']['content'] == 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')


@pytest.mark.parametrize(('stderr', 'expected'), [
    (b'error: device offline', '设备已离线'),
    (b'error: device unauthorized', '设备未授权'),
    (b"adb: error: device 'serial' not found", 'ADB 未找到该设备'),
    (b'error: no devices/emulators found', 'ADB 未找到该设备'),
    (b'adb: cannot connect to daemon', '无法连接 ADB 服务'),
    (b'unexpected error', 'ADB 截图命令失败'),
])
def test_screenshot_returns_actionable_safe_message_for_adb_errors(screenshot_client, stderr, expected):
    client, device = screenshot_client
    error = subprocess.CalledProcessError(1, ['adb'], stderr=stderr)
    with patch('apps.app_automation.views.device_views.subprocess.run', side_effect=error):
        response = client.post(f'/api/app-automation/devices/{device.pk}/screenshot/')

    assert response.status_code == 500
    assert response.data['success'] is False
    assert expected in response.data['msg']


def test_screenshot_redacts_adb_diagnostics_in_logs_and_response(screenshot_client, caplog):
    client, device = screenshot_client
    error = subprocess.CalledProcessError(1, ['adb'], stderr=b'error: device offline password=fixture-secret')
    with patch('apps.app_automation.views.device_views.subprocess.run', side_effect=error):
        response = client.post(f'/api/app-automation/devices/{device.pk}/screenshot/')

    assert response.status_code == 500
    assert 'fixture-secret' not in str(response.data)
    assert 'fixture-secret' not in caplog.text
    assert '[REDACTED]' in caplog.text


@pytest.mark.parametrize(('error', 'expected'), [
    (FileNotFoundError('private adb path'), '找不到 ADB'),
    (PermissionError('private adb path'), 'ADB 无执行权限'),
    (subprocess.TimeoutExpired(['adb'], 10), '截图超时'),
])
def test_screenshot_reports_missing_adb_permissions_and_timeout(screenshot_client, error, expected):
    client, device = screenshot_client
    with patch('apps.app_automation.views.device_views.subprocess.run', side_effect=error):
        response = client.post(f'/api/app-automation/devices/{device.pk}/screenshot/')

    assert response.status_code == 500
    assert expected in response.data['msg']
    assert 'private adb path' not in response.data['msg']


def test_screenshot_reports_empty_output(screenshot_client):
    client, device = screenshot_client
    with patch('apps.app_automation.views.device_views.subprocess.run', return_value=SimpleNamespace(stdout=b'')):
        response = client.post(f'/api/app-automation/devices/{device.pk}/screenshot/')

    assert response.status_code == 500
    assert response.data['msg'] == '截图失败：无返回数据'
