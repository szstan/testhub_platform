"""GitHub/GitLab Tests as Code synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import anyio
import httpx
import yaml
from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, SequenceNode

from apps.core.http_transport import outbound_httpx_options
from apps.core.network import UnsafeOutboundURL, validate_outbound_url
from apps.projects.access import user_can_write_project

from .models import TestCase, TestCaseSourceBinding, TestCaseSyncProfile
from .revisions import record_test_case_revision

MAX_FILES = 1000
MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SYNC_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 20
STREAM_CHUNK_SIZE = 64 * 1024
MAX_TREE_PAGES = 100
MAX_YAML_EXPANDED_NODES = 10_000
MAX_YAML_CONSTRUCTION_WORK = 10_000
MAX_YAML_EXPANDED_BYTES = 4 * 1024 * 1024
MAX_YAML_DEPTH = 32
MAX_STEPS_JSON_CHARS = 1000

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


class _SyncProfileFenceError(TestCaseSyncError):
    pass


class _SyncReadBudget:
    def __init__(self):
        self.total_bytes = 0

    def consume(self, size):
        self.total_bytes += size
        if self.total_bytes > MAX_TOTAL_BYTES:
            raise TestCaseSyncError(f'同步总数据量不能超过 {MAX_TOTAL_BYTES} bytes')


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
        # Validate URL shape here. DNS/address policy is enforced by the pinned
        # HTTPX transport inside the cancellable request deadline below.
        safe_url = validate_outbound_url(url, allow_private=True)
    except UnsafeOutboundURL as exc:
        raise TestCaseSyncError('同步源地址不允许访问') from exc
    parsed = urlparse(safe_url)
    if not allow_query and (parsed.query or parsed.fragment):
        raise TestCaseSyncError('同步源基础地址不能包含 query 或 fragment')
    if not _host_allowed(parsed.hostname or '', _provider_hosts(provider)):
        raise TestCaseSyncError(f'{provider} 同步主机不在服务端允许列表中')
    return safe_url


def _sync_clock():
    return time.monotonic()


def _check_deadline(deadline):
    if deadline is not None and _sync_clock() >= deadline:
        raise TestCaseSyncError('同步超过最大运行时长')


def _request_timeout(deadline):
    if deadline is None:
        return REQUEST_TIMEOUT_SECONDS
    remaining = deadline - _sync_clock()
    if remaining <= 0:
        raise TestCaseSyncError('同步超过最大运行时长')
    return min(REQUEST_TIMEOUT_SECONDS, remaining)


class _BudgetedSafeLoader(yaml.SafeLoader):
    def __init__(self, stream, *, deadline=None):
        self._sync_yaml_deadline = deadline
        self._sync_yaml_compose_depth = 0
        self._sync_yaml_composed_nodes = 0
        self._sync_yaml_constructed_nodes = 0
        self._sync_yaml_merge_work = 0
        super().__init__(stream)

    def compose_node(self, parent, index):
        _check_deadline(self._sync_yaml_deadline)
        self._sync_yaml_compose_depth += 1
        try:
            if self._sync_yaml_compose_depth - 1 > MAX_YAML_DEPTH:
                raise TestCaseSyncError('YAML 嵌套深度超过限制')
            self._sync_yaml_composed_nodes += 1
            if self._sync_yaml_composed_nodes > MAX_YAML_EXPANDED_NODES:
                raise TestCaseSyncError('YAML 构造节点数超过限制')
            return super().compose_node(parent, index)
        finally:
            self._sync_yaml_compose_depth -= 1

    def construct_object(self, node, deep=False):
        _check_deadline(self._sync_yaml_deadline)
        self._sync_yaml_constructed_nodes += 1
        if self._sync_yaml_constructed_nodes > MAX_YAML_EXPANDED_NODES:
            raise TestCaseSyncError('YAML 构造节点数超过限制')
        return super().construct_object(node, deep=deep)

    def _preflight_node_graph(self, root):
        remaining = MAX_YAML_EXPANDED_NODES
        active = set()

        def visit(node, depth):
            nonlocal remaining
            _check_deadline(self._sync_yaml_deadline)
            if depth > MAX_YAML_DEPTH:
                raise TestCaseSyncError('YAML 嵌套深度超过限制')
            remaining -= 1
            if remaining < 0:
                raise TestCaseSyncError('YAML 展开节点数超过限制')

            marker = id(node)
            if marker in active:
                raise TestCaseSyncError('YAML 包含循环引用')
            if isinstance(node, SequenceNode):
                children = node.value
            elif isinstance(node, MappingNode):
                children = (child for pair in node.value for child in pair)
            else:
                return

            active.add(marker)
            try:
                for child in children:
                    visit(child, depth + 1)
            finally:
                active.remove(marker)

        visit(root, 0)

    def get_single_data(self):
        node = self.get_single_node()
        if node is None:
            return None
        self._preflight_node_graph(node)
        _check_deadline(self._sync_yaml_deadline)
        return self.construct_document(node)

    def _consume_merge_work(self, amount):
        _check_deadline(self._sync_yaml_deadline)
        if self._sync_yaml_merge_work + amount > MAX_YAML_CONSTRUCTION_WORK:
            raise TestCaseSyncError('YAML 合并展开工作量超过限制')
        self._sync_yaml_merge_work += amount

    @staticmethod
    def _extend_merge_pairs(target, pairs):
        target.extend(pairs)

    def flatten_mapping(self, node):
        merge = []
        index = 0
        while index < len(node.value):
            _check_deadline(self._sync_yaml_deadline)
            key_node, value_node = node.value[index]
            if key_node.tag == 'tag:yaml.org,2002:merge':
                del node.value[index]
                if isinstance(value_node, MappingNode):
                    self.flatten_mapping(value_node)
                    self._consume_merge_work(len(value_node.value))
                    self._extend_merge_pairs(merge, value_node.value)
                elif isinstance(value_node, SequenceNode):
                    submerge = []
                    for subnode in value_node.value:
                        _check_deadline(self._sync_yaml_deadline)
                        if not isinstance(subnode, MappingNode):
                            raise ConstructorError(
                                'while constructing a mapping', node.start_mark,
                                'expected a mapping for merging, but found %s' % subnode.id,
                                subnode.start_mark,
                            )
                        self.flatten_mapping(subnode)
                        submerge.append(subnode.value)
                    submerge.reverse()
                    for pairs in submerge:
                        self._consume_merge_work(len(pairs))
                        self._extend_merge_pairs(merge, pairs)
                else:
                    raise ConstructorError(
                        'while constructing a mapping', node.start_mark,
                        'expected a mapping or list of mappings for merging, but found %s'
                        % value_node.id,
                        value_node.start_mark,
                    )
            elif key_node.tag == 'tag:yaml.org,2002:value':
                key_node.tag = 'tag:yaml.org,2002:str'
                index += 1
            else:
                index += 1
        if merge:
            self._consume_merge_work(len(merge) + len(node.value))
            node.value = merge + node.value


def _load_sync_yaml(stream, deadline=None):
    loader = _BudgetedSafeLoader(stream, deadline=deadline)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


@asynccontextmanager
async def _stream_get(url, *, headers, timeout, allow_redirects=False, allow_private=False):
    if allow_private:
        raise TestCaseSyncError('同步源不允许使用私网传输')
    async with httpx.AsyncClient(
        timeout=timeout, **outbound_httpx_options(False),
    ) as client:
        async with client.stream(
            'GET', url, headers=headers, follow_redirects=allow_redirects,
        ) as response:
            yield response


def _read_response_bytes(url, *, headers, deadline, budget, max_bytes, size_error, http_error):
    async def read():
        timeout = _request_timeout(deadline)
        chunks = []
        size = 0
        # Cancellation encloses connection, headers and body assembly. An idle
        # read timeout or a check after iter_content yields cannot stop drips.
        with anyio.fail_after(timeout):
            async with _stream_get(
                url, headers=headers, timeout=timeout,
                allow_redirects=False, allow_private=False,
            ) as response:
                _check_deadline(deadline)
                if response.status_code >= 400:
                    raise TestCaseSyncError(f'{http_error} HTTP {response.status_code}')
                async for chunk in response.aiter_bytes(chunk_size=STREAM_CHUNK_SIZE):
                    _check_deadline(deadline)
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise TestCaseSyncError(size_error)
                    budget.consume(len(chunk))
                    chunks.append(chunk)
        _check_deadline(deadline)
        return b''.join(chunks)

    try:
        return async_to_sync(read)()
    except (TimeoutError, httpx.TimeoutException) as exc:
        _check_deadline(deadline)
        raise TestCaseSyncError('同步源请求超过最大读取时限') from exc


def _request_json(url: str, *, token: str = '', deadline=None, budget=None):
    # Only syntax is checked synchronously; guarded HTTPX validates the resolved
    # target at connection time under the absolute request budget.
    safe_url = validate_outbound_url(url, allow_private=True)
    headers = {'Accept': 'application/json', 'User-Agent': 'TestHub-TestsAsCode/1.0'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
        headers['PRIVATE-TOKEN'] = token
    budget = budget or _SyncReadBudget()
    _check_deadline(deadline)
    raw = _read_response_bytes(
        safe_url, headers=headers, deadline=deadline, budget=budget,
        max_bytes=MAX_JSON_RESPONSE_BYTES,
        size_error=f'同步源 JSON 响应不能超过 {MAX_JSON_RESPONSE_BYTES} bytes',
        http_error='同步源请求失败',
    )
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise TestCaseSyncError('同步源返回了无效 JSON') from exc
    _check_deadline(deadline)
    return document


def _repository_parts(repository_url: str) -> tuple[str, str]:
    parsed = urlparse(repository_url)
    if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
        raise TestCaseSyncError('repository_url 必须是无凭证的 http/https 地址')
    parts = [part for part in parsed.path.strip('/').split('/') if part]
    if len(parts) < 2:
        raise TestCaseSyncError('repository_url 缺少 owner/repository')
    return parts[-2], parts[-1].removesuffix('.git')


def _commit_sha(document, field):
    value = str(document.get(field) or '') if isinstance(document, dict) else ''
    if not re.fullmatch(r'[0-9a-fA-F]{40,64}', value):
        raise TestCaseSyncError('同步源未返回有效的不可变 commit SHA')
    return value


def _github_files(profile: TestCaseSyncProfile, token: str, *, deadline=None, budget=None):
    _check_deadline(deadline)
    budget = budget or _SyncReadBudget()
    repository_url = _validate_provider_url(profile.repository_url, 'github', allow_query=False)
    owner, repository = _repository_parts(repository_url)
    branch = quote(profile.branch or 'main', safe='')
    api_root = f'https://api.github.com/repos/{quote(owner)}/{quote(repository)}'
    commit = _commit_sha(_request_json(
        _validate_provider_url(f'{api_root}/commits/{branch}', 'github'), token=token,
        deadline=deadline, budget=budget,
    ), 'sha')
    tree = _request_json(
        _validate_provider_url(f'{api_root}/git/trees/{commit}?recursive=1', 'github'),
        token=token, deadline=deadline, budget=budget,
    )
    if not isinstance(tree, dict) or tree.get('truncated'):
        raise TestCaseSyncError('GitHub tree 结果为空或被截断')
    prefix = (profile.path or '').strip('/ ')
    files = []
    for item in tree.get('tree') or []:
        path = str(item.get('path') or '')
        if item.get('type') == 'blob' and (not prefix or path == prefix or path.startswith(prefix + '/')) and path.lower().endswith(('.yml', '.yaml')):
            raw_url = _validate_provider_url(
                f'https://raw.githubusercontent.com/{owner}/{repository}/{commit}/{quote(path, safe="/")}',
                'github',
            )
            files.append((path, str(item.get('sha') or ''), raw_url))
    return commit, files


def _gitlab_files(profile: TestCaseSyncProfile, token: str, *, deadline=None, budget=None):
    _check_deadline(deadline)
    budget = budget or _SyncReadBudget()
    repository_url = _validate_provider_url(profile.repository_url, 'gitlab', allow_query=False)
    parsed_repository = urlparse(repository_url)
    _repository_parts(repository_url)
    if profile.api_base_url:
        base = _validate_provider_url(profile.api_base_url.rstrip('/'), 'gitlab', allow_query=False)
    else:
        base = _validate_provider_url(
            f'{parsed_repository.scheme}://{parsed_repository.netloc}/api/v4',
            'gitlab', allow_query=False,
        )
    # Preserve GitLab subgroup namespaces as part of the repository identity.
    project = quote(parsed_repository.path.strip('/').removesuffix('.git'), safe='')
    repository_api = f'{base}/projects/{project}/repository'
    branch = quote(profile.branch or 'main', safe='')
    commit = _commit_sha(_request_json(
        _validate_provider_url(f'{repository_api}/commits/{branch}', 'gitlab'), token=token,
        deadline=deadline, budget=budget,
    ), 'id')
    prefix = (profile.path or '').strip('/ ')
    files = []
    seen_paths = set()
    for page in range(1, MAX_TREE_PAGES + 1):
        _check_deadline(deadline)
        tree_url = _validate_provider_url(
            f'{repository_api}/tree?ref={commit}&recursive=true&per_page=100&page={page}', 'gitlab',
        )
        tree = _request_json(tree_url, token=token, deadline=deadline, budget=budget)
        if not isinstance(tree, list) or len(tree) > 100:
            raise TestCaseSyncError('GitLab tree 返回了无效分页')
        for item in tree:
            path = str(item.get('path') or '')
            if path in seen_paths:
                raise TestCaseSyncError('GitLab tree 分页重复，未完成同步')
            seen_paths.add(path)
            if item.get('type') == 'blob' and (not prefix or path == prefix or path.startswith(prefix + '/')) and path.lower().endswith(('.yml', '.yaml')):
                raw_url = _validate_provider_url(
                    f'{repository_api}/files/{quote(path, safe="")}/raw?ref={commit}', 'gitlab',
                )
                files.append((path, str(item.get('id') or ''), raw_url))
                if len(files) > MAX_FILES:
                    raise TestCaseSyncError(f'同步文件数不能超过 {MAX_FILES}')
        if len(tree) < 100:
            return commit, files
    raise TestCaseSyncError(f'GitLab tree 超过 {MAX_TREE_PAGES} 页，未完成同步')


def _load_files(profile: TestCaseSyncProfile, token: str, *, deadline=None):
    budget = _SyncReadBudget()
    if profile.provider == 'github':
        commit, files = _github_files(profile, token, deadline=deadline, budget=budget)
    else:
        commit, files = _gitlab_files(profile, token, deadline=deadline, budget=budget)
    if len(files) > MAX_FILES:
        raise TestCaseSyncError(f'同步文件数不能超过 {MAX_FILES}')
    loaded = []
    for path, source_sha, raw_url in files:
        _check_deadline(deadline)
        raw = _read_response_bytes(
            _validate_provider_url(raw_url, profile.provider),
            headers={
                'Accept': 'text/plain',
                'User-Agent': 'TestHub-TestsAsCode/1.0',
                **({'PRIVATE-TOKEN': token} if token else {}),
            },
            deadline=deadline, budget=budget, max_bytes=MAX_FILE_BYTES,
            size_error=f'同步文件过大: {path}', http_error='读取同步文件失败',
        )
        loaded.append((path, source_sha, raw))
    _check_deadline(deadline)
    return commit, loaded


def _yaml_string_expanded_size(value, deadline=None):
    json_size = repr_size = 2
    quote = '"' if "'" in value and '"' not in value else "'"
    short_escapes = {'"', '\\', '\b', '\t', '\n', '\f', '\r'}
    for index, character in enumerate(value):
        if index and index % 4096 == 0:
            _check_deadline(deadline)
        codepoint = ord(character)
        if character in short_escapes:
            json_size += 2
        elif codepoint < 0x20 or 0xD800 <= codepoint <= 0xDFFF:
            json_size += 6
        elif codepoint <= 0x7F:
            json_size += 1
        elif codepoint <= 0x7FF:
            json_size += 2
        elif codepoint <= 0xFFFF:
            json_size += 3
        else:
            json_size += 4

        if character in {'\\', quote, '\t', '\n', '\r'}:
            repr_size += 2
        elif not character.isprintable():
            repr_size += 4 if codepoint <= 0xFF else 6 if codepoint <= 0xFFFF else 10
        elif codepoint <= 0x7F:
            repr_size += 1
        elif codepoint <= 0x7FF:
            repr_size += 2
        elif codepoint <= 0xFFFF:
            repr_size += 3
        else:
            repr_size += 4
        if max(json_size, repr_size) > MAX_YAML_EXPANDED_BYTES:
            return MAX_YAML_EXPANDED_BYTES + 1
    return max(json_size, repr_size)


def _integer_json_size(value):
    bits = abs(value).bit_length()
    digits = max(1, (bits * 30103) // 100000 + 1)
    return digits + (1 if value < 0 else 0)


def _yaml_scalar_expanded_size(value, *, is_key=False, scalar_sizes=None, deadline=None):
    if isinstance(value, str):
        cache_key = id(value)
        if cache_key not in scalar_sizes:
            scalar_sizes[cache_key] = _yaml_string_expanded_size(value, deadline)
        return scalar_sizes[cache_key]
    if isinstance(value, bytes):
        cache_key = id(value)
        if cache_key not in scalar_sizes:
            scalar_sizes[cache_key] = 4 * len(value) + 3
        return scalar_sizes[cache_key]
    if value is None:
        return 6 if is_key else 4
    if isinstance(value, bool):
        return (6 if value else 7) if is_key else (4 if value else 5)
    if isinstance(value, int):
        size = _integer_json_size(value)
        return size + 2 if is_key else size
    if isinstance(value, float):
        return 34 if is_key else 32
    return 64


def _validate_yaml_budget(document, deadline=None):
    """Bound semantic expansion before JSON serialization duplicates YAML aliases."""
    remaining = MAX_YAML_EXPANDED_NODES
    expanded_bytes = 0
    scalar_sizes = {}

    def consume(size):
        nonlocal expanded_bytes
        expanded_bytes += size
        if expanded_bytes > MAX_YAML_EXPANDED_BYTES:
            raise TestCaseSyncError('YAML 展开数据量超过限制')

    def visit(value, depth, *, is_key=False):
        nonlocal remaining
        _check_deadline(deadline)
        if depth > MAX_YAML_DEPTH:
            raise TestCaseSyncError('YAML 嵌套深度超过限制')
        remaining -= 1
        if remaining < 0:
            raise TestCaseSyncError('YAML 展开节点数超过限制')
        if isinstance(value, dict):
            consume(2)
            for index, (key, item) in enumerate(value.items()):
                if index:
                    consume(2)
                visit(key, depth + 1, is_key=True)
                consume(2)
                visit(item, depth + 1)
        elif isinstance(value, (list, tuple, set, frozenset)):
            if isinstance(value, (set, frozenset)) and not value:
                consume(11 if isinstance(value, frozenset) else 5)
            else:
                consume(2 + (11 if isinstance(value, frozenset) else 0))
                if isinstance(value, tuple) and len(value) == 1:
                    consume(1)
            for index, item in enumerate(value):
                if index:
                    consume(2)
                visit(item, depth + 1)
        else:
            consume(_yaml_scalar_expanded_size(
                value, is_key=is_key, scalar_sizes=scalar_sizes, deadline=deadline,
            ))

    visit(document, 0)


def _iter_json_string(value):
    yield '"'
    for character in value:
        if character == '"':
            yield '\\"'
        elif character == '\\':
            yield '\\\\'
        elif character == '\b':
            yield '\\b'
        elif character == '\t':
            yield '\\t'
        elif character == '\n':
            yield '\\n'
        elif character == '\f':
            yield '\\f'
        elif character == '\r':
            yield '\\r'
        elif ord(character) < 0x20:
            yield f'\\u{ord(character):04x}'
        else:
            yield character
    yield '"'


def _json_key(value):
    if isinstance(value, str):
        return value
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(
        f'keys must be str, int, float, bool or None, not {value.__class__.__name__}'
    )


def _iter_json_chunks(value):
    if value is None:
        yield 'null'
    elif value is True:
        yield 'true'
    elif value is False:
        yield 'false'
    elif isinstance(value, str):
        yield from _iter_json_string(value)
    elif isinstance(value, int):
        yield str(value)
    elif isinstance(value, float):
        yield json.dumps(value, ensure_ascii=False)
    elif isinstance(value, (list, tuple)):
        yield '['
        for index, item in enumerate(value):
            if index:
                yield ', '
            yield from _iter_json_chunks(item)
        yield ']'
    elif isinstance(value, dict):
        yield '{'
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield ', '
            yield from _iter_json_string(_json_key(key))
            yield ': '
            yield from _iter_json_chunks(item)
        yield '}'
    else:
        raise TypeError(f'Object of type {value.__class__.__name__} is not JSON serializable')


def _json_prefix(value, limit):
    parts = []
    remaining = limit
    for chunk in _iter_json_chunks(value):
        if remaining:
            part = chunk[:remaining]
            if part:
                parts.append(part)
                remaining -= len(part)
    return ''.join(parts)


def _testcase_values(document: dict, path: str) -> dict:
    external_ref = str(document.get('id') or document.get('external_ref') or path.rsplit('.', 1)[0]).strip()
    title = str(document.get('title') or '').strip()
    if not external_ref or not title:
        raise TestCaseSyncError(f'{path}: id/external_ref 和 title 均为必填项')
    steps = document.get('steps', '')
    if isinstance(steps, (list, dict)):
        steps = _json_prefix(steps, MAX_STEPS_JSON_CHARS)
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


def _lock_current_sync_profile(profile, actor, expected_task_id=None):
    current = TestCaseSyncProfile.objects.select_for_update().select_related('project').filter(
        pk=profile.pk,
    ).first()
    if current is None:
        raise _SyncProfileFenceError('同步配置已不存在')
    if expected_task_id is not None and (
        current.sync_task_id != expected_task_id or current.sync_status != 'running'
    ):
        raise _SyncProfileFenceError('同步任务已被替换')
    if not user_can_write_project(actor, current.project):
        raise _SyncProfileFenceError('项目写入权限已撤销')
    return current


def sync_profile(profile: TestCaseSyncProfile, *, actor=None, expected_task_id=None) -> SyncSummary:
    if not profile.enabled:
        raise TestCaseSyncError('同步配置已禁用')
    actor = actor or profile.created_by
    if not user_can_write_project(actor, profile.project):
        raise TestCaseSyncError('项目写入权限已撤销')

    deadline = _sync_clock() + MAX_SYNC_SECONDS
    token = _token(profile)
    commit, files = _load_files(profile, token, deadline=deadline)
    created = updated = skipped = conflicts = 0
    warnings: list[str] = []
    seen_refs: set[str] = set()
    for path, _source_sha, raw in files:
        try:
            _check_deadline(deadline)
            document = _load_sync_yaml(raw.decode('utf-8'), deadline)
            _check_deadline(deadline)
            if not isinstance(document, dict):
                raise TestCaseSyncError('YAML 根节点必须是对象')
            _validate_yaml_budget(document, deadline)
            values = _testcase_values(document, path)
            ref = values['external_ref']
            if ref in seen_refs:
                warnings.append(f'{path}: external_ref 重复')
                skipped += 1
                continue
            seen_refs.add(ref)
            source_hash = hashlib.sha256(raw).hexdigest()
            with transaction.atomic():
                current_profile = _lock_current_sync_profile(
                    profile, actor, expected_task_id=expected_task_id,
                )
                _check_deadline(deadline)
                binding = TestCaseSourceBinding.objects.select_for_update().filter(
                    profile=current_profile, external_ref=ref,
                ).select_related('testcase').first()
                if binding:
                    testcase = TestCase.objects.select_for_update().get(pk=binding.testcase_id)
                    if testcase.project_id != current_profile.project_id:
                        warnings.append(f'{path}: 绑定用例不属于同步项目，跳过')
                        skipped += 1
                        continue
                else:
                    testcase = TestCase.objects.select_for_update().filter(
                        project=current_profile.project, external_ref=ref,
                    ).first()

                if (
                    testcase is not None and binding is not None
                    and testcase.updated_at > binding.last_synced_at
                    and binding.source_hash != source_hash
                ):
                    warnings.append(f'{path}: 检测到本地修改，跳过覆盖')
                    conflicts += 1
                    continue

                if testcase is None:
                    testcase = TestCase.objects.create(
                        project=current_profile.project, author=actor, **values,
                    )
                    record_test_case_revision(testcase, actor)
                    created += 1
                else:
                    for key, value in values.items():
                        setattr(testcase, key, value)
                    testcase.save(update_fields=[*values.keys(), 'updated_at'])
                    record_test_case_revision(testcase, actor)
                    updated += 1
                TestCaseSourceBinding.objects.update_or_create(
                    testcase=testcase,
                    defaults={
                        'profile': current_profile,
                        'external_ref': ref,
                        'source_path': path,
                        'source_commit': commit,
                        'source_hash': source_hash,
                        'last_synced_at': timezone.now(),
                    },
                )
        except _SyncProfileFenceError:
            raise
        except (UnicodeDecodeError, yaml.YAMLError, TestCaseSyncError, TypeError, ValueError) as exc:
            warnings.append(f'{path}: {exc}')
            skipped += 1

    _check_deadline(deadline)
    with transaction.atomic():
        current_profile = _lock_current_sync_profile(
            profile, actor, expected_task_id=expected_task_id,
        )
        current_profile.last_commit_sha = commit
        current_profile.last_sync_at = timezone.now()
        current_profile.save(update_fields=['last_commit_sha', 'last_sync_at', 'updated_at'])
    return SyncSummary(
        profile.pk, profile.provider, commit, len(files), created, updated,
        skipped, conflicts, warnings,
    )
