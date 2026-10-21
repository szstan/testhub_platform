import pytest

from apps.executions.datasets import DatasetError, cleanup_fixture, expand_dataset, redact_dataset, secret_reference_name
from apps.executions.models import ExecutionDataset, ExecutionFixture
from apps.executions.plugins import PluginError, PluginManifest, PluginRegistry
from apps.projects.models import Project
from apps.users.models import User


def test_matrix_expansion_is_deterministic():
    variants = expand_dataset({'matrix': {'browser': ['chrome', 'firefox'], 'locale': ['zh', 'en']}})
    assert variants == [
        {'browser': 'chrome', 'locale': 'zh'},
        {'browser': 'chrome', 'locale': 'en'},
        {'browser': 'firefox', 'locale': 'zh'},
        {'browser': 'firefox', 'locale': 'en'},
    ]


def test_dataset_rejects_invalid_shape_and_redacts_secrets():
    with pytest.raises(DatasetError):
        expand_dataset({'matrix': {'browser': 'chrome'}})
    assert redact_dataset({'password': 'secret', 'nested': {'token': 'hidden', 'value': 1}})['password'] == '[REDACTED]'
    assert secret_reference_name('TESTHUB_SECRET_1') == 'TESTHUB_SECRET_1'
    with pytest.raises(DatasetError):
        secret_reference_name('secret-value')


@pytest.mark.django_db
def test_fixture_cleanup_empty_is_safe_and_explicit_refs_are_cleaned():
    user = User.objects.create_user(username='fixture-owner', password='pass')
    project = Project.objects.create(name='Fixture Project', owner=user)
    skipped = ExecutionFixture.objects.create(project=project, run_id='empty', references={})
    assert cleanup_fixture(skipped)['status'] == 'skipped'
    assert skipped.status == 'skipped'

    fixture = ExecutionFixture.objects.create(
        project=project,
        run_id='explicit',
        references={'users': ['u-1', 'u-2']},
    )
    calls = []
    result = cleanup_fixture(fixture, cleaner=lambda kind, value: calls.append((kind, value)))
    assert result == {'status': 'cleaned', 'count': 2}
    assert calls == [('users', 'u-1'), ('users', 'u-2')]


def test_plugin_registry_versions_and_permissions():
    registry = PluginRegistry()
    registry.register(PluginManifest('demo', '1.0', 'keyword', permissions=('read',)))
    assert registry.require('demo', permissions=('read',)).manifest.version == '1.0'
    with pytest.raises(PluginError):
        registry.require('demo', permissions=('write',))
    registry.disable('demo', '1.0')
    with pytest.raises(PluginError):
        registry.require('demo', '1.0')
    with pytest.raises(PluginError):
        registry.register(PluginManifest('demo', '1.0', 'keyword'))
