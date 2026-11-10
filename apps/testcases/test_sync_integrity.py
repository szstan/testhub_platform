import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

import pytest

from apps.testcases.sync import TestCaseSyncError, _gitlab_files, sync_profile


COMMIT = 'a' * 40


def profile_fixture(**overrides):
    return SimpleNamespace(**{
        'repository_url': 'https://gitlab.com/acme/subgroup/shop.git',
        'api_base_url': '', 'provider': 'gitlab', 'branch': 'feature/cases',
        'path': 'tests', **overrides,
    })


def response(data=None, *, status=200, content=b''):
    result = Mock(status_code=status, content=content)
    result.json.return_value = data
    return result


def test_gitlab_paginates_and_pins_all_reads_to_resolved_commit():
    rows = [{'type': 'blob', 'path': f'tests/case-{index}.yaml', 'id': f'blob-{index}'} for index in range(100)]
    pages = [response({'id': COMMIT}), response(rows), response([
        {'type': 'blob', 'path': 'tests/last.yaml', 'id': 'last-blob'},
    ])]
    with patch('apps.testcases.sync.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.testcases.sync.guarded_get', side_effect=pages,
    ) as get:
        commit, files = _gitlab_files(profile_fixture(), '')
    urls = [call.args[0] for call in get.call_args_list]
    assert '/projects/acme%2Fsubgroup%2Fshop/' in urls[0]
    assert urls[0].endswith('/commits/feature%2Fcases')
    assert commit == COMMIT
    assert len(files) == 101
    assert files[-1][0] == 'tests/last.yaml'
    assert [parse_qs(urlparse(url).query)['page'] for url in urls[1:]] == [['1'], ['2']]
    assert all(parse_qs(urlparse(url).query)['ref'] == [COMMIT] for url in urls[1:])
    assert all(parse_qs(urlparse(raw_url).query)['ref'] == [COMMIT] for _path, _blob, raw_url in files)


@pytest.mark.parametrize('failure', ['page-error', 'repeated-page', 'page-bound', 'file-bound', 'malformed'])
def test_gitlab_cannot_silently_succeed_with_incomplete_tree(failure):
    rows = [{'type': 'blob', 'path': f'tests/{index}.yaml', 'id': f'blob-{index}'} for index in range(100)]
    pages = [response({'id': COMMIT}), response(rows)]
    if failure == 'page-error':
        pages.append(response(status=503))
    elif failure == 'repeated-page':
        pages.append(response(rows))
    elif failure == 'malformed':
        pages[1] = response({'error': 'not a tree list'})
    with patch('apps.testcases.sync.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.testcases.sync.guarded_get', side_effect=pages,
    ), patch('apps.testcases.sync.MAX_TREE_PAGES', 1 if failure == 'page-bound' else 100), patch(
        'apps.testcases.sync.MAX_FILES', 2 if failure == 'file-bound' else 1000,
    ):
        with pytest.raises(TestCaseSyncError):
            _gitlab_files(profile_fixture(), '')


def test_gitlab_rejects_a_blob_or_missing_id_instead_of_claiming_commit():
    with patch('apps.testcases.sync.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.testcases.sync.guarded_get', return_value=response({'id': 'blob-not-commit'}),
    ):
        with pytest.raises(TestCaseSyncError, match='commit SHA'):
            _gitlab_files(profile_fixture(), '')


@pytest.mark.django_db
def test_gitlab_binding_and_profile_store_commit_instead_of_blob():
    from apps.projects.models import Project
    from apps.testcases.models import TestCaseSourceBinding, TestCaseSyncProfile
    from apps.users.models import User

    user = User.objects.create_user(username='gitlab-immutable-owner')
    project = Project.objects.create(name='Immutable repository', owner=user)
    profile = TestCaseSyncProfile.objects.create(
        project=project, name='Immutable GitLab fixture', provider='gitlab',
        repository_url='https://gitlab.com/acme/shop', branch='main', created_by=user,
    )
    with patch('apps.testcases.sync.validate_outbound_url', side_effect=lambda url, **kwargs: url), patch(
        'apps.testcases.sync.guarded_get', side_effect=[
            response({'id': COMMIT}),
            response([{'type': 'blob', 'path': 'tests/last.yaml', 'id': 'b' * 40}]),
            response(content=b'id: imported.last\ntitle: Last testcase\nexpected_result: pass\n'),
        ],
    ) as get:
        summary = sync_profile(profile)
    profile.refresh_from_db()
    binding = TestCaseSourceBinding.objects.get(profile=profile)
    assert summary.created == 1
    assert binding.source_commit == profile.last_commit_sha == summary.commit == COMMIT
    assert get.call_args_list[-1].args[0].endswith(f'raw?ref={COMMIT}')


def test_existing_multi_case_database_upgrades_without_duplicate_external_refs(tmp_path):
    # Run the real migration graph on an isolated database, independent of the
    # normal unit-test syncdb setting and any developer database.
    script = textwrap.dedent('''
        import os, sys
        os.environ['DJANGO_SETTINGS_MODULE'] = 'backend.test_settings'
        from django.conf import settings
        settings.MIGRATION_MODULES = {}
        settings.DATABASES['default']['NAME'] = sys.argv[1]
        import django
        django.setup()
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from importlib import import_module
        executor = MigrationExecutor(connection)
        old = [('testcases', '0002_initial')]
        executor.migrate(old)
        old_apps = executor.loader.project_state(old).apps
        User = old_apps.get_model('users', 'User')
        Project = old_apps.get_model('projects', 'Project')
        Case = old_apps.get_model('testcases', 'TestCase')
        owner = User.objects.create(username='isolated-migration-owner')
        project = Project.objects.create(name='Existing data', owner_id=owner.pk)
        for index in range(4):
            Case.objects.create(project_id=project.pk, author_id=owner.pk, title=f'Existing {index}', expected_result='pass')
        target = [('testcases', '0003_testcase_external_ref_and_more')]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        new_apps = executor.loader.project_state(target).apps
        NewCase = new_apps.get_model('testcases', 'TestCase')
        refs = list(NewCase.objects.values_list('external_ref', flat=True))
        assert len(refs) == len(set(refs)) == 4 and all(refs), refs
        original = NewCase.objects.order_by('pk').first()
        original.external_ref = 'preserved-custom-ref'
        original.save(update_fields=['external_ref'])
        other = NewCase.objects.exclude(pk=original.pk).first()
        NewCase.objects.filter(pk=other.pk).update(external_ref='')
        migration = import_module('apps.testcases.migrations.0003_testcase_external_ref_and_more')
        with connection.schema_editor() as editor:
            migration.fill_external_refs(new_apps, editor)
        original.refresh_from_db()
        assert original.external_ref == 'preserved-custom-ref'
        assert not NewCase.objects.filter(external_ref='').exists()
        field = NewCase._meta.get_field('external_ref')
        assert field.null is False
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, NewCase._meta.db_table)
        assert constraints['testcase_project_external_ref_uniq']['unique'] is True
        print('isolated migration upgrade: 4 existing cases, unique refs, preserved custom ref, final schema OK')
    ''')
    result = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path / 'testcase-upgrade.sqlite3')],
        cwd=Path(__file__).resolve().parents[2], env=os.environ.copy(),
        text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'final schema OK' in result.stdout
