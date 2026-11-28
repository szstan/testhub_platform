import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from apps.testcases.sync import TestCaseSyncError, _load_files, _request_json


COMMIT = 'a' * 40


@asynccontextmanager
async def stream_context(response):
    try:
        yield response
    finally:
        response.close()


def streamed_response(*chunks):
    response = Mock(status_code=200)
    async def aiter_bytes(**_kwargs):
        for chunk in chunks:
            yield chunk
    response.aiter_bytes = aiter_bytes
    return response


def files(*count):
    return [
        (f'tests/{index}.yaml', f'blob-{index}', f'https://raw.githubusercontent.com/example/cases/{index}.yaml')
        for index in range(count[0])
    ]


def test_sync_streams_each_file_and_applies_total_byte_budget():
    profile = SimpleNamespace(provider='github')
    responses = [streamed_response(b'abc'), streamed_response(b'de')]
    with patch('apps.testcases.sync._github_files', return_value=(COMMIT, files(2))), patch(
        'apps.testcases.sync._validate_provider_url', side_effect=lambda url, *_args, **_kwargs: url,
    ), patch('apps.testcases.sync._stream_get', side_effect=[
        stream_context(responses[0]), stream_context(responses[1]),
    ]) as get, patch(
        'apps.testcases.sync.MAX_FILE_BYTES', 4,
    ), patch('apps.testcases.sync.MAX_TOTAL_BYTES', 5):
        commit, loaded = _load_files(profile, '')

    assert commit == COMMIT
    assert [item[2] for item in loaded] == [b'abc', b'de']
    assert all(call.kwargs['allow_redirects'] is False for call in get.call_args_list)
    assert all(response.close.called for response in responses)


def test_sync_rejects_single_file_over_limit_while_streaming():
    profile = SimpleNamespace(provider='github')
    response = streamed_response(b'ab', b'cd')
    with patch('apps.testcases.sync._github_files', return_value=(COMMIT, files(1))), patch(
        'apps.testcases.sync._validate_provider_url', side_effect=lambda url, *_args, **_kwargs: url,
    ), patch('apps.testcases.sync._stream_get', return_value=stream_context(response)), patch(
        'apps.testcases.sync.MAX_FILE_BYTES', 3,
    ):
        with pytest.raises(TestCaseSyncError, match='文件过大'):
            _load_files(profile, '')
    assert response.close.called




def test_metadata_json_response_is_bounded_and_closed_when_rejected():
    response = streamed_response(b'{"oversized":true}')
    with patch('apps.testcases.sync.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.testcases.sync._stream_get', return_value=stream_context(response),
    ), patch('apps.testcases.sync.MAX_JSON_RESPONSE_BYTES', 8):
        with pytest.raises(TestCaseSyncError, match='JSON 响应'):
            _request_json('https://api.github.com/repositories')
    assert response.close.called


def test_metadata_bytes_share_the_total_budget_with_yaml_files():
    profile = SimpleNamespace(
        provider='github', repository_url='https://github.com/acme/cases',
        branch='main', path='tests',
    )
    commit_body = json.dumps({'sha': COMMIT}).encode('utf-8')
    tree_body = json.dumps({
        'tree': [{'type': 'blob', 'path': 'tests/case.yaml', 'sha': 'blob'}],
    }).encode('utf-8')
    responses = [
        streamed_response(commit_body),
        streamed_response(tree_body),
        streamed_response(b'xy'),
    ]
    budget = len(commit_body) + len(tree_body) + 1
    with patch('apps.testcases.sync.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.testcases.sync._stream_get', side_effect=[
            stream_context(response) for response in responses
        ],
    ), patch('apps.testcases.sync.MAX_TOTAL_BYTES', budget):
        with pytest.raises(TestCaseSyncError, match='总数据量'):
            _load_files(profile, '')
    assert all(response.close.called for response in responses)


def test_metadata_stream_checks_deadline_between_chunks_and_closes_response():
    response = streamed_response()
    clock = {'now': 0}

    async def aiter_bytes(**_kwargs):
        yield b'{"ok":'
        clock['now'] = 11
        yield b'true}'

    response.aiter_bytes = aiter_bytes
    with patch('apps.testcases.sync.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.testcases.sync._stream_get', return_value=stream_context(response),
    ), patch('apps.testcases.sync._sync_clock', side_effect=lambda: clock['now']):
        with pytest.raises(TestCaseSyncError, match='最大运行时长'):
            _request_json('https://api.github.com/repositories', deadline=10)
    assert response.close.called


def test_sync_checks_walltime_deadline_before_starting_file_download():
    profile = SimpleNamespace(provider='github')
    with patch('apps.testcases.sync._github_files', return_value=(COMMIT, files(1))), patch(
        'apps.testcases.sync._sync_clock', return_value=12,
    ), patch('apps.testcases.sync._stream_get') as get:
        with pytest.raises(TestCaseSyncError, match='最大运行时长'):
            _load_files(profile, '', deadline=12)
    get.assert_not_called()
