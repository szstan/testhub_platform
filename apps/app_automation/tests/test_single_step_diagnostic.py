from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIClient

from apps.app_automation.executors.test_executor import AppTestExecutor
from apps.app_automation.models import (
    AppDevice,
    AppProject,
    AppProjectComponent,
    AppTestCase,
    AppTestExecution,
)
from apps.app_automation.scene_execution import (
    SCENE_CASE_CHANGED_MESSAGE,
    ScenePreparationError,
    prepare_scene_execution,
)
from apps.app_automation.tasks import classify_app_test_result, execute_app_test_task
from apps.app_automation.tests import test_app_flow
from apps.users.models import User


@pytest.fixture
def diagnostic_resources(db):
    owner = User.objects.create_user(username='single-step-owner')
    other = User.objects.create_user(username='single-step-other')
    project = AppProject.objects.create(name='single-step project', owner=owner)
    case = AppTestCase.objects.create(
        project=project,
        created_by=owner,
        name='single-step case',
        ui_flow=[
            {'type': 'app_start', 'config': {'package_name': 'org.example.synthetic'}},
            {'type': 'wait', 'config': {'seconds': 1}},
        ],
    )
    device = AppDevice.objects.create(device_id='synthetic-single-step-device', status='online')
    device.lock(owner)
    return owner, other, project, case, device


def post_run_step(user, case, payload):
    client = APIClient()
    client.force_authenticate(user=user)
    return client.post(
        f'/api/app-automation/test-cases/{case.pk}/run-step/',
        payload,
        format='json',
    )


@pytest.mark.django_db
def test_run_step_queues_only_saved_selection_and_case_version(diagnostic_resources):
    owner, _, _, case, device = diagnostic_resources
    payload = {
        'device_id': device.device_id,
        'auto_lock': False,
        'expected_user_id': owner.pk,
        'step_index': 0,
        'step': {'type': 'input', 'config': {'value': 'client-private-value'}},
        'input_values': {'password': 'client-private-secret'},
    }

    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async') as dispatch:
        response = post_run_step(owner, case, payload)

    assert response.status_code == 200, response.data
    execution = AppTestExecution.objects.get(pk=response.data['execution']['id'])
    assert execution.status == 'pending'
    assert execution.test_case_id == case.pk
    assert execution.device_id == device.pk
    assert execution.device_lease_generation == device.lease_generation
    dispatch.assert_called_once()
    queued = dispatch.call_args.kwargs
    assert queued['args'] == (execution.pk,)
    assert queued['kwargs']['step_index'] == 0
    assert queued['kwargs']['expected_case_updated_at'] == case.updated_at.isoformat()
    assert 'step' not in queued['kwargs']
    assert 'input_values' not in queued['kwargs']
    assert 'client-private-value' not in str(queued)
    assert 'client-private-secret' not in str(queued)


@pytest.mark.django_db
def test_run_step_can_acquire_the_existing_auto_lock_lease(diagnostic_resources):
    owner, _, _, case, device = diagnostic_resources
    device.unlock(expected_generation=device.lease_generation, expected_owner_id=owner.pk)

    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async') as dispatch:
        response = post_run_step(owner, case, {
            'device_id': device.device_id,
            'auto_lock': True,
            'expected_user_id': owner.pk,
            'step_index': 0,
        })

    assert response.status_code == 200, response.data
    device.refresh_from_db()
    execution = AppTestExecution.objects.get(pk=response.data['execution']['id'])
    assert device.locked_by_id == owner.pk
    assert execution.release_device_lease_on_stop is True
    assert dispatch.call_args.kwargs['kwargs']['release_device_lease'] is True


@pytest.mark.django_db
def test_run_step_rejects_identity_and_device_lease_mismatches(diagnostic_resources):
    owner, other, project, case, device = diagnostic_resources
    project.members.add(other)
    payload = {
        'device_id': device.device_id,
        'auto_lock': False,
        'expected_user_id': other.pk,
        'step_index': 0,
    }

    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async') as dispatch:
        changed_identity = post_run_step(owner, case, {**payload, 'expected_user_id': other.pk})
        foreign_lease = post_run_step(other, case, payload)

    assert changed_identity.status_code == 403
    assert foreign_lease.status_code == 403
    dispatch.assert_not_called()
    assert not AppTestExecution.objects.exists()


@pytest.mark.django_db
def test_run_step_checks_bounds_before_auto_lock_or_execution_creation(diagnostic_resources):
    owner, _, _, case, device = diagnostic_resources
    device.unlock(expected_generation=device.lease_generation, expected_owner_id=owner.pk)
    device.refresh_from_db()

    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async') as dispatch:
        response = post_run_step(owner, case, {
            'device_id': device.device_id,
            'auto_lock': True,
            'expected_user_id': owner.pk,
            'step_index': 2,
        })

    assert response.status_code == 400
    device.refresh_from_db()
    assert device.locked_by_id is None
    assert not AppTestExecution.objects.exists()
    dispatch.assert_not_called()


@pytest.mark.django_db
def test_run_step_rejects_busy_device_without_queueing(diagnostic_resources):
    owner, _, _, case, device = diagnostic_resources
    AppTestExecution.objects.create(
        test_case=case,
        user=owner,
        device=device,
        status='pending',
        device_lease_generation=device.lease_generation,
    )

    with patch('apps.app_automation.tasks.execute_app_test_task.apply_async') as dispatch:
        response = post_run_step(owner, case, {
            'device_id': device.device_id,
            'auto_lock': False,
            'expected_user_id': owner.pk,
            'step_index': 0,
        })

    assert response.status_code == 409
    assert AppTestExecution.objects.count() == 1
    dispatch.assert_not_called()


def test_debug_worker_environment_does_not_require_case_package(monkeypatch):
    for name in (
        'APP_DEVICE_ID', 'APP_TEST_CASE_ID', 'APP_EXECUTION_ID', 'APP_PACKAGE_NAME',
        'APP_DEBUG_STEP_INDEX', 'APP_DEBUG_CASE_UPDATED_AT',
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('APP_DEVICE_ID', 'synthetic-device')
    monkeypatch.setenv('APP_TEST_CASE_ID', '1')
    monkeypatch.setenv('APP_EXECUTION_ID', '2')
    assert not test_app_flow._worker_environment_ready()

    monkeypatch.setenv('APP_DEBUG_STEP_INDEX', '0')
    monkeypatch.setenv('APP_DEBUG_CASE_UPDATED_AT', '2026-01-02T03:04:05+00:00')
    assert test_app_flow._worker_environment_ready()


@pytest.mark.django_db
def test_worker_fails_stale_case_before_claim_or_executor(diagnostic_resources):
    owner, _, _, case, device = diagnostic_resources
    execution = AppTestExecution.objects.create(
        test_case=case,
        user=owner,
        device=device,
        status='pending',
        device_lease_generation=device.lease_generation,
    )
    stale_version = (case.updated_at - timedelta(days=1)).isoformat()

    with (
        patch('apps.app_automation.execution_admission.claim_app_execution_batch') as claim,
        patch('apps.app_automation.tasks.send_execution_update'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor,
    ):
        execute_app_test_task.run(
            execution.pk,
            package_name='',
            lease_generation=device.lease_generation,
            step_index=0,
            expected_case_updated_at=stale_version,
        )

    claim.assert_not_called()
    executor.return_value.run_tests.assert_not_called()
    execution.refresh_from_db()
    assert execution.status == 'error'
    assert execution.error_message == SCENE_CASE_CHANGED_MESSAGE


@pytest.mark.django_db
def test_debug_flow_selects_saved_step_and_does_not_auto_launch(diagnostic_resources, monkeypatch):
    owner, _, _, case, _ = diagnostic_resources
    execution = AppTestExecution.objects.create(test_case=case, user=owner)
    monkeypatch.setenv('APP_DEBUG_STEP_INDEX', '0')
    monkeypatch.setenv('APP_DEBUG_CASE_UPDATED_AT', case.updated_at.isoformat())
    airtest = SimpleNamespace(open_app=Mock(return_value=True))

    with (
        patch.object(test_app_flow, 'UiFlowRunner') as runner,
        patch.object(test_app_flow.allure.dynamic, 'title'),
        patch.object(test_app_flow.allure.dynamic, 'suite'),
        patch.object(test_app_flow.allure.dynamic, 'label'),
        patch.object(test_app_flow.allure, 'attach'),
    ):
        runner.return_value.run.return_value = {
            'total': 1, 'passed': 1, 'failed': 0, 'assertions': 1,
        }
        test_app_flow.TestAppFlow().test_execute_ui_flow(
            case.pk, 'org.example.synthetic', execution.pk, airtest, 'synthetic-user'
        )

    airtest.open_app.assert_not_called()
    runner.return_value.run.assert_called_once()
    assert runner.return_value.run.call_args.kwargs['ui_flow'] == [case.ui_flow[0]]


@pytest.mark.django_db
def test_selected_project_component_expands_after_root_step_selection(diagnostic_resources):
    owner, _, project, case, _ = diagnostic_resources
    AppProjectComponent.objects.create(
        project=project,
        name='two-step component',
        type='two_step_component',
        version='1',
        schema={},
        steps=[
            {'type': 'wait', 'config': {'seconds': 1}},
            {'type': 'wait', 'config': {'seconds': 2}},
        ],
    )
    case.ui_flow = [
        {'kind': 'custom', 'type': 'two_step_component', 'version': '1', 'params': {}},
        {'type': 'input', 'config': {'value': 'must-not-run'}},
    ]
    case.save()
    execution = AppTestExecution.objects.create(test_case=case, user=owner)

    flow, _ = prepare_scene_execution(
        case,
        execution,
        step_index=0,
        expected_case_updated_at=case.updated_at.isoformat(),
    )

    assert [step['config']['seconds'] for step in flow] == [1, 2]
    assert all(step['type'] == 'wait' for step in flow)


def test_executor_passes_only_index_and_timestamp_through_worker_environment(tmp_path, monkeypatch):
    executor = AppTestExecutor(base_path=str(tmp_path))
    process = Mock()
    process.wait.return_value = 0
    captured = {}

    def spawn(_args, **kwargs):
        captured.update(kwargs['env'])
        return process

    monkeypatch.setenv('APP_DEBUG_STEP_INDEX', '99')
    monkeypatch.setenv('APP_DEBUG_CASE_UPDATED_AT', 'wrong-inherited-value')
    with (
        patch.object(executor, '_preflight_worker', return_value={'adb_path': 'adb', 'allure_path': 'allure'}),
        patch.object(executor, '_get_allure_results_dir', return_value=str(tmp_path / 'results')),
        patch.object(executor, '_parse_allure_results', return_value={
            'total': 1, 'passed': 1, 'failed': 0, 'skipped': 0, 'unverified': 0,
        }),
        patch.object(executor, '_generate_allure_report', return_value='report'),
        patch('apps.app_automation.executors.test_executor.subprocess.Popen', side_effect=spawn),
    ):
        result = executor.run_tests(
            1,
            'synthetic-device',
            'org.example.synthetic',
            execution_id=7,
            step_index=0,
            expected_case_updated_at='2026-01-02T03:04:05+00:00',
        )

    assert result['success'] is True
    assert captured['APP_DEBUG_STEP_INDEX'] == '0'
    assert captured['APP_DEBUG_CASE_UPDATED_AT'] == '2026-01-02T03:04:05+00:00'
    assert not any('private' in key.lower() or 'secret' in key.lower() for key in captured)


def test_no_assertion_remains_unverified_for_diagnostic_step(diagnostic_resources, monkeypatch):
    owner, _, _, case, _ = diagnostic_resources
    execution = AppTestExecution.objects.create(test_case=case, user=owner)
    monkeypatch.setenv('APP_DEBUG_STEP_INDEX', '1')
    monkeypatch.setenv('APP_DEBUG_CASE_UPDATED_AT', case.updated_at.isoformat())
    airtest = SimpleNamespace(open_app=Mock(return_value=True))

    with (
        patch.object(test_app_flow, 'UiFlowRunner') as runner,
        patch.object(test_app_flow.allure.dynamic, 'title'),
        patch.object(test_app_flow.allure.dynamic, 'suite'),
        patch.object(test_app_flow.allure.dynamic, 'label') as label,
        patch.object(test_app_flow.allure, 'attach'),
    ):
        runner.return_value.run.return_value = {
            'total': 1, 'passed': 1, 'failed': 0, 'assertions': 0,
        }
        with pytest.raises(pytest.skip.Exception):
            test_app_flow.TestAppFlow().test_execute_ui_flow(
                case.pk, 'org.example.synthetic', execution.pk, airtest, 'synthetic-user'
            )

    label.assert_called_once_with('testhub_verification', 'unverified')
    assert classify_app_test_result({
        'success': True,
        'exit_code': 0,
        'test_results': {
            'total': 1, 'passed': 0, 'failed': 0, 'skipped': 1, 'unverified': 1,
        },
    }) == ('completed', 'unverified', '')
    airtest.open_app.assert_not_called()


@pytest.mark.django_db
def test_child_checks_case_version_before_airtest_setup(diagnostic_resources, monkeypatch):
    owner, _, _, case, device = diagnostic_resources
    execution = AppTestExecution.objects.create(
        test_case=case,
        user=owner,
        device=device,
        status='running',
        device_lease_generation=device.lease_generation,
    )
    from apps.app_automation.execution_admission import mark_device_action_pending

    mark_device_action_pending(execution.pk)
    monkeypatch.setenv('APP_TEST_CASE_ID', str(case.pk))
    monkeypatch.setenv('APP_EXECUTION_ID', str(execution.pk))
    monkeypatch.setenv('APP_DEBUG_STEP_INDEX', '0')
    monkeypatch.setenv(
        'APP_DEBUG_CASE_UPDATED_AT',
        (case.updated_at - timedelta(days=1)).isoformat(),
    )

    with patch.object(test_app_flow, 'AirtestBase') as airtest_base:
        fixture = test_app_flow.TestAppFlow.airtest.__wrapped__(
            test_app_flow.TestAppFlow(), 'synthetic-device', 'synthetic-user'
        )
        with pytest.raises(ScenePreparationError):
            next(fixture)

    airtest_base.assert_not_called()
    execution.refresh_from_db()
    assert execution.device_action_pending is False


@pytest.mark.django_db
def test_worker_persists_report_and_unverified_result_for_diagnostic(diagnostic_resources):
    owner, _, _, case, device = diagnostic_resources
    execution = AppTestExecution.objects.create(
        test_case=case,
        user=owner,
        device=device,
        status='pending',
        device_lease_generation=device.lease_generation,
    )
    heartbeat = Mock()
    heartbeat.start.return_value = heartbeat
    report = {
        'success': True,
        'exit_code': 0,
        'report_path': 'app-automation/allure-reports/execution_1',
        'test_results': {
            'total': 1, 'passed': 0, 'failed': 0, 'skipped': 1, 'unverified': 1,
        },
    }

    with (
        patch('apps.app_automation.device_pool.DeviceLeaseHeartbeat', return_value=heartbeat),
        patch('apps.app_automation.tasks.send_execution_update'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor,
    ):
        executor.return_value.run_tests.return_value = report
        execute_app_test_task.run(
            execution.pk,
            package_name='org.example.synthetic',
            lease_generation=device.lease_generation,
            step_index=0,
            expected_case_updated_at=case.updated_at.isoformat(),
        )

    executor.return_value.run_tests.assert_called_once()
    assert executor.return_value.run_tests.call_args.kwargs['step_index'] == 0
    assert executor.return_value.run_tests.call_args.kwargs['expected_case_updated_at'] == case.updated_at.isoformat()
    execution.refresh_from_db()
    assert execution.status == 'completed'
    assert execution.result == 'unverified'
    assert execution.report_path == report['report_path']
