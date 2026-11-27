"""Validated persistence and authorization helpers for flow-failure evidence."""

from __future__ import annotations

import os
import re
from pathlib import Path

from django.conf import settings
from django.db import transaction

from apps.quality_intelligence.services.redaction import redact_text

from .app_flow_diagnostic_models import AppFlowFailure, FLOW_FAILURE_CODE_CHOICES
from .models import AppTestExecution


FLOW_FAILURE_CODES = frozenset(code for code, _label in FLOW_FAILURE_CODE_CHOICES)
FAILURE_CLASSIFICATIONS = {
    "element_not_found": "locator",
    "locator_not_found": "locator",
    "element_ambiguous": "locator_ambiguity",
    "locator_ambiguous": "locator_ambiguity",
    "element_state_mismatch": "locator_state",
    "assertion_failed": "assertion",
    "read_only_business_assertion_failed": "assertion",
    "device_disconnected": "device_connection",
    "appium_read_failed": "device_connection",
    "appium_outcome_unknown": "device_action_uncertain",
    "appium_unavailable": "appium_setup",
    "appium_configuration_invalid": "appium_setup",
    "appium_client_unavailable": "appium_setup",
    "report_generation_failed": "report",
    "execution_failed": "execution",
    "step_failed": "execution",
}

_MAX_DURATION_MS = 86_400_000
_MAX_MATCH_COUNT = 1_000_000
_MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
_STEP_PATH_RE = re.compile(
    r"^ui_flow(?:\.steps)?(?:\[(?:0|[1-9][0-9]{0,5})\])?"
    r"(?:\.[\w -]{1,64}(?:\[(?:0|[1-9][0-9]{0,5})\])?)*$",
    re.UNICODE,
)
_ROOT_STEP_INDEX_RE = re.compile(r"^ui_flow(?:\.steps)?\[(\d{1,6})\]")


class FlowFailureValidationError(ValueError):
    """Raised when a runner supplies an invalid diagnostic value."""


def _plain_text(value, *, field, max_length, allow_empty=False):
    if not isinstance(value, str):
        raise FlowFailureValidationError(f"{field} must be text")
    text = "".join(char for char in value if char.isprintable()).strip()
    text = redact_text(text, max_length=max_length)
    if not text and not allow_empty:
        raise FlowFailureValidationError(f"{field} is required")
    if len(text) > max_length:
        raise FlowFailureValidationError(f"{field} is too long")
    return text


def _sanitize_step_path(value):
    path = _plain_text(value, field="step_path", max_length=512, allow_empty=True)
    if not path:
        return "", None
    if not _STEP_PATH_RE.fullmatch(path):
        return "", None
    match = _ROOT_STEP_INDEX_RE.match(path)
    index = int(match.group(1)) - 1 if match else None
    return path, index if index is None or index >= 0 else None


def _screenshots_root():
    return os.path.realpath(os.path.join(settings.MEDIA_ROOT, "app-automation", "screenshots"))


def _confined_screenshot_file(path):
    """Return a confined, small PNG path or None; never trust caller paths."""
    if not isinstance(path, str) or not path or len(path) > 4096:
        return None
    root = _screenshots_root()
    candidate = path if os.path.isabs(path) else os.path.join(root, path)
    resolved = os.path.realpath(candidate)
    try:
        if os.path.commonpath((root, resolved)) != root:
            return None
    except ValueError:
        return None
    try:
        if not os.path.isfile(resolved) or os.path.getsize(resolved) > _MAX_SCREENSHOT_BYTES:
            return None
        if Path(resolved).suffix.lower() != ".png":
            return None
        with open(resolved, "rb") as screenshot:
            if screenshot.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
    except OSError:
        return None
    return resolved


def _screenshot_reference(path):
    resolved = _confined_screenshot_file(path)
    if resolved is None:
        return ""
    return os.path.relpath(resolved, _screenshots_root()).replace(os.sep, "/")


def resolve_flow_failure_screenshot(failure):
    """Resolve a stored relative screenshot reference without exposing it."""
    if not failure or not failure.screenshot_path:
        return None
    return _confined_screenshot_file(failure.screenshot_path)


def _validate_integer(value, *, field, maximum, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise FlowFailureValidationError(f"{field} must be an integer from 0 to {maximum}")
    return value


def record_flow_failure(
    execution_id,
    *,
    step_name,
    step_path,
    code,
    duration_ms,
    match_count=None,
    screenshot_path=None,
    case_revision=None,
):
    """Persist only the first allowlisted flow failure for an execution."""
    if not isinstance(code, str) or code not in FLOW_FAILURE_CODES:
        raise FlowFailureValidationError("code is not an allowlisted flow failure")
    safe_name = _plain_text(step_name, field="step_name", max_length=160, allow_empty=True)
    safe_path, step_index = _sanitize_step_path(step_path)
    safe_duration = _validate_integer(duration_ms, field="duration_ms", maximum=_MAX_DURATION_MS)
    safe_matches = _validate_integer(
        match_count, field="match_count", maximum=_MAX_MATCH_COUNT, nullable=True,
    )

    with transaction.atomic():
        execution = (
            AppTestExecution.objects.select_for_update()
            .select_related("test_case")
            .get(pk=execution_id)
        )
        existing = AppFlowFailure.objects.filter(execution_id=execution.pk).first()
        if existing is not None:
            return existing
        if case_revision is None:
            case_revision = execution.test_case.updated_at.isoformat() if execution.test_case_id else ""
        return AppFlowFailure.objects.create(
            execution=execution,
            code=code,
            step_name=safe_name,
            step_path=safe_path,
            step_index=step_index,
            case_revision=case_revision[:64],
            engine=execution.engine or "",
            duration_ms=safe_duration,
            match_count=safe_matches,
            screenshot_path=_screenshot_reference(screenshot_path),
        )


def serialize_flow_failure(execution):
    """Build the execution-detail payload without exposing filesystem paths."""
    failure = AppFlowFailure.objects.filter(execution_id=execution.pk).first()
    current_revision = execution.test_case.updated_at.isoformat() if execution.test_case_id else ""
    if failure is None:
        return None
    has_screenshot = resolve_flow_failure_screenshot(failure) is not None
    return {
        "id": failure.pk,
        "code": failure.code,
        "classification": FAILURE_CLASSIFICATIONS.get(failure.code, "execution"),
        "step_name": failure.step_name,
        "step_path": failure.step_path,
        "step_index": failure.step_index,
        "engine": failure.engine,
        "duration_ms": failure.duration_ms,
        "match_count": failure.match_count,
        "case_revision": failure.case_revision,
        "current_case_revision": current_revision,
        "case_revision_changed": bool(
            failure.case_revision and current_revision and failure.case_revision != current_revision
        ),
        "has_screenshot": has_screenshot,
        "created_at": failure.created_at.isoformat(),
    }
