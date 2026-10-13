"""Read-only adapter for mobile/APP test executions."""

from __future__ import annotations

from typing import Any

from .base import (
    FAIL,
    INCONCLUSIVE,
    MAX_EVIDENCE,
    MAX_FAILURES,
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

_TERMINAL = frozenset({"COMPLETED", "ERROR", "STOPPED", "SUCCESS", "FAILED"})


def _models():
    from apps.app_automation.models import AppTestExecution

    return AppTestExecution


class AppSourceAdapter(BaseSourceAdapter):
    """Normalize ``AppTestExecution`` without opening reports or files."""

    source_type = "APP"
    source_scope = "CASE"
    terminal_statuses = _TERMINAL

    def normalize(self, execution_id: Any = None) -> NormalizedRun:
        execution_id = self._resolve_execution_id(execution_id)
        AppTestExecution = _models()
        execution = load_by_id(AppTestExecution.objects, execution_id, source_type=self.source_type)
        status = self._ensure_terminal(execution, execution_id)
        result = value(execution, "result", default=None)
        test_case = value(execution, "test_case", default=None)
        test_suite = value(execution, "test_suite", default=None)
        device = value(execution, "device", default=None)
        project = value(test_case, "project", default=None) or value(test_suite, "project", default=None)
        item_key = f"case:{value(test_case, 'id', default='') or execution_id}"
        outcome = normalize_outcome(result, passed=result == "passed", skipped=result == "skipped")
        if outcome == INCONCLUSIVE:
            outcome = normalize_outcome(status)
        # COMPLETED with a missing result is deliberately inconclusive: a
        # lifecycle state alone is not proof that the case passed.
        if status == "COMPLETED" and not result:
            outcome = INCONCLUSIVE

        total_steps = value(execution, "total_steps", default=0) or 0
        passed_steps = value(execution, "passed_steps", default=0) or 0
        failed_steps = value(execution, "failed_steps", default=0) or 0
        error_message = value(execution, "error_message", default="") or ""
        details = {
            "result": result,
            "total_steps": total_steps,
            "passed_steps": passed_steps,
            "failed_steps": failed_steps,
            "device_id": value(device, "device_id", default=""),
        }
        item = NormalizedItem(
            test_key=item_key,
            outcome=outcome,
            source_item_type="CASE",
            source_item_id=str(value(test_case, "id", default="") or ""),
            name=text(value(test_case, "name", default="") or value(test_suite, "name", default=""), 255),
            source_status=text(result or status, 64),
            duration_ms=duration_ms(
                value(execution, "started_at", default=None),
                value(execution, "finished_at", default=None),
                value(execution, "duration", default=None),
            ),
            details=payload(details),
        )

        normalized_evidence: list[Any] = []
        report_path = value(execution, "report_path", default="") or ""
        report_reference = safe_reference(report_path)
        if report_path and report_reference:
            normalized_evidence.append(
                evidence(
                    "ALLURE",
                    item_key=item_key,
                    snippet="Allure report reference",
                    source_uri=report_reference,
                    source_field="report_path",
                    mime_type="text/html",
                )
            )
            allure_warning: list[str] = []
        elif report_path:
            # The path exists in source data but cannot safely be exposed.
            normalized_evidence.append(
                evidence(
                    "ALLURE",
                    item_key=item_key,
                    snippet="Allure report reference redacted",
                    source_field="report_path",
                    availability="REDACTED",
                    mime_type="text/html",
                )
            )
            allure_warning = ["app_allure_reference_redacted"]
        else:
            # Missing Allure is evidence availability, not a product failure.
            normalized_evidence.append(
                evidence(
                    "ALLURE",
                    item_key=item_key,
                    snippet="Allure report unavailable",
                    source_field="report_path",
                    availability="UNAVAILABLE",
                    mime_type="text/html",
                )
            )
            allure_warning = ["app_allure_unavailable"]
        if error_message:
            normalized_evidence.append(
                evidence("LOG", item_key=item_key, snippet=error_message, source_field="error_message")
            )

        failures: list[Any] = []
        if outcome == FAIL:
            failures.append(
                failure(
                    error_message or "APP test failed",
                    item_key=item_key,
                    phase="execution",
                    context={"result": result, "status": status, "failed_steps": failed_steps},
                )
            )
        elif failed_steps and outcome == INCONCLUSIVE:
            failures.append(
                failure(
                    error_message or "APP execution has failed steps",
                    item_key=item_key,
                    phase="step",
                    context={"failed_steps": failed_steps},
                )
            )
            outcome = FAIL
            item.outcome = FAIL

        metrics: list[Any] = []
        for key, raw_value, unit in (
            ("step_total", total_steps, "count"),
            ("step_passed", passed_steps, "count"),
            ("step_failed", failed_steps, "count"),
        ):
            number = safe_number(raw_value)
            if number is not None:
                metrics.append(
                    NormalizedMetric(
                        metric_key=key,
                        value=number,
                        item_key=item_key,
                        sample_key=item_key,
                        unit=unit,
                    )
                )
        if item.duration_ms is not None:
            metrics.append(
                NormalizedMetric(
                    metric_key="duration_ms",
                    value=float(item.duration_ms),
                    item_key=item_key,
                    sample_key=item_key,
                    unit="ms",
                )
            )

        if status == "ERROR" and outcome == INCONCLUSIVE:
            outcome = FAIL
            item.outcome = FAIL
            failures.append(failure(error_message or "APP execution error", item_key=item_key, phase="execution"))
        if status == "STOPPED" and not result:
            outcome = SKIP
            item.outcome = SKIP

        summary = payload(
            {
                "result": result,
                "status": status,
                "total_steps": total_steps,
                "passed_steps": passed_steps,
                "failed_steps": failed_steps,
                "outcome": outcome,
                "allure_available": bool(report_reference),
            }
        )
        package = value(test_case, "app_package", default=None)
        source_snapshot = {
            "execution": {
                "id": value(execution, "id", default=execution_id),
                "status": status,
                "result": result,
                "started_at": value(execution, "started_at", default=None),
                "finished_at": value(execution, "finished_at", default=None),
                "duration": value(execution, "duration", default=None),
                "total_steps": total_steps,
                "passed_steps": passed_steps,
                "failed_steps": failed_steps,
                "error_message": error_message,
                "report_reference": report_reference,
            },
            "case": {
                "id": value(test_case, "id", default=None),
                "name": text(value(test_case, "name", default=""), 255),
                "package_id": value(package, "id", default=None),
                "package_name": text(value(package, "package_name", default=""), 255),
            },
            "suite": {
                "id": value(test_suite, "id", default=None),
                "name": text(value(test_suite, "name", default=""), 255),
            },
            "device": {
                "id": value(device, "id", default=None),
                "device_id": text(value(device, "device_id", default=""), 255),
                "name": text(value(device, "name", default=""), 255),
                "android_version": text(value(device, "android_version", default=""), 64),
                "connection_type": text(value(device, "connection_type", default=""), 64),
                "specs": payload(value(device, "device_specs", default={}) or {}),
            },
        }
        from ..services.provenance import fingerprint_source

        environment_fingerprint = fingerprint_source(source_snapshot.get("device", {}))
        warnings = list(allure_warning)
        if outcome == INCONCLUSIVE:
            warnings.append("app_result_unavailable")
        run = NormalizedRun(
            source_type=self.source_type,
            source_scope=self.source_scope,
            source_execution_id=str(value(execution, "id", default=execution_id)),
            project_type="APP",
            project_id=str(value(project, "id", default="") or ""),
            source_asset_id=str(value(test_case, "id", default=value(test_suite, "id", default="")) or ""),
            source_asset_name=text(value(test_case, "name", default=value(test_suite, "name", default="")), 255),
            source_status=status,
            started_at=value(execution, "started_at", default=None),
            finished_at=value(execution, "finished_at", default=None),
            duration_ms=duration_ms(value(execution, "started_at", default=None), value(execution, "finished_at", default=None), value(execution, "duration", default=None)),
            environment_fingerprint=environment_fingerprint,
            summary=summary,
            source_snapshot=source_snapshot,
            warnings=warnings,
            items=[item],
            evidence=normalized_evidence[:MAX_EVIDENCE],
            failures=failures[:MAX_FAILURES],
            metrics=metrics,
            outcome=outcome,
        )
        return finalize_run(run)


APPAdapter = AppSourceAdapter
AppAdapter = AppSourceAdapter

__all__ = ["AppSourceAdapter", "APPAdapter", "AppAdapter"]
