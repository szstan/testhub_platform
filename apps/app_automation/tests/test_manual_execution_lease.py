from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.app_automation.models import AppDevice, AppProject, AppTestCase, AppTestSuite, AppTestSuiteCase


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_manual_execution_requires_and_accepts_the_current_users_device_lock(kind):
    User = get_user_model()
    owner = User.objects.create_user(username=f'manual-{kind}-owner')
    other = User.objects.create_user(username=f'manual-{kind}-other')
    project = AppProject.objects.create(name='APP lease project', owner=owner)
    project.members.add(other)
    case = AppTestCase.objects.create(name='APP lease case', project=project, created_by=owner)
    device = AppDevice.objects.create(device_id=f'manual-{kind}-device', status='online')

    if kind == 'case':
        url = f'/api/app-automation/test-cases/{case.pk}/execute/'
        task_path = 'apps.app_automation.tasks.execute_app_test_task.apply_async'
    else:
        suite = AppTestSuite.objects.create(name='APP lease suite', project=project, created_by=owner)
        AppTestSuiteCase.objects.create(test_suite=suite, test_case=case)
        url = f'/api/app-automation/test-suites/{suite.pk}/run/'
        task_path = 'apps.app_automation.tasks.execute_app_suite_task.apply_async'

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.post(url, {'device_id': device.device_id}, format='json')
    assert response.status_code == 403

    lock = client.post(f'/api/app-automation/devices/{device.pk}/lock/', {}, format='json')
    assert lock.status_code == 200
    device.refresh_from_db()
    assert device.locked_by_id == owner.pk
    assert not device.lease_token_hash

    with patch(task_path, return_value=SimpleNamespace(id='queued-test')) as submit:
        allowed = client.post(url, {'device_id': device.device_id}, format='json')
    assert allowed.status_code == 200, allowed.data
    assert allowed.data['success'] is True
    assert submit.call_args.kwargs['kwargs']['lease_generation'] == device.lease_generation

    client.force_authenticate(user=other)
    with patch(task_path) as submit:
        denied = client.post(url, {'device_id': device.device_id}, format='json')
    assert denied.status_code == 403
    submit.assert_not_called()
