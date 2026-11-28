"""Real SDK + loopback HTTP regressions for cancellable Appium wire IO."""
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from types import SimpleNamespace

import pytest

from apps.app_automation import appium_runtime


@contextmanager
def appium_wire_server():
    stopped = threading.Event()
    state = {'drip': False, 'hold_delete': False, 'invalid_delete': False, 'requests': [], 'bytes_sent': 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_args):
            pass

        def respond(self, value, status=200, drip=False):
            raw = json.dumps(value).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                if drip:
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        state['bytes_sent'] += 1
                        if stopped.wait(0.1):
                            break
                else:
                    self.wfile.write(raw)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            state['requests'].append(('POST', self.path, json.loads(raw)))
            self.respond({'value': {
                'sessionId': 'wire-session', 'capabilities': {'platformName': 'Android'},
            }}, drip=state['drip'])

        def do_DELETE(self):
            state['requests'].append(('DELETE', self.path, None))
            if state['hold_delete']:
                stopped.wait(3)
                return
            if state['invalid_delete']:
                self.respond({'value': {'error': 'invalid session id', 'message': 'already absent', 'stacktrace': ''}}, status=404)
            else:
                self.respond({'value': None})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def real_client():
    pytest.importorskip('appium.webdriver.client_config')
    return appium_runtime._load_client()


def test_real_appium_client_cancels_slow_body_before_case_deadline_is_lost(settings):
    real_client()
    with appium_wire_server() as (endpoint, state):
        settings.APP_APPIUM_ENDPOINTS = {'1': endpoint}
        state['drip'] = True
        started = time.monotonic()
        with pytest.raises(appium_runtime.AppiumOutcomeUnknown):
            appium_runtime.create_driver(
                SimpleNamespace(pk=1, id=1, device_id='wire-serial'),
                'org.example.wire', system_port=8200, deadline=started + 1.0,
            )
        assert time.monotonic() - started < 3.0
        assert len(state['requests']) == 1
        assert state['bytes_sent'] > 0


def test_real_wire_session_and_typed_already_closed_response_are_preserved(settings):
    real_client()
    with appium_wire_server() as (endpoint, state):
        settings.APP_APPIUM_ENDPOINTS = {'1': endpoint}
        device = SimpleNamespace(pk=1, id=1, device_id='wire-serial')
        driver = appium_runtime.create_driver(
            device, 'org.example.wire', system_port=8200, deadline=time.monotonic() + 3,
        )
        assert driver.session_id == 'wire-session'
        assert state['requests'][0][1] == '/session'
        assert state['requests'][0][2]['capabilities']['alwaysMatch']['appium:appPackage'] == 'org.example.wire'
        appium_runtime.quit_driver(driver)
        assert [row[:2] for row in state['requests']] == [
            ('POST', '/session'), ('DELETE', '/session/wire-session'),
        ]
        # Re-attaching must not create a second session. Preserve the typed
        # trusted InvalidSessionIdException proof used by recovery fencing.
        attached = appium_runtime.attach_driver(SimpleNamespace(
            appium_endpoint=endpoint, remote_session_id='wire-session', device=device,
            package=SimpleNamespace(package_name='org.example.wire'), system_port=8200,
            capabilities={'platformName': 'Android'},
        ))
        state['invalid_delete'] = True
        appium_runtime.quit_driver(attached, expected_endpoint=endpoint, expected_session_id='wire-session')
        assert sum(row[0] == 'POST' for row in state['requests']) == 1


def test_attached_delete_honors_its_absolute_cleanup_deadline(settings, monkeypatch):
    real_client()
    with appium_wire_server() as (endpoint, state):
        monkeypatch.setattr(appium_runtime, '_command_timeout', lambda: 2.0)
        attached = appium_runtime.attach_driver(SimpleNamespace(
            appium_endpoint=endpoint, remote_session_id='owned-close-deadline',
            device=SimpleNamespace(pk=1, id=1, device_id='wire-serial'),
            package=SimpleNamespace(package_name='org.example.wire'), system_port=8200,
            capabilities={'platformName': 'Android'},
        ), deadline=time.monotonic() + 0.75)
        state['hold_delete'] = True
        started = time.monotonic()
        with pytest.raises(appium_runtime.AppiumOutcomeUnknown):
            appium_runtime.quit_driver(attached)
        assert time.monotonic() - started < 1.5
        assert [request[:2] for request in state['requests']] == [
            ('DELETE', '/session/owned-close-deadline'),
        ]


def test_expired_attached_cleanup_deadline_sends_no_delete(monkeypatch):
    real_client()
    with appium_wire_server() as (endpoint, state):
        clock = {'now': 10.0}
        monkeypatch.setattr(appium_runtime, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
        attached = appium_runtime.attach_driver(SimpleNamespace(
            appium_endpoint=endpoint, remote_session_id='expired-close-deadline',
            device=SimpleNamespace(pk=1, id=1, device_id='wire-serial'),
            package=SimpleNamespace(package_name='org.example.wire'), system_port=8200,
            capabilities={'platformName': 'Android'},
        ), deadline=11.0)
        clock['now'] = 12.0
        with pytest.raises(appium_runtime.AppiumOutcomeUnknown):
            appium_runtime.quit_driver(attached)
        assert state['requests'] == []


def test_cleanup_also_has_a_total_headers_deadline(settings, monkeypatch):
    real_client()
    with appium_wire_server() as (endpoint, state):
        settings.APP_APPIUM_ENDPOINTS = {'1': endpoint}
        driver = appium_runtime.create_driver(
            SimpleNamespace(pk=1, id=1, device_id='wire-serial'),
            'org.example.wire', system_port=8200, deadline=time.monotonic() + 3,
        )
        state['hold_delete'] = True
        monkeypatch.setattr(appium_runtime, '_command_timeout', lambda: 0.15)
        started = time.monotonic()
        with pytest.raises(appium_runtime.AppiumOutcomeUnknown):
            appium_runtime.quit_driver(driver)
        assert time.monotonic() - started < 1.5
