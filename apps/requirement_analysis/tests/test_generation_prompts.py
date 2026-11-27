"""Exercise the messages sent to providers without making network requests."""
import asyncio
from types import SimpleNamespace

import pytest

from apps.requirement_analysis.models import AIModelService


CASE_OUTPUT = (
    '| 用例编号 | 测试场景 | 前置条件 | 操作步骤 | 预期结果 | 优先级 |\n'
    '| --- | --- | --- | --- | --- | --- |\n'
    '| TC-001 | 验证码登录 | 已注册账号 | 输入有效验证码 | 登录成功 | P1 |'
)


def task():
    return SimpleNamespace(
        requirement_text='短信验证码有效期为5分钟，连续输错5次后锁定10分钟。',
        writer_prompt_config=SimpleNamespace(content='按Markdown表格生成用例'),
        reviewer_prompt_config=SimpleNamespace(content='检查需求覆盖与重复项'),
        writer_model_config=object(),
        reviewer_model_config=object(),
    )


def test_streaming_and_complete_generation_send_the_same_grounded_requirements(monkeypatch):
    messages_sent = []

    async def complete(config, messages):
        messages_sent.append(messages)
        return {'choices': [{'message': {'content': CASE_OUTPUT}}]}

    async def stream(config, messages, callback=None):
        messages_sent.append(messages)
        yield CASE_OUTPUT

    monkeypatch.setattr(AIModelService, 'call_openai_compatible_api', complete)
    monkeypatch.setattr(AIModelService, 'call_openai_compatible_api_stream', stream)
    current = task()

    complete_result = asyncio.run(AIModelService.generate_test_cases(current))
    stream_result = asyncio.run(AIModelService.generate_test_cases_stream(current))

    assert complete_result == stream_result == CASE_OUTPUT
    assert messages_sent[0] == messages_sent[1]
    request = messages_sent[0][1]['content']
    assert current.requirement_text in request
    assert '不得自行编造' in request
    assert '待澄清' in request
    assert '不设数量上限' not in request
    assert '2-3个异常' not in request


@pytest.mark.parametrize('streaming', [False, True])
def test_review_includes_original_requirements_and_does_not_invent_acceptance_rules(monkeypatch, streaming):
    messages_sent = []

    async def complete(config, messages):
        messages_sent.append(messages)
        return {'choices': [{'message': {'content': '检查结果'}}]}

    async def stream(config, messages, callback=None):
        messages_sent.append(messages)
        yield '检查结果'

    monkeypatch.setattr(AIModelService, 'call_openai_compatible_api', complete)
    monkeypatch.setattr(AIModelService, 'call_openai_compatible_api_stream', stream)
    current = task()
    operation = AIModelService.review_test_cases_stream if streaming else AIModelService.review_test_cases

    assert asyncio.run(operation(current, CASE_OUTPUT)) == '检查结果'
    request = messages_sent[0][1]['content']
    assert current.requirement_text in request
    assert CASE_OUTPUT in request
    assert '未明确的验收标准不要自行补齐' in request


def test_revision_uses_original_requirements_to_reject_unsupported_review_suggestions(monkeypatch):
    messages_sent = []

    async def stream(config, messages, callback=None):
        messages_sent.append(messages)
        yield CASE_OUTPUT

    monkeypatch.setattr(AIModelService, 'call_openai_compatible_api_stream', stream)
    current = task()
    result = asyncio.run(AIModelService.revise_test_cases_based_on_review(
        current, CASE_OUTPUT, '请添加需求中未说明的具体错误码',
    ))

    assert result == CASE_OUTPUT
    request = messages_sent[0][1]['content']
    assert current.requirement_text in request
    assert '仅采纳有原始需求依据' in request
    assert '不得编造' in request
    assert '不要用整行加粗' in request
