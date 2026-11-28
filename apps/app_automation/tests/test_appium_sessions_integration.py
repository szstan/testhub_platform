import base64
import subprocess
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest
from PIL import Image

from apps.app_automation import appium_execution, appium_runtime, appium_tasks
from apps.app_automation.appium_models import AppiumOperation, AppiumSession
from apps.app_automation.appium_sessions import (
    close_expired_interactive_sessions,
    dispatch_operation,
    fenced_session_snapshot,
    finish_operation,
    reserve_interactive_session,
    reserve_session_operation,
)
from apps.app_automation.device_pool import acquire_specific_device, reap_expired_device_leases
from apps.app_automation.executors.test_executor import AppTestExecutor
from apps.app_automation.models import AppDevice, AppPackage, AppProject, AppTestCase, AppTestExecution
from apps.users.models import User
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def appium_device(settings):
    user = User.objects.create_user(username='appium-qa-fixture')
    project = AppProject.objects.create(name='Appium test project', owner=user, appium_enabled=True)
    package = AppPackage.objects.create(name='Android test app', package_name='org.example.qa', created_by=user)
    device = AppDevice.objects.create(name='Fake device', device_id='qa-fake-device', status='available')
    settings.APP_APPIUM_ENDPOINTS = {str(device.pk): 'http://127.0.0.1:4723'}
    leased, token = acquire_specific_device(device.pk, user, lease_seconds=90, cap_lease_to_grant=True)
    client = APIClient()
    client.force_authenticate(user=user)
    return SimpleNamespace(user=user, device=leased, lease_token=token, project=project, package=package, client=client)


@pytest.mark.django_db(transaction=True)
def test_close_receipt_survives_lease_expiry_and_same_owner_reacquisition(appium_device, monkeypatch):
    fixture = appium_device
    session, start, _ = reserve_interactive_session(
        user=fixture.user,
        device_id=fixture.device.pk,
        lease_token=fixture.lease_token,
        expected_user_id=fixture.user.pk,
        project_id=fixture.project.pk,
        package_id=fixture.package.pk,
        request_key=str(uuid.uuid4()),
    )
    assert fenced_session_snapshot(session.pk, start.pk)
    assert finish_operation(start.pk, session.pk, status='succeeded', result={'remote_session_id': 'fixture-remote'})
    close_key = str(uuid.uuid4())
    _, close_operation, _ = reserve_session_operation(
        user=fixture.user,
        session_id=session.pk,
        expected_user_id=fixture.user.pk,
        lease_token=fixture.lease_token,
        request_key=close_key,
        action='close',
    )
    assert fenced_session_snapshot(session.pk, close_operation.pk)
    assert finish_operation(close_operation.pk, session.pk, status='succeeded')
    operation_url = f'/api/app-automation/appium/operations/{close_operation.pk}/'
    assert fixture.client.get(operation_url).data['result']['lease_ended'] is False

    AppDevice.objects.filter(pk=fixture.device.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert reap_expired_device_leases() == 1
    replacement_device, _ = acquire_specific_device(fixture.device.pk, fixture.user, cap_lease_to_grant=True)
    assert replacement_device.lease_generation != session.lease_generation
    assert fixture.client.get(operation_url).data['result']['lease_ended'] is True

    publish_close = Mock()
    monkeypatch.setattr(appium_tasks.close_appium_session_task, 'apply_async', publish_close)
    close_url = f'/api/app-automation/appium/sessions/{session.pk}/close/'
    close_request = {'expected_user_id': fixture.user.pk, 'lease_token': fixture.lease_token}
    for request_key in (close_key, str(uuid.uuid4())):
        response = fixture.client.post(close_url, {**close_request, 'idempotency_key': request_key}, format='json')
        assert response.status_code == 202
        assert response.data['operation_id'] == str(close_operation.pk)
    publish_close.assert_not_called()
    assert session.operations.filter(action='close').count() == 1
    replacement_device.refresh_from_db()
    assert replacement_device.locked_by_id == fixture.user.pk

    other = User.objects.create_user(username='unrelated-receipt-fixture')
    fixture.client.force_authenticate(other)
    response = fixture.client.post(close_url, {
        **close_request, 'expected_user_id': other.pk, 'idempotency_key': close_key,
    }, format='json')
    assert response.status_code == 404
    publish_close.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('outcome', ['succeeded', 'unknown'])
def test_close_confirmation_follows_expiry_worker_receipt(appium_device, monkeypatch, outcome):
    fixture = appium_device
    session, start, _ = reserve_interactive_session(
        user=fixture.user,
        device_id=fixture.device.pk,
        lease_token=fixture.lease_token,
        expected_user_id=fixture.user.pk,
        project_id=fixture.project.pk,
        package_id=fixture.package.pk,
        request_key=str(uuid.uuid4()),
    )
    assert fenced_session_snapshot(session.pk, start.pk)
    assert finish_operation(start.pk, session.pk, status='succeeded', result={'remote_session_id': 'fixture-remote'})
    lost_key = str(uuid.uuid4())
    close_url = f'/api/app-automation/appium/sessions/{session.pk}/close/'
    close_request = {
        'expected_user_id': fixture.user.pk, 'lease_token': fixture.lease_token, 'idempotency_key': lost_key,
    }

    def confirm_close():
        return fixture.client.post(close_url, close_request, format='json')

    # The first client request never reached the API, so the sweep owns teardown.
    AppDevice.objects.filter(pk=fixture.device.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert confirm_close().status_code == 403
    publish_close = Mock()
    monkeypatch.setattr(appium_tasks.close_appium_session_task, 'apply_async', publish_close)
    close_expired_interactive_sessions()
    close_operation = session.operations.get(action='close')
    assert close_operation.request_key != lost_key

    def assert_receipt(response, expected_status):
        assert response.status_code == 202
        assert response.data == {
            'session_id': str(session.pk), 'operation_id': str(close_operation.pk), 'status': expected_status,
        }

    assert_receipt(confirm_close(), 'queued')
    assert fixture.client.post(close_url, {**close_request, 'idempotency_key': start.request_key}, format='json').status_code == 409
    while_running = []

    def quit_driver():
        while_running.append(confirm_close())
        if outcome == 'unknown':
            raise TimeoutError('fixture teardown response was lost')

    driver = Mock()
    driver.quit.side_effect = quit_driver
    monkeypatch.setattr(appium_tasks, 'attach_driver', Mock(return_value=driver))
    appium_tasks.close_appium_session_task.run(str(close_operation.pk))
    assert len(while_running) == 1
    assert_receipt(while_running[0], 'running')
    assert_receipt(confirm_close(), outcome)
    publish_close.assert_called_once_with(
        args=[str(close_operation.pk)], task_id=str(close_operation.pk), queue='app_interactive',
    )
    driver.quit.assert_called_once_with()
    assert session.operations.filter(action='close').count() == 1

    operation_url = f'/api/app-automation/appium/operations/{close_operation.pk}/'
    polled = fixture.client.get(operation_url)
    assert polled.status_code == 200
    assert polled.data['status'] == outcome
    fixture.device.refresh_from_db()
    if outcome == 'unknown':
        assert polled.data['recovery_required'] is True
        assert fixture.device.device_action_pending is True
        assert reap_expired_device_leases() == 0
    else:
        assert polled.data['result']['status'] == 'closed'
        assert fixture.device.device_action_pending is False
        assert reap_expired_device_leases() == 1
        assert fixture.client.get(operation_url).data['result']['lease_ended'] is True

    other = User.objects.create_user(username='unrelated-expiry-receipt-fixture')
    fixture.client.force_authenticate(other)
    assert fixture.client.post(close_url, {**close_request, 'expected_user_id': other.pk}, format='json').status_code == 404
    assert fixture.client.get(operation_url).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_interactive_draft_lookup_and_expired_session_cleanup(appium_device, monkeypatch):
    fixture = appium_device
    driver = Mock(session_id='private-session', current_package=fixture.package.package_name)
    driver.find_elements.return_value = [Mock(), Mock()]
    monkeypatch.setattr(appium_tasks, 'create_driver', Mock(return_value=driver))
    monkeypatch.setattr(appium_tasks, 'attach_driver', Mock(return_value=driver))
    base = '/api/app-automation'
    identity = {'expected_user_id': fixture.user.pk, 'lease_token': fixture.lease_token}
    response = fixture.client.post(f'{base}/devices/{fixture.device.pk}/appium/sessions/', {
        **identity, 'idempotency_key': '7517c911-3ae4-47a9-a9ea-821d01cb5b98',
        'project_id': fixture.project.pk, 'app_package_id': fixture.package.pk,
    }, format='json')
    assert response.status_code == 202, response.data
    session = AppiumSession.objects.get(pk=response.data['session_id'])
    assert session.status == 'active'
    operations = f'{base}/appium/sessions/{session.pk}/operations/'
    response = fixture.client.post(operations, {
        **identity, 'idempotency_key': 'e882f373-44dc-444d-9f95-96efc9ceec5d', 'action': 'locate',
        'locator': {'strategy': 'id', 'value': 'org.example.qa:id/duplicate'},
    }, format='json')
    assert response.status_code == 202, response.data
    operation = fixture.client.get(f'{base}/appium/operations/{response.data["operation_id"]}/')
    assert operation.data['result']['count'] == 2
    assert 'private-session' not in str(operation.data)

    AppDevice.objects.filter(pk=fixture.device.pk).update(lease_expires_at=timezone.now())
    close_expired_interactive_sessions()
    session.refresh_from_db()
    fixture.device.refresh_from_db()
    assert session.status == 'closed'
    assert fixture.device.device_action_pending is False
    driver.quit.assert_called_once_with()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('published_to_queue', [False, True], ids=['not-published', 'worker-never-claimed'])
def test_expired_queued_interactive_start_is_failed_before_device_io(appium_device, published_to_queue):
    from apps.app_automation.tasks import check_and_release_expired_devices

    fixture = appium_device
    session, operation, created = reserve_interactive_session(
        user=fixture.user,
        device_id=fixture.device.pk,
        lease_token=fixture.lease_token,
        expected_user_id=fixture.user.pk,
        project_id=fixture.project.pk,
        package_id=fixture.package.pk,
        request_key=str(uuid.uuid4()),
    )
    assert created
    if published_to_queue:
        dispatch_operation(operation, SimpleNamespace(apply_async=Mock()))
        operation.refresh_from_db()
        assert operation.dispatched_at is not None

    AppDevice.objects.filter(pk=fixture.device.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    check_and_release_expired_devices.run()

    session.refresh_from_db()
    operation.refresh_from_db()
    fixture.device.refresh_from_db()
    assert operation.status == 'failed'
    assert operation.error_code == 'start_expired_before_device_io'
    assert session.status == 'failed'
    assert session.closed_at is not None
    assert fixture.device.device_action_pending is False
    assert fixture.device.locked_by_id is None
    assert fixture.device.lease_generation == session.lease_generation


@pytest.mark.django_db(transaction=True)
def test_start_worker_dequeued_after_expiry_cannot_start_device_io(appium_device, monkeypatch):
    fixture = appium_device
    session, operation, _created = reserve_interactive_session(
        user=fixture.user,
        device_id=fixture.device.pk,
        lease_token=fixture.lease_token,
        expected_user_id=fixture.user.pk,
        project_id=fixture.project.pk,
        package_id=fixture.package.pk,
        request_key=str(uuid.uuid4()),
    )
    dispatch_operation(operation, SimpleNamespace(apply_async=Mock()))
    AppDevice.objects.filter(pk=fixture.device.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    close_expired_interactive_sessions()

    create_driver = Mock()
    monkeypatch.setattr(appium_tasks, 'create_driver', create_driver)
    result = appium_tasks.start_appium_session_task.run(str(operation.pk))

    session.refresh_from_db()
    operation.refresh_from_db()
    fixture.device.refresh_from_db()
    assert result is None
    assert session.status == 'failed'
    assert operation.status == 'failed'
    assert not fixture.device.device_action_pending
    create_driver.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_expired_claimed_start_becomes_unknown_and_keeps_action_fence(appium_device):
    fixture = appium_device
    session, operation, _created = reserve_interactive_session(
        user=fixture.user,
        device_id=fixture.device.pk,
        lease_token=fixture.lease_token,
        expected_user_id=fixture.user.pk,
        project_id=fixture.project.pk,
        package_id=fixture.package.pk,
        request_key=str(uuid.uuid4()),
    )
    assert appium_tasks._operation_context(str(operation.pk)) is not None
    AppDevice.objects.filter(pk=fixture.device.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    close_expired_interactive_sessions()

    session.refresh_from_db()
    operation.refresh_from_db()
    fixture.device.refresh_from_db()
    assert operation.status == 'unknown'
    assert operation.error_code == 'start_expiry_outcome_unknown'
    assert session.status == 'unknown'
    assert fixture.device.device_action_pending
    assert fixture.device.locked_by_id == fixture.user.pk


@pytest.mark.django_db(transaction=True)
def test_expired_queued_start_does_not_clear_a_replacement_lease_marker(appium_device):
    fixture = appium_device
    replacement_owner = User.objects.create_user(username='replacement-appium-owner')
    session, operation, _created = reserve_interactive_session(
        user=fixture.user,
        device_id=fixture.device.pk,
        lease_token=fixture.lease_token,
        expected_user_id=fixture.user.pk,
        project_id=fixture.project.pk,
        package_id=fixture.package.pk,
        request_key=str(uuid.uuid4()),
    )
    replacement_generation = session.lease_generation + 1
    replacement_epoch = session.device_action_epoch + 1
    AppDevice.objects.filter(pk=fixture.device.pk).update(
        locked_by_id=replacement_owner.pk,
        lease_generation=replacement_generation,
        device_action_epoch=replacement_epoch,
        device_action_pending=True,
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    close_expired_interactive_sessions()

    session.refresh_from_db()
    operation.refresh_from_db()
    fixture.device.refresh_from_db()
    assert session.status == 'failed'
    assert operation.status == 'failed'
    assert fixture.device.locked_by_id == replacement_owner.pk
    assert fixture.device.lease_generation == replacement_generation
    assert fixture.device.device_action_epoch == replacement_epoch
    assert fixture.device.device_action_pending


@pytest.mark.django_db(transaction=True)
def test_expired_queued_operation_is_settled_and_queued_close_recovers_once(appium_device, monkeypatch):
    fixture = appium_device
    driver = Mock(session_id='private-session')
    monkeypatch.setattr(appium_tasks, 'create_driver', Mock(return_value=driver))
    session, start_operation, _created = reserve_interactive_session(
        user=fixture.user,
        device_id=fixture.device.pk,
        lease_token=fixture.lease_token,
        expected_user_id=fixture.user.pk,
        project_id=fixture.project.pk,
        package_id=fixture.package.pk,
        request_key=str(uuid.uuid4()),
    )
    appium_tasks.start_appium_session_task.run(str(start_operation.pk))
    session.refresh_from_db()
    assert session.status == 'active'

    queued_session, queued_operation, created = reserve_session_operation(
        user=fixture.user,
        session_id=session.pk,
        lease_token=fixture.lease_token,
        expected_user_id=fixture.user.pk,
        request_key=str(uuid.uuid4()),
        action='screenshot',
    )
    assert created and queued_session.pk == session.pk
    dispatch_close = Mock()
    monkeypatch.setattr(appium_tasks.close_appium_session_task, 'apply_async', dispatch_close)
    stale_attach = Mock()
    monkeypatch.setattr(appium_tasks, 'attach_driver', stale_attach)
    AppDevice.objects.filter(pk=fixture.device.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )

    appium_tasks.run_appium_operation_task.run(str(queued_operation.pk))
    close_expired_interactive_sessions()
    close_expired_interactive_sessions()

    queued_operation.refresh_from_db()
    session.refresh_from_db()
    fixture.device.refresh_from_db()
    close_operations = list(session.operations.filter(action='close'))
    assert queued_operation.status == 'failed'
    assert queued_operation.error_code == 'operation_expired_before_device_io'
    assert session.status == 'closing'
    assert len(close_operations) == 1
    assert close_operations[0].status == 'queued'
    assert dispatch_close.call_count == 1
    assert dispatch_close.call_args.kwargs['task_id'] == str(close_operations[0].pk)
    assert fixture.device.device_action_pending
    assert fixture.device.locked_by_id == fixture.user.pk
    assert fixture.device.lease_generation == session.lease_generation
    assert fixture.device.device_action_epoch == session.device_action_epoch

    appium_tasks.run_appium_operation_task.run(str(queued_operation.pk))
    stale_attach.assert_not_called()
    attach = Mock(return_value=Mock())
    close_driver = Mock()
    monkeypatch.setattr(appium_tasks, 'attach_driver', attach)
    monkeypatch.setattr(appium_tasks, 'quit_driver', close_driver)
    close_operation = close_operations[0]
    appium_tasks.close_appium_session_task.run(str(close_operation.pk))
    appium_tasks.close_appium_session_task.run(str(close_operation.pk))
    close_operation.refresh_from_db()
    session.refresh_from_db()
    fixture.device.refresh_from_db()
    assert close_operation.status == 'succeeded'
    assert session.status == 'closed'
    assert not fixture.device.device_action_pending
    assert close_driver.call_count == 1
    assert close_driver.call_args.kwargs == {
        'expected_endpoint': session.appium_endpoint,
        'expected_session_id': 'private-session',
    }


@pytest.mark.django_db(transaction=True)
def test_expired_closing_reuses_the_already_queued_close_operation(appium_device, monkeypatch):
    fixture = appium_device
    driver = Mock(session_id='private-session')
    monkeypatch.setattr(appium_tasks, 'create_driver', Mock(return_value=driver))
    session, start_operation, _created = reserve_interactive_session(
        user=fixture.user,
        device_id=fixture.device.pk,
        lease_token=fixture.lease_token,
        expected_user_id=fixture.user.pk,
        project_id=fixture.project.pk,
        package_id=fixture.package.pk,
        request_key=str(uuid.uuid4()),
    )
    appium_tasks.start_appium_session_task.run(str(start_operation.pk))
    session.refresh_from_db()

    session, close_operation, created = reserve_session_operation(
        user=fixture.user,
        session_id=session.pk,
        lease_token=fixture.lease_token,
        expected_user_id=fixture.user.pk,
        request_key=str(uuid.uuid4()),
        action='close',
    )
    assert created and session.status == 'closing'
    dispatch_close = Mock()
    monkeypatch.setattr(appium_tasks.close_appium_session_task, 'apply_async', dispatch_close)
    assert dispatch_operation(close_operation, appium_tasks.close_appium_session_task)
    AppiumOperation.objects.filter(pk=close_operation.pk).update(
        dispatched_at=timezone.now() - timedelta(seconds=5),
    )
    AppDevice.objects.filter(pk=fixture.device.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )

    close_expired_interactive_sessions()

    close_operation.refresh_from_db()
    session.refresh_from_db()
    fixture.device.refresh_from_db()
    assert close_operation.status == 'queued'
    assert close_operation.dispatched_at > fixture.device.lease_expires_at
    assert session.status == 'closing'
    assert len(list(session.operations.filter(action='close'))) == 1
    assert dispatch_close.call_count == 2
    assert all(call.kwargs['task_id'] == str(close_operation.pk) for call in dispatch_close.call_args_list)
    assert fixture.device.device_action_pending
    assert fixture.device.locked_by_id == fixture.user.pk

    from selenium.common.exceptions import InvalidSessionIdException

    close_driver = Mock(session_id='private-session')
    appium_runtime._trust_driver_close_target(
        close_driver, session.appium_endpoint, session.remote_session_id,
    )
    close_driver.quit.side_effect = InvalidSessionIdException('invalid session')
    monkeypatch.setattr(appium_tasks, 'attach_driver', Mock(return_value=close_driver))
    appium_tasks.close_appium_session_task.run(str(close_operation.pk))
    appium_tasks.close_appium_session_task.run(str(close_operation.pk))
    close_operation.refresh_from_db()
    session.refresh_from_db()
    fixture.device.refresh_from_db()
    assert close_operation.status == 'succeeded'
    assert session.status == 'closed'
    assert not fixture.device.device_action_pending
    close_driver.quit.assert_called_once_with()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('operation_status', ['running', 'unknown'])
def test_expired_running_or_uncertain_operation_keeps_appium_fence(appium_device, monkeypatch, operation_status):
    fixture = appium_device
    driver = Mock(session_id='private-session')
    monkeypatch.setattr(appium_tasks, 'create_driver', Mock(return_value=driver))
    session, start_operation, _created = reserve_interactive_session(
        user=fixture.user,
        device_id=fixture.device.pk,
        lease_token=fixture.lease_token,
        expected_user_id=fixture.user.pk,
        project_id=fixture.project.pk,
        package_id=fixture.package.pk,
        request_key=str(uuid.uuid4()),
    )
    appium_tasks.start_appium_session_task.run(str(start_operation.pk))
    operation = AppiumOperation.objects.create(
        session=session,
        user=fixture.user,
        lease_generation=session.lease_generation,
        device_action_epoch=session.device_action_epoch,
        action='screenshot',
        request_key=str(uuid.uuid4()),
        status=operation_status,
    )
    dispatch_close = Mock()
    monkeypatch.setattr(appium_tasks.close_appium_session_task, 'apply_async', dispatch_close)
    AppDevice.objects.filter(pk=fixture.device.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )

    close_expired_interactive_sessions()

    operation.refresh_from_db()
    session.refresh_from_db()
    fixture.device.refresh_from_db()
    dispatch_close.assert_not_called()
    attach = Mock()
    monkeypatch.setattr(appium_tasks, 'attach_driver', attach)
    appium_tasks.run_appium_operation_task.run(str(operation.pk))
    attach.assert_not_called()
    operation.refresh_from_db()
    assert operation.status == operation_status
    assert session.status == ('unknown' if operation_status == 'unknown' else 'active')
    assert fixture.device.device_action_pending
    assert fixture.device.locked_by_id == fixture.user.pk


def test_quit_accepts_only_trusted_invalid_session_and_keeps_transport_unknown(monkeypatch):
    from selenium.common.exceptions import InvalidSessionIdException

    trusted_driver = Mock(session_id='remote-session')
    appium_runtime._trust_driver_close_target(
        trusted_driver, 'http://127.0.0.1:4723', 'remote-session',
    )
    trusted_driver.quit.side_effect = InvalidSessionIdException('invalid session')
    appium_runtime.quit_driver(
        trusted_driver,
        expected_endpoint='http://127.0.0.1:4723',
        expected_session_id='remote-session',
    )

    mismatched_driver = Mock(session_id='remote-session')
    appium_runtime._trust_driver_close_target(
        mismatched_driver, 'http://127.0.0.1:4723', 'remote-session',
    )
    mismatched_driver.quit.side_effect = InvalidSessionIdException('invalid session')
    with pytest.raises(appium_runtime.AppiumOutcomeUnknown):
        appium_runtime.quit_driver(
            mismatched_driver,
            expected_endpoint='http://127.0.0.1:4724',
            expected_session_id='remote-session',
        )

    timeout_driver = Mock(session_id='remote-session')
    appium_runtime._trust_driver_close_target(
        timeout_driver, 'http://127.0.0.1:4723', 'remote-session',
    )
    timeout_driver.quit.side_effect = TimeoutError('transport timed out')
    with pytest.raises(appium_runtime.AppiumOutcomeUnknown):
        appium_runtime.quit_driver(
            timeout_driver,
            expected_endpoint='http://127.0.0.1:4723',
            expected_session_id='remote-session',
        )

    untrusted_driver = Mock(session_id='remote-session')
    untrusted_driver.quit.side_effect = InvalidSessionIdException('invalid session')
    with pytest.raises(appium_runtime.AppiumOutcomeUnknown):
        appium_runtime.quit_driver(
            untrusted_driver,
            expected_endpoint='http://127.0.0.1:4723',
            expected_session_id='remote-session',
        )


def _run_appium_executor_with_cancel(fixture, tmp_path, monkeypatch, process, *, cancel_kind='stop', quit_error=None):
    case = AppTestCase.objects.create(
        project=fixture.project, app_package=fixture.package, created_by=fixture.user, name='Executor Appium fixture',
    )
    execution = AppTestExecution.objects.create(
        test_case=case, user=fixture.user, device=fixture.device, status='running',
        device_lease_generation=fixture.device.lease_generation,
    )
    driver = Mock(session_id='private-execution')
    monkeypatch.setattr(
        appium_execution, '_validate_execution_inputs',
        lambda *_args: ('http://127.0.0.1:4723', 8200, {'platformName': 'Android'}),
    )
    monkeypatch.setattr(appium_execution, 'create_driver', Mock(return_value=driver))
    monkeypatch.setattr(appium_execution, 'attach_driver', Mock(return_value=driver))
    if quit_error is not None:
        monkeypatch.setattr(appium_execution, 'quit_driver', Mock(side_effect=quit_error))
    executor = AppTestExecutor(base_path=str(tmp_path))
    monkeypatch.setattr(executor, '_preflight_worker', lambda: {
        'adb_path': str(tmp_path / 'adb'), 'allure_path': str(tmp_path / 'allure'),
    })
    monkeypatch.setattr(executor, '_get_allure_results_dir', lambda _execution_id: str(tmp_path / 'allure-results'))
    monkeypatch.setattr(subprocess, 'Popen', Mock(return_value=process))
    checks = {'count': 0}

    def cancel_check():
        checks['count'] += 1
        if checks['count'] >= 4:
            if cancel_kind == 'stop':
                AppTestExecution.objects.filter(pk=execution.pk).update(stop_requested=True)
                raise RuntimeError('APP执行已停止或不再处于运行状态')
            AppDevice.objects.filter(pk=fixture.device.pk).update(
                lease_generation=fixture.device.lease_generation + 1,
            )
            raise RuntimeError('device lease lost')

    from apps.app_automation.execution_admission import mark_device_action_pending

    message = 'device lease lost' if cancel_kind == 'lease-loss' else 'APP执行已停止或不再处于运行状态'
    with pytest.raises(RuntimeError, match=message):
        executor.run_tests(
            test_case_id=case.pk,
            device_id=fixture.device.device_id,
            package_name=fixture.package.package_name,
            execution_id=execution.pk,
            cancel_check=cancel_check,
            before_device_io=lambda: mark_device_action_pending(execution.pk),
            engine='appium',
        )
    return execution, driver, process


@pytest.mark.django_db(transaction=True)
def test_executor_natural_cancel_reaps_appium_session(appium_device, tmp_path, monkeypatch):
    process = Mock(returncode=0)
    process.wait.return_value = 0
    process.poll.return_value = 0

    execution, driver, _process = _run_appium_executor_with_cancel(
        appium_device, tmp_path, monkeypatch, process,
    )

    session = AppiumSession.objects.get(execution=execution)
    execution.refresh_from_db()
    appium_device.device.refresh_from_db()
    assert session.status == 'closed'
    assert session.remote_session_id == ''
    assert execution.device_action_pending is False
    assert appium_device.device.device_action_pending is False
    assert process.wait.call_args_list[0].kwargs == {'timeout': 4}
    process.terminate.assert_not_called()
    driver.quit.assert_called_once_with()


@pytest.mark.django_db(transaction=True)
def test_executor_forced_cancel_closes_session_after_confirmed_process_reap(appium_device, tmp_path, monkeypatch):
    process = Mock()
    process.wait.side_effect = [
        subprocess.TimeoutExpired(cmd='pytest', timeout=4),
        -15,
    ]
    process.poll.return_value = None

    execution, driver, _process = _run_appium_executor_with_cancel(
        appium_device, tmp_path, monkeypatch, process,
    )

    session = AppiumSession.objects.get(execution=execution)
    execution.refresh_from_db()
    appium_device.device.refresh_from_db()
    assert session.status == 'closed'
    assert session.remote_session_id == ''
    assert execution.device_action_pending is False
    assert appium_device.device.device_action_pending is False
    process.terminate.assert_called_once_with()
    driver.quit.assert_called_once_with()


@pytest.mark.django_db(transaction=True)
def test_executor_forced_cancel_retains_fence_when_remote_close_is_unknown(appium_device, tmp_path, monkeypatch):
    process = Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired(cmd='pytest', timeout=4), -15]
    process.poll.return_value = None
    execution, driver, _process = _run_appium_executor_with_cancel(
        appium_device, tmp_path, monkeypatch, process,
        quit_error=appium_execution.AppiumOutcomeUnknown('remote close timed out'),
    )
    session = AppiumSession.objects.get(execution=execution)
    execution.refresh_from_db()
    appium_device.device.refresh_from_db()
    assert session.status == 'unknown'
    assert session.failure_code == 'appium_close_outcome_unknown'
    assert execution.device_action_pending is True
    assert appium_device.device.device_action_pending is True
    process.terminate.assert_called_once_with()
    driver.quit.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_executor_cancel_observes_exit_racing_with_force(appium_device, tmp_path, monkeypatch):
    process = Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired(cmd='pytest', timeout=4)]
    process.poll.side_effect = [None, 0]

    execution, driver, _process = _run_appium_executor_with_cancel(
        appium_device, tmp_path, monkeypatch, process,
    )

    session = AppiumSession.objects.get(execution=execution)
    execution.refresh_from_db()
    appium_device.device.refresh_from_db()
    assert session.status == 'closed'
    assert execution.device_action_pending is False
    assert appium_device.device.device_action_pending is False
    process.terminate.assert_not_called()
    driver.quit.assert_called_once_with()


@pytest.mark.django_db(transaction=True)
def test_executor_natural_cancel_does_not_close_after_lease_loss(appium_device, tmp_path, monkeypatch):
    process = Mock(returncode=0)
    process.wait.return_value = 0
    process.poll.return_value = 0

    execution, driver, _process = _run_appium_executor_with_cancel(
        appium_device, tmp_path, monkeypatch, process, cancel_kind='lease-loss',
    )

    session = AppiumSession.objects.get(execution=execution)
    execution.refresh_from_db()
    appium_device.device.refresh_from_db()
    assert session.status == 'unknown'
    assert execution.device_action_pending is True
    assert appium_device.device.device_action_pending is True
    driver.quit.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_executor_natural_cancel_keeps_fence_when_remote_close_is_unknown(appium_device, tmp_path, monkeypatch):
    process = Mock(returncode=0)
    process.wait.return_value = 0
    process.poll.return_value = 0
    close_error = appium_execution.AppiumOutcomeUnknown('remote close timed out')

    execution, driver, _process = _run_appium_executor_with_cancel(
        appium_device, tmp_path, monkeypatch, process, quit_error=close_error,
    )

    session = AppiumSession.objects.get(execution=execution)
    execution.refresh_from_db()
    appium_device.device.refresh_from_db()
    assert session.status == 'unknown'
    assert execution.device_action_pending is True
    assert appium_device.device.device_action_pending is True
    driver.quit.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_execution_confirmed_cooperative_stop_clears_both_markers(appium_device, monkeypatch):
    fixture = appium_device
    case = AppTestCase.objects.create(
        project=fixture.project, app_package=fixture.package, created_by=fixture.user, name='Appium fixture',
    )
    execution = AppTestExecution.objects.create(
        test_case=case, user=fixture.user, device=fixture.device, status='running',
        device_action_pending=True, device_lease_generation=fixture.device.lease_generation,
    )
    driver = Mock(session_id='private-execution')
    monkeypatch.setattr(appium_execution, 'create_driver', Mock(return_value=driver))
    monkeypatch.setattr(appium_execution, 'attach_driver', Mock(return_value=driver))
    handle = appium_execution.start_execution_session(execution.pk, fixture.device.device_id, fixture.package.package_name)
    AppTestExecution.objects.filter(pk=execution.pk).update(stop_requested=True)
    assert appium_execution.finish_execution_session(handle, child_exit_confirmed=True)
    execution.refresh_from_db()
    fixture.device.refresh_from_db()
    assert not fixture.device.device_action_pending
    assert not execution.device_action_pending
    driver.quit.assert_called_once_with()


def test_native_screen_snapshot_masks_editable_fields():
    buffer = BytesIO()
    Image.new('RGB', (12, 12), color='white').save(buffer, format='PNG')
    driver = Mock(current_package='org.example.qa')
    driver.page_source = (
        '<hierarchy><node class="android.widget.EditText" resource-id="org.example.qa:id/login" '
        'text="fixture-text" bounds="[2,3][6,9]" /></hierarchy>'
    )
    driver.get_screenshot_as_base64.return_value = base64.b64encode(buffer.getvalue()).decode('ascii')
    result = appium_runtime.inspect_screen(driver, package_name='org.example.qa')
    assert 'fixture-text' not in str(result['nodes'])
    with Image.open(BytesIO(base64.b64decode(result['screenshot'].split(',', 1)[1]))) as image:
        assert image.getpixel((3, 4)) == (32, 33, 36)
        assert image.getpixel((0, 0)) == (255, 255, 255)
