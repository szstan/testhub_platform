"""Check the API-case migration graph independently of test DB schema syncing."""
from django.apps import apps
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState
from django.test import override_settings


@override_settings(MIGRATION_MODULES={})
def test_api_case_migration_has_the_explicit_testcase_dependency():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    state = loader.project_state([('api_testing', '0005_api_test_cases')])
    # A fresh DB must be able to resolve the explicit generic TestCase link before
    # Django creates the API-case table. Loading all app leaves would hide this bug.
    assert ('testcases', 'testcase') in state.models
    assert state.apps.get_model('api_testing', 'ApiTestCase')._meta.get_field(
        'linked_testcase',
    ).remote_field.model == state.apps.get_model('testcases', 'TestCase')


@override_settings(MIGRATION_MODULES={})
def test_api_case_execution_heartbeat_migration_matches_the_model_contract():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    state = loader.project_state([('api_testing', '0006_api_case_execution_heartbeat')])
    execution = state.apps.get_model('api_testing', 'ApiCaseExecution')
    assert execution._meta.get_field('heartbeat_at').null is True
    assert any(index.fields == ['status', 'heartbeat_at'] for index in execution._meta.indexes)


@override_settings(MIGRATION_MODULES={})
def test_api_case_migration_state_matches_the_live_models():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    changes = MigrationAutodetector(
        loader.project_state(), ProjectState.from_apps(apps),
        questioner=NonInteractiveMigrationQuestioner(),
    ).changes(graph=loader.graph, trim_to_apps={'api_testing'})
    assert not changes.get('api_testing'), [
        operation.describe()
        for migration in changes.get('api_testing', [])
        for operation in migration.operations
    ]
