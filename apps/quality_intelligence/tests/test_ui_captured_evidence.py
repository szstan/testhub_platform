from __future__ import annotations

import base64
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from apps.quality_intelligence.adapters.ui import (
    UiCaseSourceAdapter,
    UiSourceAdapter,
    _add_case_evidence,
)
from apps.ui_automation.evidence import from_data_url, project_screenshots
from apps.ui_automation.models import (
    TestCase as UiTestCase,
    TestCaseExecution as UiTestCaseExecution,
    TestExecution as UiTestExecution,
    TestSuite as UiTestSuite,
    UiProject,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def ui_context():
    user = get_user_model().objects.create_user(username="ui-evidence-owner")
    project = UiProject.objects.create(
        name="UI evidence project",
        base_url="https://example.test",
        owner=user,
    )
    suite = UiTestSuite.objects.create(project=project, name="Evidence suite")
    case = UiTestCase.objects.create(
        name="Evidence case",
        project=project,
        created_by=user,
    )
    return user, project, suite, case


def _projected_screenshot(payload: bytes, *, execution_id: int, item_id: int):
    captured_at = "2026-10-31T00:00:00+00:00"
    data_url = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
    screenshot = project_screenshots(
        [{"url": data_url, "description": "checkout screenshot", "timestamp": captured_at}],
        producer="ui_views.run_case",
        execution_id=execution_id,
        item_id=item_id,
    )[0]
    return screenshot


def _assert_captured_reference(evidence, reference):
    metadata = reference["metadata"]
    assert evidence.sha256 == reference["sha256"]
    assert evidence.size_bytes == reference["size_bytes"]
    assert evidence.mime_type == reference["mime_type"]
    assert evidence.source_uri == reference["uri"]
    assert evidence.availability == "AVAILABLE"
    assert evidence.provenance == metadata
    assert evidence.captured_at.isoformat() == metadata["captured_at"]


def test_suite_and_case_adapters_preserve_projected_screenshot_reference(ui_context):
    user, project, suite, case = ui_context
    screenshot = _projected_screenshot(b"deterministic PNG bytes", execution_id=31, item_id=case.pk)
    reference = screenshot["evidence_reference"]
    suite_execution = UiTestExecution.objects.create(
        project=project,
        test_suite=suite,
        status="SUCCESS",
        result_data={
            "test_cases": [
                {
                    "test_case_id": case.pk,
                    "test_case_name": case.name,
                    "status": "passed",
                    "screenshots": [screenshot],
                }
            ]
        },
    )
    case_execution = UiTestCaseExecution.objects.create(
        test_case=case,
        project=project,
        status="passed",
        created_by=user,
        screenshots=[screenshot],
    )

    # The adapter consumes only the producer's metadata; it does not decode the data URL.
    with patch("apps.ui_automation.evidence.from_data_url", side_effect=AssertionError("consumer decoded data URL")):
        suite_run = UiSourceAdapter().read(suite_execution.pk)
        case_run = UiCaseSourceAdapter().read(case_execution.pk)

    suite_evidence = next(item for item in suite_run.evidence if item.evidence_type == "SCREENSHOT")
    case_evidence = next(item for item in case_run.evidence if item.evidence_type == "SCREENSHOT")
    _assert_captured_reference(suite_evidence, reference)
    _assert_captured_reference(case_evidence, reference)


def test_ai_recording_sequence_reference_uses_same_content_addressed_consumer(ui_context):
    _user, project, suite, _case = ui_context
    captured_at = "2026-10-31T00:00:00+00:00"
    payload = b"GIF89a deterministic animation"
    data_url = "data:image/gif;base64," + base64.b64encode(payload).decode("ascii")
    reference = from_data_url(
        data_url,
        evidence_type="RECORDING",
        metadata={
            "producer": "ui_ai_execution.gif",
            "execution_id": "55",
            "item_id": "13",
            "step_number": None,
            "captured_at": captured_at,
        },
    ).to_contract()
    result = {
        "test_case_id": 13,
        "status": "passed",
        "screenshots_sequence": [
            {
                "description": "AI execution recording",
                "timestamp": captured_at,
                "evidence_reference": reference,
            }
        ],
    }
    execution = UiTestExecution.objects.create(
        project=project,
        test_suite=suite,
        status="SUCCESS",
        result_data={"test_cases": [result]},
    )

    with patch("apps.ui_automation.evidence.from_data_url", side_effect=AssertionError("consumer decoded data URL")):
        normalized = UiSourceAdapter().read(execution.pk)

    assert len(normalized.evidence) >= 1
    _assert_captured_reference(normalized.evidence[0], reference)
    assert normalized.evidence[0].item_key == "case:13"


def test_malformed_reference_and_unreferenced_data_url_fail_closed():
    malformed = {
        "description": "untrusted screenshot",
        "url": "data:image/png;base64,not-decoded-by-consumer",
        "evidence_reference": {
            "evidence_type": "SCREENSHOT",
            "sha256": "not-a-sha256",
            "size_bytes": 12,
            "mime_type": "image/png",
            "uri": "urn:sha256:not-a-sha256",
            "availability": "AVAILABLE",
            "metadata": {"producer": "untrusted"},
        },
    }
    legacy_data_url = {
        "description": "legacy screenshot",
        "url": "data:image/png;base64,not-decoded-by-consumer",
    }
    target = []
    _add_case_evidence(
        target,
        item_key="case:malformed",
        screenshots=[malformed, legacy_data_url],
    )

    assert len(target) == 2
    assert all(item.availability == "UNAVAILABLE" for item in target)
    assert all(item.sha256 == "" for item in target)
    assert all(item.size_bytes is None for item in target)
    assert all(item.source_uri == "" for item in target)


def test_historical_screenshot_reference_fallback_remains_available():
    target = []
    _add_case_evidence(
        target,
        item_key="case:legacy",
        screenshots=[{"path": "/media/ui_screenshots/legacy.png", "description": "legacy"}],
    )

    assert len(target) == 1
    assert target[0].source_uri == "/media/ui_screenshots/legacy.png"
    assert target[0].source_field == "screenshots"
    assert target[0].availability == "AVAILABLE"
    assert target[0].sha256


def test_invalid_capture_timestamp_is_omitted():
    target = []
    _add_case_evidence(
        target,
        item_key="case:timestamp",
        screenshots=[{"path": "/media/screenshot.png", "timestamp": "not-a-timestamp"}],
    )

    assert len(target) == 1
    assert target[0].captured_at is None


def test_reference_captured_timestamp_accepts_datetime_values():
    captured_at = datetime(2026, 10, 31, tzinfo=timezone.utc)
    target = []
    _add_case_evidence(
        target,
        item_key="case:timestamp-datetime",
        screenshots=[
            {
                "description": "timestamped",
                "timestamp": captured_at,
                "evidence_reference": {
                    "evidence_type": "SCREENSHOT",
                    "sha256": "a" * 64,
                    "size_bytes": 1,
                    "mime_type": "image/png",
                    "uri": "urn:sha256:" + "a" * 64,
                    "availability": "AVAILABLE",
                    "metadata": {"captured_at": captured_at},
                },
            }
        ],
    )

    assert target[0].captured_at == captured_at
