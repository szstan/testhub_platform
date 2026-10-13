"""Read-only adapters for UI suite and case executions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .base import (
    FAIL,
    INCONCLUSIVE,
    MAX_EVIDENCE,
    MAX_FAILURES,
    MAX_ITEMS,
    MAX_METRICS,
    PASS,
    SKIP,
    BaseSourceAdapter,
    NormalizedItem,
    NormalizedMetric,
    NormalizedRun,
    duration_ms,
    evidence,
    failure,
    finalize_run,
    load_by_id,
    normalize_outcome,
    outcome_for_items,
    payload,
    safe_number,
    safe_reference,
    text,
    value,
)


_SUITE_TERMINAL = frozenset({"SUCCESS", "FAILED", "ABORTED"})
_CASE_TERMINAL = frozenset({"PASSED", "FAILED", "ERROR"})


def _models():
    from apps.ui_automation.models import Screenshot, TestCaseExecution, TestExecution

    return Screenshot, TestCaseExecution, TestExecution


def _mapping_list(value_: Any, *keys: str) -> list[Mapping[str, Any]]:
    if isinstance(value_, list):
        return [item for item in value_ if isinstance(item, Mapping)]
    if isinstance(value_, Mapping):
        for key in keys:
            candidate = value_.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, Mapping)]
    return []


def _json_steps(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def _screenshot_rows(source: Any) -> list[Any]:
    try:
        manager = source.screenshots
        queryset = manager.all().order_by("captured_at")
        return list(queryset[:200])
    except Exception:
        return []


def _case_rows(TestCaseExecution: Any, source: Any) -> list[Any]:
    suite_id = value(value(source, "test_suite", default=None), "id", default=None)
    project_id = value(source, "project_id", default=None)
    if suite_id is None:
        return []
    try:
        filters = {"test_suite_id": suite_id}
        if project_id is not None:
            filters["project_id"] = project_id
        queryset = TestCaseExecution.objects.filter(**filters).order_by("created_at", "id")
        return list(queryset[:1000])
    except Exception:
        return []


def _case_context(case_execution: Any) -> dict[str, Any]:
    return {
        "id": value(case_execution, "id", default=None),
        "test_case_id": value(case_execution, "test_case_id", default=None),
        "status": value(case_execution, "status", default=""),
        "engine": value(case_execution, "engine", default=""),
        "browser": value(case_execution, "browser", default=""),
        "headless": value(case_execution, "headless", default=False),
        "execution_time": value(case_execution, "execution_time", default=None),
        "execution_logs": _json_steps(value(case_execution, "execution_logs", default="")),
        "error_message": value(case_execution, "error_message", default=""),
        "screenshots": value(case_execution, "screenshots", default=[]),
        "started_at": value(case_execution, "started_at", default=None),
        "finished_at": value(case_execution, "finished_at", default=None),
    }


def _add_case_evidence(
    target: list[Any],
    *,
    item_key: str,
    logs: Any = None,
    screenshots: Any = None,
    error_message: Any = None,
) -> None:
    if logs not in (None, "", [], {}):
        target.append(evidence("LOG", item_key=item_key, snippet=payload(logs), source_field="execution_logs"))
    if error_message:
        target.append(evidence("LOG", item_key=item_key, snippet=error_message, source_field="error_message"))
    if isinstance(screenshots, list):
        for screenshot in screenshots[:100]:
            if isinstance(screenshot, Mapping):
                uri = screenshot.get("url") or screenshot.get("path") or screenshot.get("image")
                # Data URLs contain the complete image and are intentionally
                # represented as unavailable metadata rather than copied.
                if isinstance(uri, str) and uri.lower().startswith("data:"):
                    target.append(
                        evidence(
                            "SCREENSHOT",
                            item_key=item_key,
                            snippet=screenshot.get("description", "screenshot captured"),
                            source_field="screenshots",
                            availability="UNAVAILABLE",
                        )
                    )
                else:
                    target.append(
                        evidence(
                            "SCREENSHOT",
                            item_key=item_key,
                            snippet=screenshot.get("description", "screenshot captured"),
                            source_uri=uri,
                            source_field="screenshots",
                            captured_at=screenshot.get("timestamp"),
                        )
                    )
            elif screenshot:
                target.append(
                    evidence(
                        "SCREENSHOT",
                        item_key=item_key,
                        snippet="screenshot captured",
                        source_uri=screenshot,
                        source_field="screenshots",
                    )
                )


def _item_from_case(
    case_data: Mapping[str, Any],
    *,
    index: int,
    fallback: Any = None,
) -> tuple[NormalizedItem, Any, Any, Any]:
    case_id = case_data.get("test_case_id") or case_data.get("case_id") or case_data.get("id")
    if case_id is None and fallback is not None:
        case_id = value(fallback, "test_case_id", "id", default=None)
    name = case_data.get("test_case_name") or case_data.get("name")
    if not name and fallback is not None:
        name = value(value(fallback, "test_case", default=None), "name", default="")
    key = str(case_data.get("test_key") or (f"case:{case_id}" if case_id is not None else f"case:{index + 1}"))
    status = case_data.get("status") or (value(fallback, "status", default="") if fallback else "")
    outcome = normalize_outcome(status, passed=case_data.get("passed"), skipped=case_data.get("skipped"))
    logs = case_data.get("steps") or case_data.get("logs")
    screenshots = case_data.get("screenshots")
    error_message = case_data.get("error") or case_data.get("error_message")
    if fallback is not None:
        if logs is None:
            logs = _json_steps(value(fallback, "execution_logs", default=""))
        if screenshots is None:
            screenshots = value(fallback, "screenshots", default=[])
        if not error_message:
            error_message = value(fallback, "error_message", default="")
    execution_seconds = case_data.get("duration") or case_data.get("execution_time")
    if execution_seconds is None and fallback is not None:
        execution_seconds = value(fallback, "execution_time", default=None)
    details = {
        "steps": logs,
        "screenshots_count": len(screenshots) if isinstance(screenshots, list) else 0,
        "error": error_message,
    }
    item = NormalizedItem(
        test_key=key,
        outcome=outcome,
        source_item_type="CASE",
        source_item_id=str(case_id or value(fallback, "id", default="") if fallback else case_id or ""),
        name=text(name, 255),
        source_status=text(status, 64),
        duration_ms=duration_ms(None, None, execution_seconds),
        details=payload(details),
    )
    return item, logs, screenshots, error_message


class UiSourceAdapter(BaseSourceAdapter):
    """Normalize UI suite ``TestExecution`` and case evidence context."""

    source_type = "UI"
    source_scope = "SUITE"
    terminal_statuses = _SUITE_TERMINAL

    def normalize(self, execution_id: Any = None) -> NormalizedRun:
        execution_id = self._resolve_execution_id(execution_id)
        Screenshot, TestCaseExecution, TestExecution = _models()
        execution = load_by_id(TestExecution.objects, execution_id, source_type=self.source_type)
        status = self._ensure_terminal(execution, execution_id)
        result_data = value(execution, "result_data", default=None)
        case_results = _mapping_list(result_data, "test_cases", "cases", "results")
        case_rows = _case_rows(TestCaseExecution, execution)
        rows_by_case: dict[str, Any] = {}
        for row in case_rows:
            case_id = value(row, "test_case_id", default=None)
            if case_id is not None:
                rows_by_case.setdefault(str(case_id), row)
        warnings: list[str] = []
        if not case_results and not case_rows:
            warnings.append("ui_case_results_unavailable")

        items: list[NormalizedItem] = []
        normalized_evidence: list[Any] = []
        failures: list[Any] = []
        metrics: list[Any] = []
        result_snapshots: list[Any] = []
        consumed_rows: set[int] = set()
        for index, case_data in enumerate(case_results[: self.max_items]):
            case_id = case_data.get("test_case_id") or case_data.get("case_id") or case_data.get("id")
            row = rows_by_case.get(str(case_id)) if case_id is not None else None
            if row is not None:
                consumed_rows.add(id(row))
            item, logs, screenshots, error_message = _item_from_case(
                case_data, index=index, fallback=row
            )
            items.append(item)
            before = len(normalized_evidence)
            _add_case_evidence(
                normalized_evidence,
                item_key=item.test_key,
                logs=logs,
                screenshots=screenshots,
                error_message=error_message,
            )
            if len(normalized_evidence) == before:
                warnings.append(f"ui_item_evidence_unavailable:{item.test_key}")
            if item.outcome == FAIL:
                failures.append(
                    failure(error_message or "UI case failed", item_key=item.test_key, phase="case")
                )
            duration = safe_number(item.duration_ms)
            if duration is not None:
                metrics.append(
                    NormalizedMetric(
                        metric_key="duration_ms",
                        value=duration,
                        item_key=item.test_key,
                        sample_key=item.test_key,
                        unit="ms",
                    )
                )
            result_snapshots.append(payload(dict(case_data)))

        # The suite result payload is the only stable parent link. Child rows
        # have no FK to this suite execution, so use them as a fallback only
        # when the aggregate did not persist case results.
        if not case_results:
            for index, row in enumerate(case_rows[: self.max_items]):
                context = _case_context(row)
                item, logs, screenshots, error_message = _item_from_case(
                    context, index=index, fallback=row
                )
                items.append(item)
                _add_case_evidence(
                    normalized_evidence,
                    item_key=item.test_key,
                    logs=logs,
                    screenshots=screenshots,
                    error_message=error_message,
                )
                if item.outcome == FAIL:
                    failures.append(failure(error_message or "UI case failed", item_key=item.test_key, phase="case"))
                result_snapshots.append(payload(context))

        # Screenshot rows are linked to the suite execution. The image field is
        # metadata only; opening or reading the file would violate the adapter
        # boundary.
        suite_screenshots = _screenshot_rows(execution)
        for row in suite_screenshots:
            image = value(row, "image", default=None)
            image_name = value(image, "name", default=image)
            normalized_evidence.append(
                evidence(
                    "SCREENSHOT",
                    snippet=value(row, "description", "name", default="screenshot captured"),
                    source_uri=safe_reference(image_name),
                    source_field="screenshots.image",
                    captured_at=value(row, "captured_at", default=None),
                )
            )
        report_url = value(execution, "report_url", default="")
        if report_url:
            normalized_evidence.append(
                evidence("ARTIFACT", snippet="UI execution report", source_uri=report_url, source_field="report_url")
            )
        elif status in _SUITE_TERMINAL:
            warnings.append("ui_report_unavailable")

        outcome = outcome_for_items(items, status)
        if status == "FAILED" and outcome == INCONCLUSIVE:
            outcome = FAIL
        if status == "ABORTED" and not items:
            outcome = SKIP
        summary = payload(
            {
                "total": value(execution, "total_cases", default=0),
                "passed": value(execution, "passed_cases", default=0),
                "failed": value(execution, "failed_cases", default=0),
                "skipped": value(execution, "skipped_cases", default=0),
                "result_count": len(items),
                "outcome": outcome,
            }
        )
        project = value(execution, "project", default=None)
        suite = value(execution, "test_suite", default=None)
        script = value(execution, "test_script", default=None)
        source_snapshot = {
            "execution": {
                "id": value(execution, "id", default=execution_id),
                "status": status,
                "environment": value(execution, "environment", default=""),
                "engine": value(execution, "engine", default=""),
                "browser": value(execution, "browser", default=""),
                "headless": value(execution, "headless", default=False),
                "started_at": value(execution, "started_at", default=None),
                "finished_at": value(execution, "finished_at", default=None),
                "result_data": payload(result_data or {}),
                "report_url": safe_reference(report_url),
            },
            "suite": {
                "id": value(suite, "id", default=None),
                "name": text(value(suite, "name", default=""), 255),
                "script_id": value(script, "id", default=None),
                "script_name": text(value(script, "name", default=""), 255),
            },
            "cases": result_snapshots,
            "stored_case_executions": [_case_context(row) for row in case_rows[: self.max_items]],
            "screenshots": [
                {
                    "id": value(row, "id", default=None),
                    "name": text(value(row, "name", default=""), 255),
                    "description": text(value(row, "description", default="")),
                    "reference": safe_reference(value(value(row, "image", default=None), "name", default="")),
                }
                for row in suite_screenshots[:200]
            ],
        }
        run = NormalizedRun(
            source_type=self.source_type,
            source_scope=self.source_scope,
            source_execution_id=str(value(execution, "id", default=execution_id)),
            project_type="UI",
            project_id=str(value(project, "id", default="") or ""),
            source_asset_id=str(value(suite, "id", default=value(script, "id", default="")) or ""),
            source_asset_name=text(value(suite, "name", default=value(script, "name", default="")), 255),
            source_status=status,
            started_at=value(execution, "started_at", default=None),
            finished_at=value(execution, "finished_at", default=None),
            duration_ms=duration_ms(value(execution, "started_at", default=None), value(execution, "finished_at", default=None), value(execution, "duration", default=None)),
            environment_fingerprint=text(
                value(execution, "environment", default="unknown"), 128
            ) or "unknown",
            summary=summary,
            source_snapshot=source_snapshot,
            warnings=warnings,
            items=items[:MAX_ITEMS],
            evidence=normalized_evidence[:MAX_EVIDENCE],
            failures=failures[:MAX_FAILURES],
            metrics=metrics[:MAX_METRICS],
            outcome=outcome,
        )
        return finalize_run(run)


class UiCaseSourceAdapter(BaseSourceAdapter):
    """Normalize one UI ``TestCaseExecution`` row when called directly."""

    source_type = "UI"
    source_scope = "CASE"
    terminal_statuses = _CASE_TERMINAL

    def normalize(self, execution_id: Any = None) -> NormalizedRun:
        execution_id = self._resolve_execution_id(execution_id)
        _, TestCaseExecution, _ = _models()
        execution = load_by_id(TestCaseExecution.objects, execution_id, source_type=self.source_type)
        status = self._ensure_terminal(execution, execution_id)
        context = _case_context(execution)
        item, logs, screenshots, error_message = _item_from_case(context, index=0, fallback=execution)
        normalized_evidence: list[Any] = []
        _add_case_evidence(
            normalized_evidence,
            item_key=item.test_key,
            logs=logs,
            screenshots=screenshots,
            error_message=error_message,
        )
        failures = [failure(error_message or "UI case failed", item_key=item.test_key, phase="case")] if item.outcome == FAIL else []
        outcome = item.outcome
        run = NormalizedRun(
            source_type=self.source_type,
            source_scope=self.source_scope,
            source_execution_id=str(value(execution, "id", default=execution_id)),
            project_type="UI",
            project_id=str(value(execution, "project_id", default="") or ""),
            source_asset_id=str(value(value(execution, "test_case", default=None), "id", default="") or ""),
            source_asset_name=text(value(value(execution, "test_case", default=None), "name", default=""), 255),
            source_status=status,
            started_at=value(execution, "started_at", default=None),
            finished_at=value(execution, "finished_at", default=None),
            duration_ms=duration_ms(None, None, value(execution, "execution_time", default=None)),
            summary=payload({"outcome": outcome, "result_count": 1}),
            source_snapshot={"execution": context},
            warnings=[] if normalized_evidence else ["ui_item_evidence_unavailable:" + item.test_key],
            items=[item],
            evidence=normalized_evidence,
            failures=failures,
            metrics=[
                NormalizedMetric(
                    metric_key="duration_ms",
                    value=float(item.duration_ms or 0),
                    item_key=item.test_key,
                    sample_key=item.test_key,
                    unit="ms",
                )
            ]
            if item.duration_ms is not None
            else [],
            outcome=outcome,
        )
        return finalize_run(run)


UIAdapter = UiSourceAdapter
UiAdapter = UiSourceAdapter
UISourceAdapter = UiSourceAdapter
UICaseAdapter = UiCaseSourceAdapter

__all__ = [
    "UiSourceAdapter",
    "UISourceAdapter",
    "UIAdapter",
    "UiAdapter",
    "UiCaseSourceAdapter",
    "UICaseAdapter",
]
