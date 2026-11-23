import copy

import pytest
from django.test import override_settings

from apps.app_automation.models import AppDataSet, AppProject, AppProjectComponent, AppTestCase, AppTestExecution
from apps.app_automation.scene_execution import ScenePreparationError, prepare_scene_execution
from apps.users.models import User


def secret_config(project, aliases=None, shared=None):
    return {
        'projects': {str(project.pk): aliases or {}},
        'global': shared or {},
    }


@pytest.fixture
def scene_execution(db):
    user = User.objects.create_user(username='scene-preparation-owner')
    project = AppProject.objects.create(name='scene preparation', owner=user)
    case = AppTestCase.objects.create(
        project=project, created_by=user, name='case',
        variables=[{'name': 'complex', 'scope': 'local', 'value': {'nested': [1, 2]}, 'metadata': {'legacy': True}},
                   {'name': 'username', 'value': 'old'}, {'name': 'username', 'value': 'older'},
                   {'name': 'password', 'value': 'secret://password'}],
        ui_flow=[{'type': 'input', 'config': {'value': '{{local.password}}'}}],
    )
    dataset = AppDataSet.objects.create(
        project=project, name='accounts', version=1, columns=['username'],
        rows=[{'username': 'selected'}], created_by=user,
    )
    execution = AppTestExecution.objects.create(test_case=case, user=user, dataset=dataset, dataset_row_index=0)
    return user, project, case, dataset, execution


@pytest.mark.django_db
def test_preparation_binds_data_marks_input_and_preserves_legacy_json_variables(scene_execution):
    _, project, case, dataset, execution = scene_execution
    original_variables = copy.deepcopy(case.variables)
    config = secret_config(project, {'password': 'synthetic-password-42'})
    with override_settings(APP_SCENE_SECRET_ALIASES=config):
        flow, variables = prepare_scene_execution(case, execution)
    assert variables[0]['value'] == {'nested': [1, 2]}
    assert variables[0]['metadata'] == {'legacy': True}
    assert [item['value'] for item in variables if item.get('name') == 'username'] == ['selected', 'selected']
    assert variables[3]['value'] == 'synthetic-password-42'
    assert variables[3]['sensitive'] is True
    assert flow[0]['config']['sensitive'] is True
    assert case.variables == original_variables
    dataset.refresh_from_db()
    assert 'synthetic-password-42' not in str(dataset.rows)


@pytest.mark.django_db
def test_preparation_rechecks_project_and_expands_versioned_component(scene_execution):
    user, project, case, _, execution = scene_execution
    AppProjectComponent.objects.create(
        project=project, name='input', type='project_input', version='1',
        schema={'properties': {'value': {'type': 'string'}}, 'required': ['value']},
        steps=[{'type': 'input', 'config': {'value': '{{params.value}}'}}],
    )
    config = secret_config(project, {'password': 'synthetic-password-42'})
    with override_settings(APP_SCENE_SECRET_ALIASES=config):
        case.ui_flow = [{'kind': 'custom', 'type': 'project_input', 'version': '1',
                         'params': {'value': '{{local.password}}'}}]
        flow, _ = prepare_scene_execution(case, execution)
        assert flow[0]['type'] == 'input'
        assert flow[0]['config']['sensitive'] is True
        project.owner = User.objects.create_user(username='replacement-owner')
        project.save(update_fields=['owner'])
        with pytest.raises(ScenePreparationError):
            prepare_scene_execution(case, execution)


@pytest.mark.django_db
def test_preparation_fails_before_device_for_unknown_alias_or_wrong_execution(scene_execution):
    _, project, case, _, execution = scene_execution
    with override_settings(APP_SCENE_SECRET_ALIASES=secret_config(project)):
        with pytest.raises(ScenePreparationError) as error:
            prepare_scene_execution(case, execution)
    assert 'password' not in str(error.value)
    execution.test_case_id += 100
    with pytest.raises(ScenePreparationError, match='不匹配'):
        prepare_scene_execution(case, execution)


@pytest.mark.django_db
def test_nested_alias_marks_the_whole_legacy_variable_sensitive(scene_execution):
    _, project, case, _, execution = scene_execution
    execution.dataset = None
    case.variables = [{'name': 'login', 'value': {'password': 'secret://password', 'token': ['secret://shared']}}]
    case.ui_flow = [{'type': 'input', 'config': {'value': '{{local.login.password}}'}}]
    config = secret_config(project, {'password': 'synthetic-nested-secret'}, {'shared': 'synthetic-shared-secret'})
    with override_settings(APP_SCENE_SECRET_ALIASES=config):
        flow, variables = prepare_scene_execution(case, execution)
    assert variables[0]['value']['password'] == 'synthetic-nested-secret'
    assert variables[0]['value']['token'] == ['synthetic-shared-secret']
    assert variables[0]['sensitive'] is True
    assert flow[0]['config']['sensitive'] is True


@pytest.mark.django_db
def test_nested_alias_resolution_rechecks_membership(scene_execution):
    _, project, case, _, execution = scene_execution
    execution.dataset = None
    case.variables = [{'name': 'login', 'value': {'password': 'secret://password'}}]
    project.owner = User.objects.create_user(username='nested-replacement-owner')
    project.save(update_fields=['owner'])
    config = secret_config(project, {'password': 'synthetic-nested-secret'})
    with override_settings(APP_SCENE_SECRET_ALIASES=config):
        with pytest.raises(ScenePreparationError):
            prepare_scene_execution(case, execution)
