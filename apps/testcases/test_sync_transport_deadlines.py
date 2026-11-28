"""Exercise deadlines while headers/body assembly are still awaiting network IO."""
import time
from types import SimpleNamespace
from unittest.mock import patch

import anyio
import httpx
import pytest

from apps.testcases import sync


class DripBody(httpx.AsyncByteStream):
    def __init__(self):
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(1000):
            await anyio.sleep(0.01)
            self.yielded += 1
            yield b'x' * 1024

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('kind', ['metadata', 'yaml'])
def test_absolute_deadline_cancels_body_before_first_64k_chunk(kind):
    body = DripBody()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=body, request=request))
    started = time.monotonic()
    deadline = started + 0.25
    with patch.object(sync, 'outbound_httpx_options', return_value={
        'transport': transport, 'trust_env': False,
    }) as options:
        with pytest.raises(sync.TestCaseSyncError, match='最大运行时长'):
            if kind == 'metadata':
                sync._request_json('https://api.github.com/repos/example/cases', deadline=deadline)
            else:
                with patch.object(sync, '_github_files', return_value=(
                    'a' * 40,
                    [('tests/case.yaml', 'blob', 'https://raw.githubusercontent.com/example/cases/case.yaml')],
                )):
                    sync._load_files(SimpleNamespace(provider='github'), '', deadline=deadline)
    assert 0 < body.yielded < 64
    assert time.monotonic() - started < 2
    assert body.closed is True
    # The production request always selects connection-time guarded HTTPX.
    options.assert_called_once_with(False)


def test_absolute_deadline_cancels_request_while_waiting_for_headers():
    cancellation = []

    async def no_headers(_request):
        try:
            await anyio.sleep_forever()
        finally:
            cancellation.append(True)

    transport = httpx.MockTransport(no_headers)
    started = time.monotonic()
    with patch.object(sync, 'outbound_httpx_options', return_value={
        'transport': transport, 'trust_env': False,
    }):
        with pytest.raises(sync.TestCaseSyncError, match='最大运行时长'):
            sync._request_json('https://api.github.com/repos/example/cases', deadline=started + 0.1)
    assert cancellation == [True]
    assert time.monotonic() - started < 2


def test_valid_response_keeps_actual_byte_budget_and_json_decoding():
    raw = b'{"sha":"bounded"}'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=raw, request=request))
    budget = sync._SyncReadBudget()
    with patch.object(sync, 'outbound_httpx_options', return_value={
        'transport': transport, 'trust_env': False,
    }):
        assert sync._request_json('https://api.github.com/repos/example/cases', budget=budget) == {'sha': 'bounded'}
    assert budget.total_bytes == len(raw)
