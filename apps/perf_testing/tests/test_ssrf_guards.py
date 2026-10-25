import asyncio
import socket
from types import SimpleNamespace

import pytest

from apps.core.network import UnsafeOutboundURL
from apps.perf_testing.engines import builtin
from apps.perf_testing.services import jmx_inspect


def _public_dns(monkeypatch):
    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port)),
        ],
    )


class _Response:
    def __init__(self):
        self.status_code = 200
        self.headers = {'content-type': 'text/plain'}
        self.content = b'ok'
        self.text = 'ok'

    def json(self):
        return {'ok': True}


class _Client:
    last_calls = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        type(self).last_calls = self.calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _Response()


def test_debug_run_rejects_rendered_private_url_before_http_request(monkeypatch):
    monkeypatch.setattr(builtin, '_allow_private_targets', lambda: False)
    fake = _Client
    fake.last_calls = []
    monkeypatch.setattr(builtin.httpx, 'AsyncClient', fake)

    result = asyncio.run(builtin.debug_run({
        'env_config': {'base_url': 'https://public.example.test'},
        'runtime_config': {},
        'variables': [{'name': 'target', 'value': 'http://127.0.0.1:8080'}],
        'steps': [{
            'name': 'private', 'method': 'GET', 'url': '{{target}}/admin',
            'enabled': True,
        }],
    }))

    assert result[0]['success'] is False
    assert 'UnsafeOutboundURL' in result[0]['error']
    assert fake.last_calls == []


def test_debug_run_validates_rendered_public_url_before_request(monkeypatch):
    _public_dns(monkeypatch)
    monkeypatch.setattr(builtin, '_allow_private_targets', lambda: False)
    fake = _Client
    fake.last_calls = []
    monkeypatch.setattr(builtin.httpx, 'AsyncClient', fake)

    result = asyncio.run(builtin.debug_run({
        'env_config': {},
        'runtime_config': {},
        'variables': [{'name': 'target', 'value': 'https://public.example.test'}],
        'steps': [{
            'name': 'public', 'method': 'GET', 'url': '{{target}}/health',
            'enabled': True,
        }],
    }))

    assert result[0]['success'] is True
    assert fake.last_calls[0][1] == 'https://public.example.test/health'


@pytest.mark.asyncio
async def test_httpx_redirect_hook_rejects_private_next_request(monkeypatch):
    monkeypatch.setattr(builtin, '_allow_private_targets', lambda: False)
    response = SimpleNamespace(
        url='https://public.example.test/start',
        headers={'location': 'http://127.0.0.1:8080/admin'},
    )
    with pytest.raises(UnsafeOutboundURL):
        await builtin._validate_httpx_redirect(response)


def test_jmx_inspection_exposes_dynamic_hosts():
    text = '''
<jmeterTestPlan>
  <hashTree>
    <ThreadGroup testname="users" enabled="true">
      <stringProp name="ThreadGroup.num_threads">1</stringProp>
    </ThreadGroup>
    <hashTree>
      <HTTPSamplerProxy testname="request" enabled="true">
        <stringProp name="HTTPSampler.domain">${TARGET_HOST}</stringProp>
      </HTTPSamplerProxy>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
'''
    meta = jmx_inspect.inspect_jmx_text(text)
    assert meta['valid'] is True
    assert meta['hosts'] == []
    assert meta['dynamic_hosts'] == ['${TARGET_HOST}']
    assert jmx_inspect.summarize(meta)['dynamic_hosts'] == ['${TARGET_HOST}']
