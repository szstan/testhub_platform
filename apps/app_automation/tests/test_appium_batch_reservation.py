from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from apps.app_automation import appium_execution, device_pool, tasks
from apps.app_automation.appium_models import AppiumSession
from apps.app_automation.execution_admission import (
    claim_app_execution_batch,
    mark_device_action_pending,
    release_device_if_idle,
)
from apps.app_automation.executors import test_executor
from apps.app_automation.models import AppDevice, AppPackage, AppProject, AppTestCase, AppTestExecution
from apps.users.models import User


@pytest.fixture
def appium_batch(settings, monkeypatch):
    owner = User.objects.create_user(username='batch-appium-review-fixture')
    project = AppProject.objects.create(name='Appium batch fixture', owner=owner, appium_enabled=True)
    package = AppPackage.objects.create(name='Fixture package', package_name='org.qa.batch', created_by=owner)
    device = AppDevice.objects.create(name='Fixture device', device_id='batch-fixture-device', status='available')
    device, _ = device_pool.acquire_specific_device(device.pk, owner, lease_seconds=90)
    settings.APP_APPIUM_ENDPOINTS = {device.device_id: 'http://127.0.0.1:4723'}
    monkeypatch.setattr(appium_execution, '_validate_execution_inputs', lambda *args: ('http://127.0.0.1:4723', 8200, {}))
    outcomes = []

    def create_driver(*args, **kwargs):
        driver = Mock(session_id=str(uuid4()))
        outcomes.append(driver)
        return driver

    attached = Mock()
    monkeypatch.setattr(appium_execution, 'create_driver', create_driver)
    monkeypatch.setattr(appium_execution, 'attach_driver', Mock(return_value=attached))
    dispatch_id = str(uuid4())

    def make_execution(engine, *, task_id=dispatch_id):
        case = AppTestCase.objects.create(
            name=f'Fixture {engine}', project=project, app_package=package, engine=engine,
            created_by=owner, ui_flow=[{'type': 'wait', 'timeout': 1}],
        )
        return AppTestExecution.objects.create(
            test_case=case, device=device, user=owner, task_id=task_id,
            device_lease_generation=device.lease_generation,
        )

    return SimpleNamespace(
        device=device, owner=owner, package=package, dispatch_id=dispatch_id,
        outcomes=outcomes, attached=attached, make_execution=make_execution,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('second_engine', ['legacy', 'appium'])
def test_batch_worker_executes_same_dispatch_siblings_and_clears_device_markers(
    appium_batch, second_engine, tmp_path, monkeypatch,
):
    batch = appium_batch
    executions = [batch.make_execution('appium'), batch.make_execution(second_engine)]
    executor = test_executor.AppTestExecutor
    spawned = []

    def spawn(*args, **kwargs):
        spawned.append(kwargs['env']['APP_TEST_CASE_ID'])
        return Mock(wait=Mock(return_value=0), poll=Mock(return_value=0))

    monkeypatch.setattr(executor, '_preflight_worker', lambda self: {'adb_path': 'fixture/adb'})
    monkeypatch.setattr(executor, '_get_allure_results_dir', lambda *args: str(tmp_path / 'allure'))
    monkeypatch.setattr(executor, '_parse_allure_results', lambda *args: {'total': 1, 'passed': 1, 'failed': 0})
    monkeypatch.setattr(executor, '_generate_allure_report', lambda *args: 'fixture/report')
    monkeypatch.setattr(test_executor.subprocess, 'Popen', spawn)
    monkeypatch.setattr(device_pool, 'DeviceLeaseHeartbeat', Mock())
    monkeypatch.setattr(tasks, 'send_execution_update', Mock())

    tasks.execute_app_batch_task.run(batch.dispatch_id, lease_generation=batch.device.lease_generation)

    assert spawned == [str(row.test_case_id) for row in executions]
    for row in executions:
        row.refresh_from_db()
        assert (row.status, row.result, row.device_action_pending) == ('completed', 'passed', False)
    expected_sessions = 1 if second_engine == 'legacy' else 2
    assert len(batch.outcomes) == expected_sessions
    assert batch.attached.quit.call_count == expected_sessions
    assert AppiumSession.objects.filter(status='closed', execution__in=executions).count() == expected_sessions
    assert release_device_if_idle(batch.device.pk, batch.owner, batch.device.lease_generation)


@pytest.mark.django_db(transaction=True)
def test_other_dispatch_still_blocks_admission_without_leaving_a_device_marker(appium_batch):
    batch = appium_batch
    first, sibling = batch.make_execution('appium'), batch.make_execution('legacy')
    batch_ids = [first.pk, sibling.pk]
    assert claim_app_execution_batch(batch_ids)
    mark_device_action_pending(first.pk)
    batch.make_execution('legacy', task_id=str(uuid4()))

    with pytest.raises(appium_execution.AppiumExecutionError, match='cannot be reserved'):
        appium_execution.start_execution_session(
            first.pk, batch.device.device_id, batch.package.package_name,
            batch_dispatch_id=batch.dispatch_id, batch_execution_ids=batch_ids,
        )

    first.refresh_from_db()
    assert batch.outcomes == []
    assert first.device_action_pending is False
    assert AppiumSession.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_rejected_duplicate_reservation_preserves_the_active_session_marker(appium_batch):
    batch = appium_batch
    first = batch.make_execution('appium')
    assert claim_app_execution_batch([first.pk])
    mark_device_action_pending(first.pk)
    arguments = (first.pk, batch.device.device_id, batch.package.package_name)
    handle = appium_execution.start_execution_session(*arguments)
    with pytest.raises(appium_execution.AppiumExecutionError, match='cannot be reserved'):
        appium_execution.start_execution_session(*arguments)
    first.refresh_from_db()
    assert first.device_action_pending
    assert len(batch.outcomes) == 1
    assert appium_execution.finish_execution_session(handle, child_exit_confirmed=True)
