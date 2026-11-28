import pytest
from unittest.mock import patch

from apps.projects.models import Project
from apps.testcases import sync
from apps.testcases.models import TestCase, TestCaseSyncProfile
from apps.users.models import User


@pytest.fixture
def constructor_budget_profile(db):
    owner = User.objects.create_user(username='yaml-constructor-budget-owner')
    project = Project.objects.create(name='YAML constructor budget', owner=owner)
    return TestCaseSyncProfile.objects.create(
        project=project, name='YAML constructor source', provider='github',
        repository_url='https://github.com/example/cases', created_by=owner,
    )


def test_merge_expansion_budget_rejects_duplicate_key_amplification(constructor_budget_profile):
    lines = ['base: &base {same: value}']
    previous = 'base'
    for level in range(1, 9):
        name = f'merge{level}'
        lines.append(f'{name}: &{name} {{<<: [*{previous}, *{previous}]}}')
        previous = name
    lines.extend([
        '<<: *merge8',
        'id: merged.case',
        'title: Merged case',
    ])
    body = ('\n'.join(lines) + '\n').encode()

    with (
        patch.object(sync, '_load_files', return_value=(
            'a' * 40, [('merge.yaml', 'blob', body)],
        )),
        patch.object(sync, 'MAX_YAML_CONSTRUCTION_WORK', 80),
        patch.object(TestCase.objects, 'create', wraps=TestCase.objects.create) as create,
    ):
        summary = sync.sync_profile(constructor_budget_profile)

    assert summary.created == 0
    assert summary.skipped == 1
    assert 'YAML' in summary.warnings[0]
    create.assert_not_called()
    assert not TestCase.objects.filter(project=constructor_budget_profile.project).exists()


def test_small_legitimate_yaml_merge_remains_supported(constructor_budget_profile):
    body = (
        b'defaults: &defaults {expected_result: passed, priority: high}\n'
        b'<<: *defaults\n'
        b'id: merged.valid\n'
        b'title: Bounded merge\n'
    )
    with patch.object(sync, '_load_files', return_value=(
        'a' * 40, [('merge.yaml', 'blob', body)],
    )):
        summary = sync.sync_profile(constructor_budget_profile)

    testcase = TestCase.objects.get(project=constructor_budget_profile.project)
    assert summary.created == 1
    assert testcase.expected_result == 'passed'
    assert testcase.priority == 'high'


def test_merge_budget_rejects_before_list_extension():
    body = 'common: &common {answer: 42}\ncase: {<<: *common}\n'
    with (
        patch.object(sync, 'MAX_YAML_CONSTRUCTION_WORK', 0),
        patch.object(
            sync._BudgetedSafeLoader, '_extend_merge_pairs',
            side_effect=AssertionError('merge extension ran before budget rejection'),
        ),
        pytest.raises(sync.TestCaseSyncError, match='YAML'),
    ):
        sync._load_sync_yaml(body)


def test_yaml_depth_is_checked_during_composition():
    with patch.object(sync, 'MAX_YAML_DEPTH', 1), pytest.raises(
        sync.TestCaseSyncError, match='YAML.*深度',
    ):
        sync._load_sync_yaml('outer: {middle: {inner: value}}')


def test_yaml_deadline_is_checked_during_composition():
    clock = iter([0, 0, 2])
    with patch.object(sync, '_sync_clock', side_effect=lambda: next(clock)), pytest.raises(
        sync.TestCaseSyncError, match='最大运行时长',
    ):
        sync._load_sync_yaml('outer: value', deadline=1)
