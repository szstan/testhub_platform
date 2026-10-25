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
from django.db import transaction
from django.utils import timezone

from apps.core.network import UnsafeOutboundURL, validate_outbound_url

from .models import TestCase, TestCaseSourceBinding, TestCaseSyncProfile

MAX_FILES = 1000
MAX_FILE_BYTES = 1 * 1024 * 1024

_DEFAULT_SECRET_ALIASES = {
    'github': 'TESTHUB_GIT_TOKEN',
    'gitlab': 'TESTHUB_GIT_TOKEN',
    # Keep the original deployment name usable only because it is explicitly
    # allowlisted here; arbitrary environment variable names are never read.
    'TESTHUB_GIT_TOKEN': 'TESTHUB_GIT_TOKEN',
}
_DEFAULT_PROVIDER_HOSTS = {
    'github': {'github.com', 'api.github.com', 'raw.githubusercontent.com'},
    'gitlab': {'gitlab.com'},
}


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


def _secret_aliases() -> dict[str, str]:
    """Return the server-owned alias -> environment variable mapping.

    The profile value is an alias, never an environment variable name selected
    by a project member.  A string setting is accepted for deployments that
    configure ``alias=ENV_NAME`` pairs through environment configuration.
    """
    configured = getattr(settings, 'TESTCASE_SYNC_SECRET_ALIASES', None)
    if configured is None:
        configured = getattr(settings, 'TESTCASE_SYNC_SECRET_ALIAS_MAP', None)
    if configured is None:
        configured = _DEFAULT_SECRET_ALIASES

    if isinstance(configured, dict):
        pairs = configured.items()
    elif isinstance(configured, str):
        parsed = []
        for item in configured.split(','):
            if '=' in item:
                parsed.append(item.split('=', 1))
        pairs = parsed
    elif isinstance(configured, (list, tuple, set)):
        # A list is useful for deployments where the alias and environment
        # variable deliberately share a name; it remains server-controlled.
        pairs = ((item, item) for item in configured)
    else:
        pairs = ()

    aliases = {}
    for alias, env_name in pairs:
        alias = str(alias or '').strip()
        env_name = str(env_name or '').strip()
        if alias and env_name:
            aliases[alias] = env_name
    return aliases


def _resolve_secret_alias(alias: str) -> str:
    alias = str(alias or '').strip()
    aliases = _secret_aliases()
    env_name = aliases.get(alias)
    if env_name is None:
        env_name = aliases.get(alias.lower())
    if not env_name:
        raise TestCaseSyncError('token_env_ref 必须是服务端已配置的 secret alias')
    return env_name


def _token(profile: TestCaseSyncProfile) -> str:
    ref = str(profile.token_env_ref or '').strip()
    if not ref:
        return ''
    env_name = _resolve_secret_alias(ref)
    token = os.environ.get(env_name, '')
    if not token:
        raise TestCaseSyncError(f'同步 secret alias 未配置: {ref}')
    return token


def _provider_hosts(provider: str) -> set[str]:
    configured = None
    for setting_name in (
        'TESTCASE_SYNC_ALLOWED_PROVIDER_HOSTS',
        'TESTCASE_SYNC_ALLOWED_HOSTS',
        'TESTCASE_SYNC_PROVIDER_HOSTS',
    ):
        candidate = getattr(settings, setting_name, None)
        if candidate:
            configured = candidate
            break

    if isinstance(configured, dict):
        values = configured.get(provider)
        if values is None:
            values = _DEFAULT_PROVIDER_HOSTS.get(provider, set())
    elif isinstance(configured, str):
        values = configured.split(',')
    elif configured is None:
        values = _DEFAULT_PROVIDER_HOSTS.get(provider, set())
    else:
        values = configured
    return {str(value).strip().lower() for value in values or () if str(value).strip()}


def _host_allowed(host: str, allowed: set[str]) -> bool:
    host = (host or '').lower().rstrip('.')
    for pattern in allowed:
        pattern = pattern.lower().rstrip('.')
        if pattern.startswith('*.'):
            suffix = pattern[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == pattern:
            return True
    return False


def _validate_provider_url(url: str, provider: str, *, allow_query: bool = True) -> str:
    try:
        safe_url = validate_outbound_url(url, allow_private=False)
    except UnsafeOutboundURL as exc:
        raise TestCaseSyncError('同步源地址不允许访问') from exc
    parsed = urlparse(safe_url)
    if not allow_query and (parsed.query or parsed.fragment):
        raise TestCaseSyncError('同步源基础地址不能包含 query 或 fragment')
    if not _host_allowed(parsed.hostname or '', _provider_hosts(provider)):
        raise TestCaseSyncError(f'{provider} 同步主机不在服务端允许列表中')
    return safe_url


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
    repository_url = _validate_provider_url(profile.repository_url, 'github', allow_query=False)
    owner, repository = _repository_parts(repository_url)
    branch = quote(profile.branch or 'main', safe='')
    tree_url = _validate_provider_url(
        f'https://api.github.com/repos/{quote(owner)}/{quote(repository)}/git/trees/{branch}?recursive=1',
        'github',
    )
    tree = _request_json(tree_url, token=token)
    if not isinstance(tree, dict) or tree.get('truncated'):
        raise TestCaseSyncError('GitHub tree 结果为空或被截断')
    prefix = (profile.path or '').strip('/ ')
    files = []
    for item in tree.get('tree') or []:
        path = str(item.get('path') or '')
        if item.get('type') == 'blob' and (not prefix or path == prefix or path.startswith(prefix + '/')) and path.lower().endswith(('.yml', '.yaml')):
            raw_url = _validate_provider_url(
                f'https://raw.githubusercontent.com/{owner}/{repository}/{quote(profile.branch or "main", safe="")}/{quote(path, safe="/")}',
                'github',
            )
            files.append((path, str(item.get('sha') or ''), raw_url))
    return str(tree.get('sha') or ''), files


def _gitlab_files(profile: TestCaseSyncProfile, token: str):
    repository_url = _validate_provider_url(profile.repository_url, 'gitlab', allow_query=False)
    parsed_repository = urlparse(repository_url)
    owner, repository = _repository_parts(repository_url)
    if profile.api_base_url:
        base = _validate_provider_url(profile.api_base_url.rstrip('/'), 'gitlab', allow_query=False)
    else:
        base = _validate_provider_url(
            f'{parsed_repository.scheme}://{parsed_repository.netloc}/api/v4',
            'gitlab',
            allow_query=False,
        )
    project = quote(f'{owner}/{repository}', safe='')
    tree_url = f'{base}/projects/{project}/repository/tree?ref={quote(profile.branch or "main", safe="")}&recursive=true&per_page=100'
    tree = _request_json(tree_url, token=token)
    prefix = (profile.path or '').strip('/ ')
    files = []
    for item in tree if isinstance(tree, list) else []:
        path = str(item.get('path') or '')
        if item.get('type') == 'blob' and (not prefix or path == prefix or path.startswith(prefix + '/')) and path.lower().endswith(('.yml', '.yaml')):
            raw_url = _validate_provider_url(
                f'{base}/projects/{project}/repository/files/{quote(path, safe="")}/raw?ref={quote(profile.branch or "main", safe="")}',
                'gitlab',
            )
            files.append((path, str(item.get('id') or ''), raw_url))
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
            _validate_provider_url(raw_url, profile.provider),
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
