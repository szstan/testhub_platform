import json

import pytest

from apps.requirement_analysis.ai_contracts import (
    AIContractError,
    normalize_generation_output,
    parse_draft,
    render_markdown,
    summarize_generation_output,
)


def test_structured_draft_is_validated_redacted_fingerprinted_and_rendered():
    raw = json.dumps({
        'test_cases': [{
            'title': '登录成功',
            'priority': 'P0',
            'precondition': '用户已注册',
            'steps': ['输入账号', '输入密码', '点击登录'],
            'expected_result': '进入首页',
            'source_refs': ['需求文档第 2 节'],
            'confidence': 0.92,
        }],
        'coverage_notes': ['正常路径'],
        'secret': 'must be redacted',
    })

    draft = parse_draft(raw)
    rendered = render_markdown(draft)

    assert draft['contract_version'] == '1.0'
    assert len(draft['draft_fingerprint']) == 64
    assert draft['cases'][0]['case_id'] == 'AI-001'
    assert 'must be redacted' not in json.dumps(draft, ensure_ascii=False)
    assert 'AI-001: 登录成功' in rendered
    assert '1. 输入账号' in rendered
    summary = summarize_generation_output(raw)
    assert summary['structured'] is True
    assert summary['case_count'] == 1


def test_legacy_markdown_remains_compatible():
    raw = '## Legacy case\n- step 1'

    normalized, draft = normalize_generation_output(raw)

    assert normalized == raw
    assert draft is None


def test_invalid_structured_draft_is_rejected():
    with pytest.raises(AIContractError):
        parse_draft('{"test_cases": [{"title": "missing steps"}]}')
