from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.projects.models import Project, ProjectMember
from apps.testcases.models import TestCase, TestCaseSyncProfile
from apps.testcases.sync import TestCaseSyncError, sync_profile
from apps.testcases.tasks import sync_testcase_profile
from apps.users.models import User


@pytest.fixture
def sync_fixture(db):
    owner = User.objects.create_user(username='sync-task-owner')
    actor = User.objects.create_user(username='sync-task-actor')
    project = Project.objects.create(name='Sync task project', owner=owner)
    ProjectMember.objects.create(project=project, user=actor, role='tester')
    profile = TestCaseSyncProfile.objects.create(
        project=project, name='repo', provider='github',
        repository_url='https://github.com/acme/cases', created_by=owner,
    )
    return owner, actor, project, profile


def summary_for(profile):
    return SimpleNamespace(
        profile_id=profile.pk, provider='github', commit='a' * 40,
        files=0, created=0, updated=0, skipped=0, conflicts=0, warnings=[],
    )


@pytest.mark.django_db
def test_worker_claim_is_queued_only_and_duplicate_delivery_does_not_run_twice(sync_fixture):
    _owner, actor, _project, profile = sync_fixture
    profile.sync_status = 'queued'
    profile.sync_task_id = 'claim-1'
    profile.save(update_fields=['sync_status', 'sync_task_id'])
    calls = []

    def sync_once(current_profile, *, actor, expected_task_id):
        calls.append((current_profile.pk, expected_task_id))
        sync_testcase_profile.apply(args=(profile.pk, actor.pk), task_id='claim-1')
        profile.refresh_from_db()
        assert profile.sync_status == 'running'
        return summary_for(profile)

    with patch('apps.testcases.sync.sync_profile', side_effect=sync_once):
        sync_testcase_profile.apply(args=(profile.pk, actor.pk), task_id='claim-1')
        sync_testcase_profile.apply(args=(profile.pk, actor.pk), task_id='claim-1')

    profile.refresh_from_db()
    assert calls == [(profile.pk, 'claim-1')]
    assert profile.sync_status == 'completed'
    assert profile.sync_task_id == 'claim-1'


@pytest.mark.django_db
@pytest.mark.parametrize('raises', [False, True])
def test_worker_terminal_write_cannot_overwrite_a_newer_claim(sync_fixture, raises):
    _owner, actor, _project, profile = sync_fixture
    profile.sync_status = 'queued'
    profile.sync_task_id = 'old-claim'
    profile.save(update_fields=['sync_status', 'sync_task_id'])

    def replace_claim(*_args, **_kwargs):
        TestCaseSyncProfile.objects.filter(pk=profile.pk).update(
            sync_status='queued', sync_task_id='new-claim',
            sync_result={'newer': True}, sync_error='',
        )
        if raises:
            raise TestCaseSyncError('old worker failed')
        return summary_for(profile)

    with patch('apps.testcases.sync.sync_profile', side_effect=replace_claim):
        if raises:
            with pytest.raises(TestCaseSyncError, match='old worker failed'):
                sync_testcase_profile.apply(
                    args=(profile.pk, actor.pk), task_id='old-claim', throw=True,
                )
        else:
            sync_testcase_profile.apply(args=(profile.pk, actor.pk), task_id='old-claim')

    profile.refresh_from_db()
    assert profile.sync_status == 'queued'
    assert profile.sync_task_id == 'new-claim'
    assert profile.sync_result == {'newer': True}
    assert profile.sync_error == ''


@pytest.mark.django_db
@pytest.mark.parametrize('fence_change', ['task', 'permission'])
def test_sync_profile_rechecks_worker_fence_before_case_persistence(sync_fixture, fence_change):
    _owner, actor, project, profile = sync_fixture
    profile.sync_status = 'running'
    profile.sync_task_id = 'active-claim'
    profile.save(update_fields=['sync_status', 'sync_task_id'])
    raw = b'id: imported.case\ntitle: Imported case\nexpected_result: ok\n'

    def load_files(*_args, **_kwargs):
        if fence_change == 'task':
            TestCaseSyncProfile.objects.filter(pk=profile.pk).update(
                sync_status='queued', sync_task_id='new-claim',
            )
        else:
            ProjectMember.objects.filter(project=project, user=actor).delete()
        return 'a' * 40, [('tests/case.yaml', 'blob', raw)]

    with patch('apps.testcases.sync._load_files', side_effect=load_files):
        with pytest.raises(TestCaseSyncError):
            sync_profile(profile, actor=actor, expected_task_id='active-claim')

    assert not TestCase.objects.filter(project=project, external_ref='imported.case').exists()
    if fence_change == 'task':
        profile.refresh_from_db()
        assert profile.sync_status == 'queued'
        assert profile.sync_task_id == 'new-claim'
