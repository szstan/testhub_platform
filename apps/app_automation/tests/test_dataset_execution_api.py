from unittest.mock import patch

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.models import AppDataSet, AppDevice, AppProject, AppTestCase, AppTestExecution
from apps.app_automation.views.test_case_views import AppTestCaseViewSet
from apps.users.models import User


@pytest.fixture
def dataset_case(db):
    user = User.objects.create_user(username='dataset-execution-owner')
    stranger = User.objects.create_user(username='dataset-execution-stranger')
    project = AppProject.objects.create(name='dataset execution', owner=user)
    case = AppTestCase.objects.create(name='case', project=project, created_by=user)
    dataset = AppDataSet.objects.create(
        project=project, name='fixtures', version=1, columns=['name'],
        rows=[{'name': 'row-one-private'}, {'name': 'row-two-private'}], created_by=user,
    )
    device = AppDevice.objects.create(device_id='synthetic-dataset-device', status='online')
    device.lock(user)
    return user, stranger, project, case, dataset, device


def execute(user, case, **payload):
    request = APIRequestFactory().post('/execute-dataset/', payload, format='json')
    force_authenticate(request, user=user)
    return AppTestCaseViewSet.as_view({'post': 'execute_dataset'})(request, pk=case.pk)


@pytest.mark.django_db
def test_all_rows_queue_one_sequential_batch_without_values_in_celery(dataset_case):
    user, _, _, case, dataset, device = dataset_case
    with patch('apps.app_automation.tasks.execute_app_batch_task.apply_async') as dispatch:
        response = execute(user, case, dataset_id=dataset.pk, device_id=device.device_id,
                           expected_user_id=user.pk, mode='all')
    assert response.status_code == 200
    rows = list(AppTestExecution.objects.filter(pk__in=response.data['data']['execution_ids']).order_by('pk'))
    assert [row.dataset_row_index for row in rows] == [0, 1]
    assert all(row.dataset_id == dataset.pk and row.device_id == device.pk for row in rows)
    assert len({row.task_id for row in rows}) == 1
    dispatch.assert_called_once()
    assert 'row-one-private' not in str(dispatch.call_args)
    assert 'row-two-private' not in str(dispatch.call_args)
    assert 'row-one-private' not in str(response.data)


@pytest.mark.django_db
def test_single_row_and_project_scope_are_validated_before_dispatch(dataset_case):
    user, stranger, _, case, dataset, device = dataset_case
    with patch('apps.app_automation.tasks.execute_app_batch_task.apply_async') as dispatch:
        bad_index = execute(user, case, dataset_id=dataset.pk, device_id=device.device_id, row_index=9)
        wrong_user = execute(user, case, dataset_id=dataset.pk, device_id=device.device_id,
                             expected_user_id=stranger.pk)
        forbidden = execute(stranger, case, dataset_id=dataset.pk, device_id=device.device_id)
        valid = execute(user, case, dataset_id=dataset.pk, device_id=device.device_id,
                        mode='single', row_index=1)
    assert bad_index.status_code == 400
    assert wrong_user.status_code == 403
    assert forbidden.status_code == 404
    assert valid.status_code == 200
    execution = AppTestExecution.objects.get(pk=valid.data['data']['execution_ids'][0])
    assert execution.dataset_row_index == 1
    dispatch.assert_called_once()


@pytest.mark.django_db
def test_dataset_from_another_project_and_busy_device_are_rejected(dataset_case):
    user, _, _, case, dataset, device = dataset_case
    other = AppProject.objects.create(name='other dataset project', owner=user)
    foreign = AppDataSet.objects.create(project=other, name='other', version=1,
                                        columns=['x'], rows=[{'x': 'a'}])
    AppTestExecution.objects.create(test_case=case, device=device, user=user, status='pending',
                                    device_lease_generation=device.lease_generation)
    with patch('apps.app_automation.tasks.execute_app_batch_task.apply_async') as dispatch:
        wrong_project = execute(user, case, dataset_id=foreign.pk, device_id=device.device_id)
        busy = execute(user, case, dataset_id=dataset.pk, device_id=device.device_id)
    assert wrong_project.status_code == 404
    assert busy.status_code == 409
    dispatch.assert_not_called()


@pytest.mark.django_db
def test_queue_failure_settles_rows_without_exposing_broker_error(dataset_case):
    user, _, _, case, dataset, device = dataset_case
    with patch('apps.app_automation.tasks.execute_app_batch_task.apply_async',
               side_effect=RuntimeError('synthetic-broker-private')):
        response = execute(user, case, dataset_id=dataset.pk, device_id=device.device_id, mode='all')
    assert response.status_code == 500
    assert 'synthetic-broker-private' not in str(response.data)
    assert list(AppTestExecution.objects.filter(dataset=dataset).values_list('status', flat=True)) == ['error', 'error']
