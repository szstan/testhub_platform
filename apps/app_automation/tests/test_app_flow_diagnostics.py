from pathlib import Path

import pytest
from rest_framework.test import APIClient

from apps.app_automation.app_flow_diagnostic_models import AppFlowFailure
from apps.app_automation.app_flow_diagnostics import (
    FlowFailureValidationError,
    record_flow_failure,
)
from apps.app_automation.models import AppDevice, AppProject, AppTestCase, AppTestExecution
from apps.users.models import User


PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def diagnostic_execution(db, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    owner = User.objects.create_user(username="flow-diagnostic-owner")
    outsider = User.objects.create_user(username="flow-diagnostic-outsider")
    project = AppProject.objects.create(name="diagnostic project", owner=owner)
    case = AppTestCase.objects.create(
        project=project,
        created_by=owner,
        name="diagnostic case",
        ui_flow=[{"type": "tap", "name": "Continue"}],
    )
    device = AppDevice.objects.create(device_id="flow-diagnostic-device", status="online")
    execution = AppTestExecution.objects.create(
        test_case=case,
        device=device,
        user=owner,
        status="error",
        result="failed",
    )
    AppTestExecution.objects.filter(pk=execution.pk).update(engine="appium")
    execution.refresh_from_db()
    return owner, outsider, project, case, device, execution


def create_screenshot(settings, relative="owner-hash/failure.png"):
    root = Path(settings.MEDIA_ROOT) / "app-automation" / "screenshots"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_HEADER + b"synthetic image data")
    return target


@pytest.mark.django_db
def test_record_flow_failure_validates_and_redacts_runner_fields(diagnostic_execution):
    _owner, _outsider, _project, case, _device, execution = diagnostic_execution

    failure = record_flow_failure(
        execution.pk,
        step_name="Tap Continue password=top-secret",
        step_path="ui_flow[3].if_steps[2]",
        code="element_not_found",
        duration_ms=250,
        match_count=0,
    )

    assert failure.step_name == "Tap Continue password=[REDACTED]"
    assert failure.step_path == "ui_flow[3].if_steps[2]"
    assert failure.step_index == 2
    assert failure.case_revision == case.updated_at.isoformat()
    assert failure.engine == "appium"
    assert failure.match_count == 0

    with pytest.raises(FlowFailureValidationError):
        record_flow_failure(
            execution.pk,
            step_name="Tap",
            step_path="ui_flow[0]",
            code="exception: private app credential",
            duration_ms=1,
        )
    with pytest.raises(FlowFailureValidationError):
        record_flow_failure(
            execution.pk,
            step_name="Tap",
            step_path="ui_flow[0]",
            code="execution_failed",
            duration_ms=True,
        )
    with pytest.raises(FlowFailureValidationError):
        record_flow_failure(
            execution.pk,
            step_name="Tap",
            step_path="ui_flow[0]",
            code="execution_failed",
            duration_ms=1,
            match_count=True,
        )


@pytest.mark.django_db
def test_first_flow_failure_is_immutable_and_invalid_paths_are_not_retained(diagnostic_execution):
    _owner, _outsider, _project, _case, _device, execution = diagnostic_execution
    first = record_flow_failure(
        execution.pk,
        step_name="Initial step",
        step_path="ui_flow[1]",
        code="assertion_failed",
        duration_ms=25,
    )
    second = record_flow_failure(
        execution.pk,
        step_name="Later step",
        step_path="../../private/token",
        code="device_disconnected",
        duration_ms=50,
    )

    assert second.pk == first.pk
    assert second.code == "assertion_failed"
    assert second.step_name == "Initial step"
    assert second.step_index == 0
    assert AppFlowFailure.objects.filter(execution=execution).count() == 1


@pytest.mark.django_db
def test_execution_detail_returns_revision_classification_state_and_scoped_evidence(
    diagnostic_execution, settings,
):
    owner, outsider, _project, case, _device, execution = diagnostic_execution
    screenshot_path = create_screenshot(settings)
    record_flow_failure(
        execution.pk,
        step_name="Generate report",
        step_path="ui_flow[1]",
        code="report_generation_failed",
        duration_ms=900,
        screenshot_path=str(screenshot_path),
    )
    case.name = "updated after the failure"
    case.save(update_fields=["name", "updated_at"])
    execution.status = "running"
    execution.stop_requested = True
    execution.save(update_fields=["status", "stop_requested", "updated_at"])

    owner_client = APIClient()
    owner_client.force_authenticate(owner)
    detail = owner_client.get(f"/api/app-automation/executions/{execution.pk}/")

    assert detail.status_code == 200
    assert detail.data["case_revision"] == case.updated_at.isoformat()
    assert detail.data["flow_failure"]["classification"] == "report"
    assert detail.data["error_classification"] == "report"
    assert detail.data["flow_failure"]["case_revision_changed"] is True
    assert detail.data["flow_failure"]["current_case_revision"] == case.updated_at.isoformat()
    assert detail.data["execution_state"] == "stopping"
    assert detail.data["stop_requested"] is True
    assert detail.data["device_action_pending"] is False
    assert detail.data["flow_failure"]["has_screenshot"] is True
    assert detail.data["flow_failure"]["screenshot_url"].endswith(
        f"/executions/{execution.pk}/flow-failure-evidence/"
    )
    assert "screenshot_path" not in str(detail.data)
    assert str(settings.MEDIA_ROOT) not in str(detail.data)

    evidence = owner_client.get(detail.data["flow_failure"]["screenshot_url"])
    assert evidence.status_code == 200
    assert evidence["Content-Type"] == "image/png"
    assert b"synthetic image data" in b"".join(evidence.streaming_content)

    execution.device_action_pending = True
    execution.stop_requested = False
    execution.save(update_fields=["device_action_pending", "stop_requested", "updated_at"])
    pending_recovery = owner_client.get(f"/api/app-automation/executions/{execution.pk}/")
    assert pending_recovery.data["execution_state"] == "device_recovery_pending"
    assert pending_recovery.data["device_action_pending"] is True

    execution.status = "pending"
    execution.device_action_pending = False
    execution.save(update_fields=["status", "device_action_pending", "updated_at"])
    pending_execution = owner_client.get(f"/api/app-automation/executions/{execution.pk}/")
    assert pending_execution.data["execution_state"] == "pending"

    outsider_client = APIClient()
    outsider_client.force_authenticate(outsider)
    assert outsider_client.get(f"/api/app-automation/executions/{execution.pk}/").status_code == 404
    assert outsider_client.get(detail.data["flow_failure"]["screenshot_url"]).status_code == 404


@pytest.mark.django_db
def test_screenshot_outside_confined_root_is_omitted(diagnostic_execution, settings, tmp_path):
    _owner, _outsider, _project, _case, _device, execution = diagnostic_execution
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_HEADER + b"must not be exposed")
    failure = record_flow_failure(
        execution.pk,
        step_name="Tap",
        step_path="ui_flow[1]",
        code="element_ambiguous",
        duration_ms=30,
        match_count=2,
        screenshot_path=str(outside),
    )

    assert failure.screenshot_path == ""
    assert AppFlowFailure.objects.filter(pk=failure.pk).exists()
