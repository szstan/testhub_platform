import json
from unittest.mock import patch

import pytest

from apps.projects.models import Project
from apps.testcases import sync
from apps.testcases.models import TestCase, TestCaseSyncProfile
from apps.users.models import User


@pytest.fixture
def yaml_profile(db):
    owner = User.objects.create_user(username='yaml-budget-owner')
    project = Project.objects.create(name='YAML budget project', owner=owner)
    return TestCaseSyncProfile.objects.create(
        project=project, name='YAML source', provider='github',
        repository_url='https://github.com/example/cases', created_by=owner,
    )


@pytest.mark.parametrize('body', [
    b'id: cycle\ntitle: Cycle\nsteps: &loop [*loop]\n',
    b'id: expanded\ntitle: Expanded\nleaf: &leaf [x,x,x,x,x]\n'
    b'group: &group [*leaf,*leaf,*leaf,*leaf,*leaf]\nsteps: [*group,*group,*group,*group,*group]\n',
])
def test_yaml_alias_expansion_is_bounded_before_case_serialization(yaml_profile, body):
    with (
        patch.object(sync, '_load_files', return_value=('a' * 40, [('case.yaml', 'blob', body)])),
        patch.object(sync, 'MAX_YAML_EXPANDED_NODES', 50),
        patch.object(sync, '_testcase_values') as values,
    ):
        summary = sync.sync_profile(yaml_profile)
    assert summary.skipped == 1
    assert summary.created == 0
    assert 'YAML' in summary.warnings[0]
    values.assert_not_called()
    assert not TestCase.objects.filter(project=yaml_profile.project).exists()


def test_compact_large_scalar_alias_expansion_is_rejected_before_serialization(yaml_profile):
    aliases = [b'*shared'] * 90
    body = (
        b'id: oversized\ntitle: Oversized\nshared: &shared "'
        + b'x' * (64 * 1024)
        + b'"\nsteps: [' + b','.join(aliases) + b']\n'
    )
    assert len(body) < sync.MAX_FILE_BYTES
    assert len(aliases) + 10 < sync.MAX_YAML_EXPANDED_NODES

    with (
        patch.object(sync, '_load_files', return_value=('a' * 40, [('case.yaml', 'blob', body)])),
        patch.object(sync.json, 'dumps', wraps=json.dumps) as dumps,
        patch.object(TestCase.objects, 'create', wraps=TestCase.objects.create) as testcase_create,
    ):
        summary = sync.sync_profile(yaml_profile)

    assert summary.skipped == 1
    assert 'YAML 展开数据量' in summary.warnings[0]
    dumps.assert_not_called()
    testcase_create.assert_not_called()
    assert not TestCase.objects.filter(project=yaml_profile.project).exists()


def test_large_steps_are_serialized_as_a_bounded_json_prefix():
    steps = ['"\\\\\\n' * 2000, {'unicode': '测试'}]
    expected = json.dumps(steps, ensure_ascii=False)[:sync.MAX_STEPS_JSON_CHARS]
    with patch.object(sync.json, 'dumps', side_effect=AssertionError('full JSON serialization')):
        values = sync._testcase_values({'id': 'large', 'title': 'Large', 'steps': steps}, 'large.yaml')

    assert values['steps'] == expected


def test_small_legitimate_yaml_aliases_remain_supported(yaml_profile):
    body = b'id: small\ntitle: Small alias\ncommon: &common [Click]\nsteps: [*common,*common]\nexpected: Done\n'
    with patch.object(sync, '_load_files', return_value=('a' * 40, [('case.yaml', 'blob', body)])):
        summary = sync.sync_profile(yaml_profile)
    assert summary.created == 1
    assert summary.skipped == 0
    assert TestCase.objects.get(project=yaml_profile.project).steps == '[["Click"], ["Click"]]'
