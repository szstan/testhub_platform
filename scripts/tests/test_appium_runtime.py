from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from scripts.validate_linux_runtime import validate_appium


@pytest.fixture
def appium_status_server():
    requests = []
    reply = {"status": 200, "body": b'{"value":{"ready":true,"build":{"version":"3.2.0"}}}'}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(reply["status"])
            if reply["status"] == 302:
                self.send_header("Location", "/second-request")
            self.end_headers()
            self.wfile.write(reply["body"])

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests, reply
        finally:
            server.shutdown()
            thread.join()


def test_appium_readiness_uses_http_status_and_checks_version(appium_status_server):
    url, requests, reply = appium_status_server
    assert validate_appium(url, "3.2.0") == {"ok": True}
    assert requests == ["/status"]
    assert validate_appium(url, "3.1.0")["ok"] is False
    reply["body"] = b'{"value":{"ready":false}}'
    assert validate_appium(url)["ok"] is False


def test_appium_readiness_rejects_other_destinations_and_redirects(appium_status_server):
    url, requests, reply = appium_status_server
    assert validate_appium("http://example.invalid:4723")["ok"] is False
    assert validate_appium(f"{url}/extra")["ok"] is False
    assert requests == []
    reply["status"] = 302
    assert validate_appium(url)["ok"] is False
    assert requests == ["/status"]
