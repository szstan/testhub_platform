import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / 'testhub_ci.py'
SPEC = importlib.util.spec_from_file_location('testhub_ci_script', MODULE_PATH)
TESTHUB_CI = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(TESTHUB_CI)


def test_trigger_dispatches_created_job_by_default(monkeypatch):
    calls = []

    def fake_request_json(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        if method == 'POST' and endpoint.endswith('/jobs/'):
            return 201, {'id': 42}
        return 202, {'receipt': {'supported': True}}

    monkeypatch.setattr(TESTHUB_CI, 'request_json', fake_request_json)
    monkeypatch.setattr(
        'sys.argv',
        [
            'testhub_ci.py', '--base-url', 'http://testhub', '--token', 'token',
            'trigger', '--project-id', '7', '--source-type', 'api',
            '--source-id', 'api:suite:42', '--idempotency-key', 'fixed-key',
        ],
    )

    assert TESTHUB_CI.main() == 0
    assert [item[0:2] for item in calls] == [
        ('POST', 'http://testhub/api/executions/jobs/'),
        ('POST', 'http://testhub/api/executions/jobs/42/dispatch/'),
    ]


def test_trigger_no_dispatch_only_creates_job(monkeypatch):
    calls = []

    def fake_request_json(method, endpoint, **kwargs):
        calls.append((method, endpoint))
        return 201, {'id': 42}

    monkeypatch.setattr(TESTHUB_CI, 'request_json', fake_request_json)
    monkeypatch.setattr(
        'sys.argv',
        [
            'testhub_ci.py', '--base-url', 'http://testhub', '--token', 'token',
            'trigger', '--project-id', '7', '--source-id', 'ci-run', '--no-dispatch',
        ],
    )

    assert TESTHUB_CI.main() == 0
    assert calls == [('POST', 'http://testhub/api/executions/jobs/')]
