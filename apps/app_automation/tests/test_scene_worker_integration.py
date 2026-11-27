from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.test import override_settings

from apps.app_automation.models import AppDataSet, AppProject, AppTestCase, AppTestExecution
from apps.app_automation.scene_execution import ScenePreparationError
from apps.app_automation.tests import test_app_flow
from apps.users.models import User


@pytest.mark.django_db
@override_settings(APP_SCENE_SECRET_ALIASES={'global': {'login': 'synthetic-private-login'}})
def test_worker_binds_selected_dataset_and_marks_secret_input_before_device_use():
    owner = User.objects.create_user(username='scene-worker-owner')
    project = AppProject.objects.create(name='scene worker', owner=owner)
    case = AppTestCase.objects.create(
        project=project, created_by=owner, name='case',
        variables=[{'name': 'password', 'scope': 'local', 'value': 'secret://login'}],
        ui_flow=[{'type': 'input', 'config': {'value': '{{local.password}}'}}],
    )
    dataset = AppDataSet.objects.create(project=project, name='accounts', version=1,
                                        columns=['username'], rows=[{'username': 'fixture'}])
    execution = AppTestExecution.objects.create(test_case=case, user=owner, dataset=dataset, dataset_row_index=0)
    airtest = SimpleNamespace(open_app=Mock(return_value=True))
    with (
        patch.object(test_app_flow, 'UiFlowRunner') as runner,
        patch.object(test_app_flow.allure.dynamic, 'title'),
        patch.object(test_app_flow.allure.dynamic, 'suite'),
        patch.object(test_app_flow.allure.dynamic, 'label'),
        patch.object(test_app_flow.allure, 'attach'),
    ):
        runner.return_value.run.return_value = {'total': 1, 'passed': 1, 'failed': 0, 'assertions': 1}
        test_app_flow.TestAppFlow().test_execute_ui_flow(case.pk, 'org.example.app', execution.pk, airtest, 'fixture')
    kwargs = runner.return_value.run.call_args.kwargs
    assert kwargs['ui_flow'][0]['config']['sensitive'] is True
    bound = {item['name']: item for item in kwargs['variables']}
    assert bound['password']['value'] == 'synthetic-private-login'
    assert bound['password']['sensitive'] is True
    assert bound['username']['value'] == 'fixture'
    airtest.open_app.assert_called_once_with('org.example.app')
    case.refresh_from_db()
    assert case.variables[0]['value'] == 'secret://login'


@pytest.mark.django_db
@override_settings(APP_SCENE_SECRET_ALIASES={'global': {'login': 'synthetic-private-login'}})
def test_worker_rejects_revoked_project_access_before_app_launch():
    owner = User.objects.create_user(username='scene-revoked-owner')
    member = User.objects.create_user(username='scene-revoked-member')
    project = AppProject.objects.create(name='revoked project', owner=owner)
    case = AppTestCase.objects.create(project=project, created_by=owner, name='case', ui_flow=[{'type': 'wait'}])
    dataset = AppDataSet.objects.create(project=project, name='accounts', version=1,
                                        columns=['name'], rows=[{'name': 'fixture'}])
    execution = AppTestExecution.objects.create(test_case=case, user=member, dataset=dataset, dataset_row_index=0)
    airtest = SimpleNamespace(open_app=Mock(return_value=True))
    with pytest.raises(ScenePreparationError):
        test_app_flow.TestAppFlow().test_execute_ui_flow(case.pk, 'org.example.app', execution.pk, airtest, 'fixture')
    airtest.open_app.assert_not_called()
