"""GitHub/GitLab Tests as Code synchronization."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote, urlparse

import requests
import yaml
from django.conf import settings
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.network import UnsafeOutboundURL, validate_outbound_url

from .models import TestCase, TestCaseSourceBinding, TestCaseSyncProfile

MAX_FILES = 1000
MAX_FILE_BYTES = 1 * 1024 * 1024


class TestCaseSyncError(ValueError):
    pass


@dataclass(frozen=True)
class SyncSummary:
    profile_id: int
    provider: str
    commit: str
    files: int
    created: int
    updated: int
    skipped: int
    conflicts: int
    warnings: list[str]


def _token(profile: TestCaseSyncProfile) -> str:
    ref = str(profile.token_env_ref or '').strip()
    return os.environ.get(ref, '') if ref else ''


def _request_json(url: str, *, token: str = ''):
    safe_url = validate_outbound_url(
        url,
        allow_private=bool(getattr(settings, 'PERF_ALLOW_PRIVATE_TARGETS', False)),
    )
    headers = {'Accept': 'application/json', 'User-Agent': 'TestHub-TestsAsCode/1.0'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
        headers['PRIVATE-TOKEN'] = token
    response = requests.get(safe_url, headers=headers, timeout=20, allow_redirects=False)
    if response.status_code >= 400:
        raise TestCaseSyncError(f'同步源请求失败 HTTP {response.status_code}')
    try:
        return response.json()
    except ValueError as exc:
        raise TestCaseSyncError('同步源返回了无效 JSON') from exc


def _repository_parts(repository_url: str) -> tuple[str, str]:
    parsed = urlparse(repository_url)
    if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
        raise TestCaseSyncError('repository_url 必须是无凭证的 http/https 地址')
    parts = [part for part in parsed.path.strip('/').split('/') if part]
    if len(parts) < 2:
        raise TestCaseSyncError('repository_url 缺少 owner/repository')
    return parts[-2], parts[-1].removesuffix('.git')


def _github_files(profile: TestCaseSyncProfile, token: str):
    owner, repository = _repository_parts(profile.repository_url)
    branch = quote(profile.branch or 'main', safe='')
    tree = _request_json(f'https://api.github.com/repos/{quote(owner)}/{quote(repository)}/git/trees/{branch}?recursive=1', token=token)
    if not isinstance(tree, dict) or tree.get('truncated'):
        raise TestCaseSyncError('GitHub tree 结果为空或被截断')
    prefix = (profile.path or '').strip('/ ')
    files = []
    for item in tree.get('tree') or []:
        path = str(item.get('path') or '')
        if item.get('type') == 'blob' and (not prefix or path == prefix or path.startswith(prefix + '/')) and path.lower().endswith(('.yml', '.yaml')):
            files.append((path, str(item.get('sha') or ''), f'https://raw.githubusercontent.com/{owner}/{repository}/{quote(profile.branch or "main", safe="")}/{quote(path, safe="/")}'))
    return str(tree.get('sha') or ''), files


def _gitlab_files(profile: TestCaseSyncProfile, token: str):
    owner, repository = _repository_parts(profile.repository_url)
    base = profile.api_base_url or f'{urlparse(profile.repository_url).scheme}://{urlparse(profile.repository_url).netloc}/api/v4'
    base = validate_outbound_url(base.rstrip('/'), allow_private=False)
    project = quote(f'{owner}/{repository}', safe='')
    tree = _request_json(f'{base}/projects/{project}/repository/tree?ref={quote(profile.branch or "main", safe="")}&recursive=true&per_page=100', token=token)
    prefix = (profile.path or '').strip('/ ')
    files = []
    for item in tree if isinstance(tree, list) else []:
        path = str(item.get('path') or '')
        if item.get('type') == 'blob' and (not prefix or path == prefix or path.startswith(prefix + '/')) and path.lower().endswith(('.yml', '.yaml')):
            files.append((path, str(item.get('id') or ''), f'{base}/projects/{project}/repository/files/{quote(path, safe="")}/raw?ref={quote(profile.branch or "main", safe="")}'))
    return '', files


def _load_files(profile: TestCaseSyncProfile, token: str):
    if profile.provider == 'github':
        commit, files = _github_files(profile, token)
    else:
        commit, files = _gitlab_files(profile, token)
    if len(files) > MAX_FILES:
        raise TestCaseSyncError(f'同步文件数不能超过 {MAX_FILES}')
    loaded = []
    for path, source_sha, raw_url in files:
        response = requests.get(
            validate_outbound_url(
                raw_url,
                allow_private=bool(getattr(settings, 'PERF_ALLOW_PRIVATE_TARGETS', False)),
            ),
            headers={'Accept': 'text/plain', 'User-Agent': 'TestHub-TestsAsCode/1.0', **({'PRIVATE-TOKEN': token} if token else {})},
            timeout=20,
            allow_redirects=False,
        )
        if response.status_code >= 400:
            raise TestCaseSyncError(f'读取同步文件失败 HTTP {response.status_code}')
        if len(response.content) > MAX_FILE_BYTES:
            raise TestCaseSyncError(f'同步文件过大: {path}')
        loaded.append((path, source_sha, response.content))
    return commit, loaded


def _testcase_values(document: dict, path: str) -> dict:
    external_ref = str(document.get('id') or document.get('external_ref') or path.rsplit('.', 1)[0]).strip()
    title = str(document.get('title') or '').strip()
    if not external_ref or not title:
        raise TestCaseSyncError(f'{path}: id/external_ref 和 title 均为必填项')
    steps = document.get('steps', '')
    if isinstance(steps, (list, dict)):
        steps = json.dumps(steps, ensure_ascii=False)
    tags = document.get('tags') or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    priority = str(document.get('priority') or 'medium').lower()
    if priority in {'p0', 'critical'}:
        priority = 'critical'
    elif priority in {'p1', 'high'}:
        priority = 'high'
    elif priority in {'p3', 'low'}:
        priority = 'low'
    else:
        priority = 'medium'
    test_type = str(document.get('type') or document.get('test_type') or 'functional').lower()
    if test_type in {'auto', 'automated', 'e2e'}:
        test_type = 'functional'
    return {
        'external_ref': external_ref[:255],
        'title': title[:500],
        'description': str(document.get('description') or '')[:100000],
        'preconditions': str(document.get('preconditions') or '')[:100000],
        'steps': str(steps)[:1000],
        'expected_result': str(document.get('expected_result') or document.get('expected') or '')[:100000],
        'priority': priority,
        'test_type': test_type if test_type in {'functional', 'integration', 'api', 'ui', 'performance', 'security'} else 'functional',
        'tags': [str(item)[:100] for item in tags[:100]],
    }


def sync_profile(profile: TestCaseSyncProfile) -> SyncSummary:
    if not profile.enabled:
        raise TestCaseSyncError('同步配置已禁用')
    token = _token(profile)
    commit, files = _load_files(profile, token)
    created = updated = skipped = conflicts = 0
    warnings: list[str] = []
    seen_refs: set[str] = set()
    for path, source_sha, raw in files:
        try:
            document = yaml.safe_load(raw.decode('utf-8'))
            if not isinstance(document, dict):
                raise TestCaseSyncError('YAML 根节点必须是对象')
            values = _testcase_values(document, path)
            ref = values['external_ref']
            if ref in seen_refs:
                warnings.append(f'{path}: external_ref 重复')
                skipped += 1
                continue
            seen_refs.add(ref)
            source_hash = hashlib.sha256(raw).hexdigest()
            binding = TestCaseSourceBinding.objects.filter(profile=profile, external_ref=ref).select_related('testcase').first()
            testcase = binding.testcase if binding else TestCase.objects.filter(project=profile.project, external_ref=ref).first()
            if testcase is not None and binding is not None and testcase.updated_at > binding.last_synced_at and binding.source_hash != source_hash:
                warnings.append(f'{path}: 检测到本地修改，跳过覆盖')
                conflicts += 1
                continue
            with transaction.atomic():
                if testcase is None:
                    testcase = TestCase.objects.create(project=profile.project, author=profile.created_by, **values)
                    created += 1
                else:
                    for key, value in values.items():
                        setattr(testcase, key, value)
                    testcase.project = profile.project
                    testcase.save(update_fields=[*values.keys(), 'project', 'updated_at'])
                    updated += 1
                TestCaseSourceBinding.objects.update_or_create(
                    testcase=testcase,
                    defaults={
                        'profile': profile,
                        'external_ref': ref,
                        'source_path': path,
                        'source_commit': commit or source_sha,
                        'source_hash': source_hash,
                        'last_synced_at': timezone.now(),
                    },
                )
        except (UnicodeDecodeError, yaml.YAMLError, TestCaseSyncError, TypeError, ValueError) as exc:
            warnings.append(f'{path}: {exc}')
            skipped += 1

    profile.last_commit_sha = commit or (files[0][1] if files else '')
    profile.last_sync_at = timezone.now()
    profile.save(update_fields=['last_commit_sha', 'last_sync_at', 'updated_at'])
    return SyncSummary(profile.pk, profile.provider, profile.last_commit_sha, len(files), created, updated, skipped, conflicts, warnings)
