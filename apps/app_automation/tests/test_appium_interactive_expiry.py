import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.app_automation import appium_tasks
from apps.app_automation.appium_models import AppiumOperation, AppiumSession
from apps.app_automation.appium_sessions import (
    CLOSE_REPUBLISH_INTERVAL,
    close_expired_interactive_sessions,
    dispatch_operation,
    fenced_session_snapshot,
    finish_operation,
    reserve_interactive_session,
)
from apps.app_automation.device_pool import DeviceLeaseError, acquire_specific_device, release_device
from apps.app_automation.models import AppDevice, AppPackage, AppProject
from apps.users.models import User


@pytest.fixture
def appium_expiry_context(settings):
    suffix = uuid.uuid4().hex[:10]
    user = User.objects.create_user(username=f"appium-expiry-{suffix}")
    project = AppProject.objects.create(
        name="Appium expiry project", owner=user, appium_enabled=True,
    )
    package = AppPackage.objects.create(
        name="Appium expiry app", package_name="org.example.expiry", created_by=user,
    )
    device = AppDevice.objects.create(
        name="Appium expiry device", device_id=f"expiry-{suffix}", status="available",
    )
    device, lease_token = acquire_specific_device(
        device.pk, user, lease_seconds=90, cap_lease_to_grant=True,
    )
    settings.APP_APPIUM_ENDPOINTS = {str(device.pk): "http://127.0.0.1:4723"}
    return SimpleNamespace(
        user=user,
        project=project,
        package=package,
        device=device,
        lease_token=lease_token,
    )


def _expire_lease(device):
    AppDevice.objects.filter(pk=device.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )


def _reserve_start(context):
    session, operation, created = reserve_interactive_session(
        user=context.user,
        device_id=context.device.pk,
        lease_token=context.lease_token,
        expected_user_id=context.user.pk,
        project_id=context.project.pk,
        package_id=context.package.pk,
        request_key=str(uuid.uuid4()),
    )
    assert created
    dispatch_operation(operation, SimpleNamespace(apply_async=Mock()))
    return session, operation


def _make_active_session(context, *, operation_action=None, session_status="active"):
    device = AppDevice.objects.get(pk=context.device.pk)
    device.device_action_epoch += 1
    device.device_action_pending = True
    device.save(update_fields=["device_action_epoch", "device_action_pending", "updated_at"])
    session = AppiumSession.objects.create(
        kind="interactive",
        device=device,
        user=context.user,
        project=context.project,
        package=context.package,
        lease_generation=device.lease_generation,
        device_action_epoch=device.device_action_epoch,
        system_port=8201,
        appium_endpoint="http://127.0.0.1:4723",
        remote_session_id="fake-remote-session",
        status=session_status,
        request_key=str(uuid.uuid4()),
    )
    operation = None
    if operation_action is not None:
        operation = AppiumOperation.objects.create(
            session=session,
            user=context.user,
            lease_generation=session.lease_generation,
            device_action_epoch=session.device_action_epoch,
            action=operation_action,
            request_key=str(uuid.uuid4()),
            payload={"element_id": 1, "locator": {}} if operation_action == "click" else {},
        )
    return session, operation


def _fake_async_publish(monkeypatch):
    publish = Mock()
    monkeypatch.setattr(appium_tasks.close_appium_session_task, "apply_async", publish)
    return publish


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('claim_status', ['queued', 'running', 'succeeded'])
def test_failed_start_publish_respects_the_durable_worker_claim(appium_expiry_context, monkeypatch, claim_status):
    context = appium_expiry_context
    session, operation, _ = reserve_interactive_session(
        user=context.user,
        device_id=context.device.pk,
        lease_token=context.lease_token,
        expected_user_id=context.user.pk,
        project_id=context.project.pk,
        package_id=context.package.pk,
        request_key=str(uuid.uuid4()),
    )

    def publish_then_raise(**_kwargs):
        if claim_status != 'queued':
            assert fenced_session_snapshot(session.pk, operation.pk)
        if claim_status == 'succeeded':
            assert finish_operation(operation.pk, session.pk, status='succeeded', result={
                'remote_session_id': 'fixture-session',
            })
        raise ConnectionError('fixture broker connection lost')

    assert dispatch_operation(operation, SimpleNamespace(apply_async=publish_then_raise)) is False
    session.refresh_from_db()
    operation.refresh_from_db()
    device = AppDevice.objects.get(pk=context.device.pk)
    if claim_status == 'queued':
        assert (operation.status, operation.error_code) == ('failed', 'dispatch_failed_before_device_io')
        assert session.status == 'failed' and session.closed_at is not None
        assert not device.device_action_pending
        create_driver = Mock()
        monkeypatch.setattr(appium_tasks, 'create_driver', create_driver)
        # The broker can deliver its copy after the publisher has failed the queued row.
        appium_tasks.start_appium_session_task.run(str(operation.pk))
        create_driver.assert_not_called()
        assert release_device(device.pk, context.user, context.lease_token)
    else:
        assert operation.status == claim_status
        assert session.status == ('active' if claim_status == 'succeeded' else 'starting')
        assert device.device_action_pending


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('action', ['tree', 'click'])
def test_failed_publish_does_not_poison_an_active_session(appium_expiry_context, action):
    session, operation = _make_active_session(appium_expiry_context, operation_action=action)
    publish = Mock(side_effect=ConnectionError('fixture broker connection lost'))
    assert not dispatch_operation(operation, SimpleNamespace(apply_async=publish))
    session.refresh_from_db()
    operation.refresh_from_db()
    assert session.status == 'active'
    assert operation.status == 'failed'
    assert AppDevice.objects.get(pk=session.device_id).device_action_pending
    assert fenced_session_snapshot(session.pk, operation.pk) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('first_publish_fails', [False, True])
def test_expired_unclaimed_close_can_be_republished_more_than_once(
    appium_expiry_context, monkeypatch, first_publish_fails,
):
    context = appium_expiry_context
    session, operation = _make_active_session(context, operation_action='close', session_status='closing')
    _expire_lease(context.device)
    clock = [timezone.now()]
    monkeypatch.setattr(timezone, 'now', lambda: clock[0])
    publish = _fake_async_publish(monkeypatch)
    if first_publish_fails:
        publish.side_effect = [ConnectionError('fixture broker connection lost'), None, None]
    driver = Mock()
    monkeypatch.setattr(appium_tasks, 'attach_driver', Mock(return_value=driver))

    close_expired_interactive_sessions()
    assert publish.call_count == 1
    for attempt in (2, 3):
        close_expired_interactive_sessions()
        assert publish.call_count == attempt - 1
        clock[0] += CLOSE_REPUBLISH_INTERVAL + timedelta(seconds=1)
        close_expired_interactive_sessions()
        assert publish.call_count == attempt
    assert all(call.kwargs['task_id'] == str(operation.pk) for call in publish.call_args_list)
    assert AppiumOperation.objects.filter(session=session, action='close').count() == 1
    assert AppDevice.objects.get(pk=context.device.pk).device_action_pending

    appium_tasks.close_appium_session_task.run(str(operation.pk))
    appium_tasks.close_appium_session_task.run(str(operation.pk))
    session.refresh_from_db()
    operation.refresh_from_db()
    assert session.status == 'closed'
    assert operation.status == 'succeeded'
    assert not AppDevice.objects.get(pk=context.device.pk).device_action_pending
    driver.quit.assert_called_once_with()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("event_order", ["worker-first", "sweeper-first"])
def test_expired_queued_start_settles_identically_in_both_event_orders(
    appium_expiry_context, monkeypatch, event_order,
):
    context = appium_expiry_context
    publish_close = _fake_async_publish(monkeypatch)
    create_driver = Mock()
    attach_driver = Mock()
    monkeypatch.setattr(appium_tasks, "create_driver", create_driver)
    monkeypatch.setattr(appium_tasks, "attach_driver", attach_driver)
    session, operation = _reserve_start(context)
    _expire_lease(context.device)

    if event_order == "worker-first":
        appium_tasks.start_appium_session_task.run(str(operation.pk))
        close_expired_interactive_sessions()
    else:
        close_expired_interactive_sessions()
        appium_tasks.start_appium_session_task.run(str(operation.pk))

    session.refresh_from_db()
    operation.refresh_from_db()
    device = AppDevice.objects.get(pk=context.device.pk)
    assert operation.status == "failed"
    assert operation.error_code == "start_expired_before_device_io"
    assert session.status == "failed"
    assert session.closed_at is not None
    assert device.device_action_pending is False
    assert AppiumOperation.objects.filter(session=session, action="close").count() == 0
    create_driver.assert_not_called()
    attach_driver.assert_not_called()
    publish_close.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("action", ["screenshot", "click"])
@pytest.mark.parametrize("event_order", ["worker-first", "sweeper-first"])
def test_expired_queued_operation_is_failed_and_one_verified_close_is_scheduled(
    appium_expiry_context, monkeypatch, action, event_order,
):
    context = appium_expiry_context
    publish_close = _fake_async_publish(monkeypatch)
    attach_driver = Mock(return_value=Mock(session_id="fake-remote-session"))
    quit_driver = Mock()
    monkeypatch.setattr(appium_tasks, "attach_driver", attach_driver)
    monkeypatch.setattr(appium_tasks, "quit_driver", quit_driver)
    session, operation = _make_active_session(context, operation_action=action)
    dispatch_operation(operation, SimpleNamespace(apply_async=Mock()))
    _expire_lease(context.device)

    if event_order == "worker-first":
        appium_tasks.run_appium_operation_task.run(str(operation.pk))
        close_expired_interactive_sessions()
    else:
        close_expired_interactive_sessions()
        appium_tasks.run_appium_operation_task.run(str(operation.pk))

    session.refresh_from_db()
    operation.refresh_from_db()
    device = AppDevice.objects.get(pk=context.device.pk)
    close_operations = list(AppiumOperation.objects.filter(session=session, action="close"))
    assert operation.status == "failed"
    assert operation.error_code == "operation_expired_before_device_io"
    assert session.status == "closing"
    assert device.device_action_pending is True
    assert len(close_operations) == 1
    close_operation = close_operations[0]
    assert close_operation.status == "queued"
    publish_close.assert_called_once()
    attach_driver.assert_not_called()
    quit_driver.assert_not_called()

    appium_tasks.close_appium_session_task.run(str(close_operation.pk))

    session.refresh_from_db()
    close_operation.refresh_from_db()
    device.refresh_from_db()
    assert close_operation.status == "succeeded"
    assert session.status == "closed"
    assert session.remote_session_id == ""
    assert device.device_action_pending is False
    attach_driver.assert_called_once_with(session)
    quit_driver.assert_called_once_with(
        attach_driver.return_value,
        expected_endpoint=session.appium_endpoint,
        expected_session_id="fake-remote-session",
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("event_order", ["worker-first", "sweeper-first"])
def test_expired_closing_queued_close_is_reused_until_teardown_is_verified(
    appium_expiry_context, monkeypatch, event_order,
):
    context = appium_expiry_context
    publish_close = _fake_async_publish(monkeypatch)
    attach_driver = Mock(return_value=Mock(session_id="fake-remote-session"))
    quit_driver = Mock()
    monkeypatch.setattr(appium_tasks, "attach_driver", attach_driver)
    monkeypatch.setattr(appium_tasks, "quit_driver", quit_driver)
    session, operation = _make_active_session(
        context, operation_action="close", session_status="closing",
    )
    dispatch_operation(operation, appium_tasks.close_appium_session_task)
    operation.dispatched_at = timezone.now() - timedelta(seconds=2)
    operation.save(update_fields=["dispatched_at", "updated_at"])
    _expire_lease(context.device)

    if event_order == "worker-first":
        appium_tasks.close_appium_session_task.run(str(operation.pk))
        close_expired_interactive_sessions()
    else:
        close_expired_interactive_sessions()
        session.refresh_from_db()
        operation.refresh_from_db()
        device = AppDevice.objects.get(pk=context.device.pk)
        assert operation.status == "queued"
        assert session.status == "closing"
        assert device.device_action_pending is True
        publish_close.assert_called_with(
            args=[str(operation.pk)], task_id=str(operation.pk), queue="app_interactive",
        )
        appium_tasks.close_appium_session_task.run(str(operation.pk))

    session.refresh_from_db()
    operation.refresh_from_db()
    device = AppDevice.objects.get(pk=context.device.pk)
    assert AppiumOperation.objects.filter(session=session, action="close").count() == 1
    assert operation.status == "succeeded"
    assert session.status == "closed"
    assert device.device_action_pending is False
    attach_driver.assert_called_once_with(session)
    quit_driver.assert_called_once_with(
        attach_driver.return_value,
        expected_endpoint=session.appium_endpoint,
        expected_session_id="fake-remote-session",
    )
    if event_order == "worker-first":
        publish_close.assert_called_once()
    else:
        assert publish_close.call_count == 2


@pytest.mark.django_db(transaction=True)
def test_expired_claimed_operation_keeps_recovery_fence_and_is_not_closed(
    appium_expiry_context, monkeypatch,
):
    context = appium_expiry_context
    publish_close = _fake_async_publish(monkeypatch)
    session, operation = _make_active_session(context, operation_action="click")
    operation.status = "running"
    operation.dispatched_at = timezone.now()
    operation.save(update_fields=["status", "dispatched_at", "updated_at"])
    _expire_lease(context.device)

    close_expired_interactive_sessions()

    session.refresh_from_db()
    operation.refresh_from_db()
    device = AppDevice.objects.get(pk=context.device.pk)
    assert session.status == "active"
    assert operation.status == "running"
    assert device.device_action_pending is True
    assert AppiumOperation.objects.filter(session=session, action="close").count() == 0
    publish_close.assert_not_called()
    with pytest.raises(DeviceLeaseError):
        release_device(device.pk, context.user, context.lease_token)


def _additional_leased_context(context, device_suffix):
    device = AppDevice.objects.create(
        name=f"Expiry fixture {device_suffix}", device_id=f"expiry-extra-{device_suffix}",
        status="locked", locked_by=context.user, lease_generation=1,
    )
    return SimpleNamespace(user=context.user, device=device, project=context.project, package=context.package)


@pytest.mark.django_db(transaction=True)
def test_default_sweep_reaches_idle_session_after_fifty_claimed_operations(appium_expiry_context, monkeypatch):
    publish_close = _fake_async_publish(monkeypatch)
    for index in range(50):
        context = _additional_leased_context(appium_expiry_context, index)
        _session, operation = _make_active_session(context, operation_action="tree")
        operation.status = "running"
        operation.save(update_fields=["status", "updated_at"])
        _expire_lease(context.device)
    idle_context = _additional_leased_context(appium_expiry_context, "idle")
    idle_session, _operation = _make_active_session(idle_context)
    _expire_lease(idle_context.device)

    close_expired_interactive_sessions()

    idle_session.refresh_from_db()
    close_operation = idle_session.operations.get(action="close")
    assert idle_session.status == "closing"
    publish_close.assert_called_once_with(
        args=[str(close_operation.pk)], task_id=str(close_operation.pk), queue="app_interactive",
    )
    assert AppiumOperation.objects.filter(action="tree", status="running").count() == 50


@pytest.mark.django_db(transaction=True)
def test_expired_close_republication_rotates_when_sweep_limit_is_reached(appium_expiry_context, monkeypatch):
    now = timezone.now()
    clock = [now]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    publish_close = _fake_async_publish(monkeypatch)
    operations = []
    for index in range(2):
        context = _additional_leased_context(appium_expiry_context, index)
        _session, operation = _make_active_session(context, operation_action="close", session_status="closing")
        operation.dispatched_at = now - CLOSE_REPUBLISH_INTERVAL - timedelta(seconds=2 - index)
        operation.save(update_fields=["dispatched_at", "updated_at"])
        _expire_lease(context.device)
        operations.append(operation)

    close_expired_interactive_sessions(max_sessions=1)
    # Both closes are still queued and due, but the first was just given a turn.
    clock[0] += CLOSE_REPUBLISH_INTERVAL + timedelta(seconds=1)
    close_expired_interactive_sessions(max_sessions=1)

    assert [call.kwargs['task_id'] for call in publish_close.call_args_list] == [str(op.pk) for op in operations]


@pytest.mark.django_db(transaction=True)
def test_expired_worker_with_replaced_epoch_is_quarantined_without_dispatch(
    appium_expiry_context, monkeypatch,
):
    context = appium_expiry_context
    publish_close = _fake_async_publish(monkeypatch)
    attach_driver = Mock()
    session, operation = _make_active_session(context, operation_action="screenshot")
    AppDevice.objects.filter(pk=context.device.pk).update(
        device_action_epoch=session.device_action_epoch + 1,
    )
    _expire_lease(context.device)

    appium_tasks.run_appium_operation_task.run(str(operation.pk))
    close_expired_interactive_sessions()

    session.refresh_from_db()
    operation.refresh_from_db()
    device = AppDevice.objects.get(pk=context.device.pk)
    assert operation.status == "unknown"
    assert operation.error_code == "ownership_fence_changed"
    assert session.status == "active"
    assert device.device_action_pending is True
    assert AppiumOperation.objects.filter(session=session, action="close").count() == 0
    attach_driver.assert_not_called()
    publish_close.assert_not_called()
    with pytest.raises(DeviceLeaseError):
        release_device(device.pk, context.user, context.lease_token)
