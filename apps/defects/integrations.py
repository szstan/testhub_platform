"""External defect integration helpers."""

from __future__ import annotations

import os
from urllib.parse import urlparse

import requests

from apps.core.network import validate_outbound_url

from .models import Defect, GitHubIssueIntegration


class ExternalIssueError(ValueError):
    pass


def _repo_parts(repository_url: str):
    parsed = urlparse(repository_url)
    try:
        validate_outbound_url(repository_url, allow_private=False)
    except UnsafeOutboundURL as exc:
        raise ExternalIssueError('GitHub repository_url 不允许访问') from exc
    parts = [part for part in parsed.path.strip('/').split('/') if part]
    if len(parts) < 2:
        raise ExternalIssueError('GitHub repository_url 缺少 owner/repository')
    return parts[-2], parts[-1].removesuffix('.git')


def push_defect_to_github(defect: Defect, integration: GitHubIssueIntegration):
    if defect.external_key:
        return {'created': False, 'external_key': defect.external_key, 'external_url': defect.external_url}
    token = os.environ.get(integration.token_env_ref, '')
    if not token:
        raise ExternalIssueError('GitHub token 未配置')
    owner, repo = _repo_parts(integration.repository_url)
    endpoint = f'https://api.github.com/repos/{owner}/{repo}/issues'
    payload = {
        'title': defect.title[:300],
        'body': '\n\n'.join(filter(None, [
            defect.description,
            f'Expected: {defect.expected_result}' if defect.expected_result else '',
            f'Actual: {defect.actual_result}' if defect.actual_result else '',
            f'TestHub defect: {defect.code}',
        ]))[:60000],
        'labels': [f'testhub:{defect.priority}', f'testhub:{defect.severity}'],
    }
    response = requests.post(
        endpoint,
        json=payload,
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'},
        timeout=20,
        allow_redirects=False,
    )
    if response.status_code >= 400:
        raise ExternalIssueError(f'GitHub issue 创建失败 HTTP {response.status_code}')
    data = response.json()
    key = str(data.get('number') or '')
    url = str(data.get('html_url') or '')
    if not key or not url:
        raise ExternalIssueError('GitHub issue 响应缺少标识')
    defect.external_provider = 'github'
    defect.external_key = key
    defect.external_url = url[:500]
    defect.save(update_fields=['external_provider', 'external_key', 'external_url', 'updated_at'])
    return {'created': True, 'external_key': key, 'external_url': url}
