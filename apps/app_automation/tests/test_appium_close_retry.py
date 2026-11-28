"""Retry confirmed-child cleanup without releasing ownership on uncertainty."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.app_automation import appium_execution
from apps.app_automation.appium_models import AppiumSession
from apps.app_automation.models import AppTestCase, AppTestExecution
from apps.app_automation.tests.test_appium_sessions_integration import appium_device


pytestmark = pytest.mark.django_db(transaction=True)


def owned_execution(fixture, monkeypatch):
    case = AppTestCase.objects.create(
        project=fixture.project, app_package=fixture.package, created_by=fixture.user, name='Retry close fixture',
    )
    execution = AppTestExecution.objects.create(
        test_case=case, user=fixture.user, device=fixture.device, status='running',
        device_action_pending=True, device_lease_generation=fixture.device.lease_generation,
    )
    driver = Mock(session_id='retry-owned-session')
    monkeypatch.setattr(appium_execution, 'create_driver', Mock(return_value=driver))
    handle = appium_execution.start_execution_session(execution.pk, fixture.device.device_id, fixture.package.package_name)
    attached = Mock(return_value=driver)
    monkeypatch.setattr(appium_execution, 'attach_driver', attached)
    return execution, handle, attached


def test_confirmed_reap_retries_transient_close_and_releases_only_after_success(appium_device, monkeypatch):
    execution, handle, attach = owned_execution(appium_device, monkeypatch)
    close = Mock(side_effect=[appium_execution.AppiumOutcomeUnknown('transient timeout'), None])
    monkeypatch.setattr(appium_execution, 'quit_driver', close)
    assert appium_execution.finish_execution_session(handle, child_exit_confirmed=True)
    assert close.call_count == 2
    assert attach.call_count == 2
    assert {call.args[0].remote_session_id for call in attach.call_args_list} == {'retry-owned-session'}
    session = AppiumSession.objects.get(pk=handle.id)
    assert session.status == 'closed'
    assert session.remote_session_id == ''
    execution.refresh_from_db()
    appium_device.device.refresh_from_db()
    assert not execution.device_action_pending
    assert not appium_device.device.device_action_pending


def test_exhausted_close_retries_keep_both_action_markers(appium_device, monkeypatch):
    execution, handle, attach = owned_execution(appium_device, monkeypatch)
    close = Mock(side_effect=appium_execution.AppiumOutcomeUnknown('timeout'))
    monkeypatch.setattr(appium_execution, 'quit_driver', close)
    assert not appium_execution.finish_execution_session(handle, child_exit_confirmed=True)
    assert attach.call_count == close.call_count == 2
    assert AppiumSession.objects.get(pk=handle.id).status == 'unknown'
    execution.refresh_from_db()
    appium_device.device.refresh_from_db()
    assert execution.device_action_pending
    assert appium_device.device.device_action_pending


def test_retry_revalidates_lease_before_sending_another_delete(appium_device, monkeypatch):
    execution, handle, attach = owned_execution(appium_device, monkeypatch)

    def close_after_lease_changed(_driver):
        type(appium_device.device).objects.filter(pk=appium_device.device.pk).update(
            lease_generation=appium_device.device.lease_generation + 1,
        )
        raise appium_execution.AppiumOutcomeUnknown('reply lost')

    close = Mock(side_effect=close_after_lease_changed)
    monkeypatch.setattr(appium_execution, 'quit_driver', close)
    assert not appium_execution.finish_execution_session(handle, child_exit_confirmed=True)
    assert attach.call_count == close.call_count == 1
    execution.refresh_from_db()
    appium_device.device.refresh_from_db()
    assert execution.device_action_pending
    assert appium_device.device.device_action_pending


def test_close_attempts_share_a_fixed_total_deadline(appium_device, monkeypatch):
    _execution, handle, attach = owned_execution(appium_device, monkeypatch)
    clock = {'now': 10.0}
    monkeypatch.setattr(appium_execution, 'time', SimpleNamespace(monotonic=lambda: clock['now']))

    def deadline_consumed(_driver):
        clock['now'] = 20.0
        raise appium_execution.AppiumOutcomeUnknown('deadline exhausted')

    monkeypatch.setattr(appium_execution, 'quit_driver', Mock(side_effect=deadline_consumed))
    assert not appium_execution.finish_execution_session(handle, child_exit_confirmed=True)
    attach.assert_called_once()
    assert attach.call_args.kwargs['deadline'] == 15.0
