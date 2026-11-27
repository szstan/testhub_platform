import logging

import pytest
from django.test import override_settings

from apps.app_automation.models import AppDataSet, AppProject, AppTestCase, AppTestExecution
from apps.app_automation.scene_data_runtime import SceneDataError, execution_variables
from apps.users.models import User


def secret_config(project_aliases=(), global_aliases=None):
    return {
        'projects': {str(project.pk): aliases for project, aliases in project_aliases},
        'global': global_aliases or {},
    }


@pytest.fixture
def data_execution(db):
    owner = User.objects.create_user(username='data-runtime-owner')
    member = User.objects.create_user(username='data-runtime-member')
    project = AppProject.objects.create(name='runtime data', owner=owner)
    project.members.add(member)
    case = AppTestCase.objects.create(
        name='runtime case', project=project, created_by=owner,
        variables=[{'name': 'username', 'value': 'original', 'scope': 'local'},
                   {'name': 'api_key', 'value': 'secret://api', 'scope': 'global'}],
    )
    dataset = AppDataSet.objects.create(
        project=project, name='accounts', version=1, columns=['username', 'password'],
        rows=[{'username': 'first', 'password': 'secret://password'},
              {'username': 'second', 'password': 'secret://password'}],
        sensitive_columns=['password'], created_by=owner,
    )
    execution = AppTestExecution.objects.create(
        test_case=case, user=member, dataset=dataset, dataset_row_index=1,
    )
    return owner, member, project, case, dataset, execution


@pytest.mark.django_db
def test_bind_selected_row_and_resolve_allowlisted_secrets_only_in_memory(data_execution, caplog):
    _, _, project, case, dataset, execution = data_execution
    caplog.set_level(logging.DEBUG)
    config = secret_config([(project, {
        'api': 'synthetic-api-value', 'password': 'synthetic-password-value',
    })])
    with override_settings(APP_SCENE_SECRET_ALIASES=config):
        bound = execution_variables(execution, case)
    values = {item['name']: item for item in bound}
    assert values['username']['value'] == 'second'
    assert values['api_key']['value'] == 'synthetic-api-value'
    assert values['api_key']['sensitive'] is True
    assert values['password']['value'] == 'synthetic-password-value'
    assert values['password']['sensitive'] is True
    dataset.refresh_from_db()
    case.refresh_from_db()
    assert dataset.rows[1]['password'] == 'secret://password'
    assert case.variables[1]['value'] == 'secret://api'
    assert 'synthetic-api-value' not in caplog.text
    assert 'synthetic-password-value' not in caplog.text


@pytest.mark.django_db
def test_unknown_alias_and_out_of_range_row_fail_without_value_disclosure(data_execution):
    _, _, project, case, _, execution = data_execution
    config = secret_config([(project, {})])
    with override_settings(APP_SCENE_SECRET_ALIASES=config):
        with pytest.raises(SceneDataError) as error:
            execution_variables(execution, case)
        assert 'password' not in str(error.value)
        execution.dataset_row_index = 99
        with pytest.raises(SceneDataError, match='数据集行'):
            execution_variables(execution, case)
        execution.dataset_row_index = None
        with pytest.raises(SceneDataError, match='行号'):
            execution_variables(execution, case)


@pytest.mark.django_db
def test_membership_and_project_are_rechecked_at_execution(data_execution):
    owner, member, project, case, dataset, execution = data_execution
    config = secret_config([(project, {'api': 'fixture', 'password': 'fixture'})])
    with override_settings(APP_SCENE_SECRET_ALIASES=config):
        project.members.remove(member)
        with pytest.raises(SceneDataError, match='访问权限'):
            execution_variables(execution, case)
        project.members.add(member)
        other = AppProject.objects.create(name='other runtime', owner=owner)
        dataset.project = other
        dataset.save(update_fields=['project'])
        execution.dataset = dataset
        with pytest.raises(SceneDataError, match='项目不匹配'):
            execution_variables(execution, case)


@pytest.mark.django_db
def test_case_aliases_work_without_a_dataset(data_execution):
    _, _, project, case, _, execution = data_execution
    execution.dataset = None
    execution.dataset_row_index = None
    config = secret_config([(project, {'api': 'synthetic-api-value'})])
    with override_settings(APP_SCENE_SECRET_ALIASES=config):
        values = {item['name']: item for item in execution_variables(execution, case)}
    assert values['username']['value'] == 'original'
    assert values['api_key']['sensitive'] is True


@pytest.mark.django_db
def test_project_aliases_are_isolated_and_override_explicit_global_aliases(data_execution):
    owner, member, project_a, case_a, _, execution_a = data_execution
    project_b = AppProject.objects.create(name='runtime project b', owner=owner)
    project_b.members.add(member)
    case_a.variables = [
        {'name': 'private', 'value': 'secret://private'},
        {'name': 'shared', 'value': 'secret://shared'},
        {'name': 'overridden', 'value': 'secret://overridden'},
    ]
    execution_a.dataset = None
    execution_a.dataset_row_index = None
    case_b = AppTestCase.objects.create(
        name='project b case', project=project_b, created_by=owner,
        variables=[
            {'name': 'private', 'value': 'secret://private'},
            {'name': 'shared', 'value': 'secret://shared'},
            {'name': 'overridden', 'value': 'secret://overridden'},
        ],
    )
    execution_b = AppTestExecution.objects.create(test_case=case_b, user=member)
    config = secret_config(
        [(project_a, {'private': 'synthetic-project-a-value', 'overridden': 'synthetic-project-a-override'}),
         (project_b, {'private': 'synthetic-project-b-value', 'project_b_only': 'synthetic-project-b-only'})],
        {'shared': 'synthetic-global-shared', 'overridden': 'synthetic-global-overridden'},
    )
    with override_settings(APP_SCENE_SECRET_ALIASES=config):
        values_a = {item['name']: item for item in execution_variables(execution_a, case_a)}
        values_b = {item['name']: item for item in execution_variables(execution_b, case_b)}
        case_a.variables = [{'name': 'project_b_only', 'value': 'secret://project_b_only'}]
        with pytest.raises(SceneDataError) as error:
            execution_variables(execution_a, case_a)
    assert values_a['private']['value'] == 'synthetic-project-a-value'
    assert values_a['shared']['value'] == 'synthetic-global-shared'
    assert values_a['overridden']['value'] == 'synthetic-project-a-override'
    assert values_b['private']['value'] == 'synthetic-project-b-value'
    assert values_b['shared']['value'] == 'synthetic-global-shared'
    assert values_b['overridden']['value'] == 'synthetic-global-overridden'
    assert 'synthetic-project-b-only' not in str(error.value)
    assert 'project_b_only' not in str(error.value)


@pytest.mark.django_db
def test_alias_resolution_rejects_unauthorized_and_removed_members(data_execution):
    owner, member, project, case, _, execution = data_execution
    execution.dataset = None
    execution.dataset_row_index = None
    case.variables = [{'name': 'api_key', 'value': 'secret://api'}]
    stranger = User.objects.create_user(username='data-runtime-stranger')
    stranger_execution = AppTestExecution.objects.create(test_case=case, user=stranger)
    config = secret_config(global_aliases={'api': 'synthetic-private-value'})
    with override_settings(APP_SCENE_SECRET_ALIASES=config):
        with pytest.raises(SceneDataError) as error:
            execution_variables(stranger_execution, case)
        assert 'synthetic-private-value' not in str(error.value)
        project.members.remove(member)
        with pytest.raises(SceneDataError) as error:
            execution_variables(execution, case)
    assert 'synthetic-private-value' not in str(error.value)


@pytest.mark.django_db
def test_admin_can_resolve_explicitly_global_alias(data_execution):
    _, _, project, case, _, execution = data_execution
    execution.dataset = None
    execution.dataset_row_index = None
    case.variables = [{'name': 'api_key', 'value': 'secret://shared'}]
    execution.user = User.objects.create_user(username='data-runtime-admin', is_staff=True)
    config = secret_config(global_aliases={'shared': 'synthetic-global-admin-value'})
    with override_settings(APP_SCENE_SECRET_ALIASES=config):
        bound = execution_variables(execution, case)
    assert bound[0]['value'] == 'synthetic-global-admin-value'


@pytest.mark.django_db
@pytest.mark.parametrize('malformed', [
    {'api': 'synthetic-private-value'},
    {'projects': {'not-a-project': {'api': 'synthetic-private-value'}}},
    {'projects': {'1': {'bad/alias': 'synthetic-private-value'}}},
    {'global': {'api': ['synthetic-private-value']}},
    {'global': {'api': ''}},
    {'unknown': {'api': 'synthetic-private-value'}},
])
def test_malformed_secret_config_is_rejected_generically(data_execution, malformed):
    _, _, _, case, _, execution = data_execution
    with override_settings(APP_SCENE_SECRET_ALIASES=malformed):
        with pytest.raises(SceneDataError) as error:
            execution_variables(execution, case)
    assert 'synthetic-private-value' not in str(error.value)
