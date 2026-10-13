"""Read-only adapter for API test-suite executions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
    source_status,
    text,
    value,
)


_TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


def _models():
    # Keep source imports lazy so ``import apps.quality_intelligence.adapters``
    # also works under lightweight Django test settings.
    from apps.api_testing.models import RequestHistory, TestExecution

    return TestExecution, RequestHistory


def _as_results(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    if isinstance(raw, Mapping):
        for key in ("results", "requests", "items", "test_results"):
            candidate = raw.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, Mapping)]
        if any(key in raw for key in ("name", "request_id", "status_code", "passed", "status")):
            return [raw]
    return []


def _query_list(manager: Any, **filters: Any) -> list[Any]:
    try:
        queryset = manager.filter(**filters)
        if hasattr(queryset, "order_by"):
            queryset = queryset.order_by("-executed_at")
        return list(queryset[:200] if hasattr(queryset, "__getitem__") else queryset)
    except Exception:
        return []


def _request_context(suite: Any) -> tuple[list[Any], list[dict[str, Any]]]:
    relationships: list[Any] = []
    contexts: list[dict[str, Any]] = []
    try:
        queryset = suite.testsuiterequest_set.all().order_by("order")
        relationships = list(queryset[:MAX_ITEMS])
    except Exception:
        try:
            relationships = list(suite.testsuiterequest_set.all())[:MAX_ITEMS]
        except Exception:
            relationships = []
    for relation in relationships:
        request = value(relation, "request", default=None)
        if request is None:
            continue
        contexts.append(
            {
                "id": value(request, "id", default=None),
                "name": text(value(request, "name", default=""), 255),
                "method": text(value(request, "method", default=""), 16),
                "url": text(value(request, "url", default=""), 1024),
                "collection_id": value(value(request, "collection", default=None), "id", default=None),
                "suite_request_id": value(relation, "id", default=None),
                "order": value(relation, "order", default=0),
                "enabled": bool(value(relation, "enabled", default=True)),
            }
        )
    return relationships, contexts


def _history_context(histories: Iterable[Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    latest: dict[str, Any] = {}
    snapshots: list[dict[str, Any]] = []
    for history in histories:
        request_id = value(history, "request_id", default=None)
        if request_id is None:
            request = value(history, "request", default=None)
            request_id = value(request, "id", default=None)
        if request_id is None:
            continue
        key = str(request_id)
        if key in latest:
            continue
        latest[key] = history
        snapshots.append(
            {
                "id": value(history, "id", default=None),
                "request_id": request_id,
                "status_code": value(history, "status_code", default=None),
                "response_time": value(history, "response_time", default=None),
                "request_data": payload(value(history, "request_data", default={}) or {}),
                "response_data": payload(value(history, "response_data", default={}) or {}),
                "assertions_results": payload(value(history, "assertions_results", default=[]) or []),
                "executed_at": value(history, "executed_at", default=None),
            }
        )
    return latest, snapshots[:MAX_ITEMS]


class ApiSourceAdapter(BaseSourceAdapter):
    """Normalize API ``TestExecution`` and its request/history context."""

    source_type = "API"
    source_scope = "SUITE"
    terminal_statuses = _TERMINAL

    def normalize(self, execution_id: Any = None) -> NormalizedRun:
        execution_id = self._resolve_execution_id(execution_id)
        TestExecution, RequestHistory = _models()
        execution = load_by_id(TestExecution.objects, execution_id, source_type=self.source_type)
        status = self._ensure_terminal(execution, execution_id)
        suite = value(execution, "test_suite", default=None)
        project = value(suite, "project", default=None)
        relationships, requests = _request_context(suite) if suite is not None else ([], [])

        request_ids = [item.get("id") for item in requests if item.get("id") is not None]
        histories = _query_list(RequestHistory.objects, request_id__in=request_ids) if request_ids else []
        history_by_request, history_snapshots = _history_context(histories)
        raw_results = value(execution, "results", default=None)
        results = _as_results(raw_results)
        warnings: list[str] = []
        if not results:
            warnings.append("api_results_unavailable")
        if request_ids and not history_by_request:
            warnings.append("api_request_history_unavailable")

        items: list[NormalizedItem] = []
        normalized_evidence = []
        failures = []
        metrics = []
        result_snapshots: list[dict[str, Any]] = []
        for index, result in enumerate(results[: self.max_items]):
            request_id = value(result, "request_id", "api_request_id", default=None)
            history_id = value(result, "history_id", default=None)
            result_name = value(result, "name", "request_name", default="")
            request_context = next(
                (item for item in requests if request_id is not None and str(item.get("id")) == str(request_id)),
                None,
            )
            if request_id is None and request_context is not None:
                request_id = request_context.get("id")
            history = None
            if history_id is not None:
                history = next(
                    (candidate for candidate in histories if str(value(candidate, "id", default="")) == str(history_id)),
                    None,
                )
            if history is None and request_id is not None:
                history = history_by_request.get(str(request_id))
            if not result_name and request_context:
                result_name = request_context.get("name", "")
            test_key = str(
                value(result, "test_key", "key", default=None)
                or (f"request:{request_id}" if request_id is not None else f"request:{index + 1}")
            )
            result_status = value(result, "outcome", "status", default=None)
            result_passed = value(result, "passed", "success", default=None)
            result_failed = value(result, "failed", default=None)
            result_skipped = value(result, "skipped", default=None)
            history_status = value(history, "status", default=None)
            assertions = value(result, "assertions_results", "assertions", default=None)
            if assertions is None and history is not None:
                assertions = value(history, "assertions_results", default=None)
            explicit_outcome = normalize_outcome(
                result_status or history_status,
                passed=result_passed,
                failed=result_failed,
                skipped=result_skipped,
            )
            status_code = value(result, "status_code", "http_status", default=None)
            if status_code is None and history is not None:
                status_code = value(history, "status_code", default=None)
            if explicit_outcome == INCONCLUSIVE and status_code is not None:
                try:
                    explicit_outcome = FAIL if int(status_code) >= 400 else PASS
                except (TypeError, ValueError):
                    pass
            details = {
                "method": value(result, "method", default=None) or (request_context or {}).get("method", ""),
                "url": value(result, "url", default=None) or (request_context or {}).get("url", ""),
                "status_code": status_code,
                "response_time_ms": value(result, "response_time", "response_time_ms", default=None),
                "history_id": value(history, "id", default=None),
                "assertions_present": bool(assertions),
            }
            item = NormalizedItem(
                test_key=test_key,
                outcome=explicit_outcome,
                source_item_type="REQUEST",
                source_item_id=str(request_id or history_id or ""),
                name=text(result_name, 255),
                source_status=text(result_status or ("FAILED" if explicit_outcome == FAIL else ""), 64),
                duration_ms=duration_ms(None, None, details["response_time_ms"]),
                details=payload(details),
            )
            items.append(item)

            request_data = value(history, "request_data", default=None) if history is not None else None
            response_data = value(history, "response_data", default=None) if history is not None else None
            request_data = request_data if request_data is not None else value(result, "request_data", "request", default=None)
            response_data = response_data if response_data is not None else value(result, "response_data", "response", default=None)
            if request_data is not None:
                normalized_evidence.append(
                    evidence("REQUEST", item_key=test_key, snippet=payload(request_data), source_field="request_data")
                )
            if response_data is not None:
                normalized_evidence.append(
                    evidence("RESPONSE", item_key=test_key, snippet=payload(response_data), source_field="response_data")
                )
            if assertions is not None:
                normalized_evidence.append(
                    evidence("ASSERTION", item_key=test_key, snippet=payload(assertions), source_field="assertions_results")
                )
            if not any(entry.item_key == test_key for entry in normalized_evidence):
                warnings.append(f"api_item_evidence_unavailable:{test_key}")

            error_message = value(result, "error", "error_message", default="")
            if not error_message and history is not None:
                error_message = value(history, "error_message", default="")
            context = {
                "status_code": status_code,
                "assertions_results": assertions,
                "response": response_data,
            }
            if explicit_outcome in {FAIL}:
                if not error_message and status_code is not None:
                    error_message = f"HTTP status {status_code}"
                failures.append(
                    failure(
                        error_message or "API request failed",
                        item_key=test_key,
                        context=context,
                        phase="request",
                        source_error_code=str(status_code or ""),
                    )
                )
            response_time = safe_number(details["response_time_ms"])
            if response_time is not None:
                metrics.append(
                    NormalizedMetric(
                        metric_key="response_time_ms",
                        value=response_time,
                        item_key=test_key,
                        sample_key=test_key,
                        unit="ms",
                        dimensions={"method": text(details["method"], 16)},
                    )
                )
            result_snapshots.append(payload(dict(result)))

        outcome = outcome_for_items(items, status)
        if status == "FAILED" and not failures:
            error = value(execution, "error_message", default="API execution failed") or "API execution failed"
            failures.append(failure(error, phase="execution"))
        if status == "FAILED" and outcome == INCONCLUSIVE:
            outcome = FAIL
        if status == "CANCELLED" and not items:
            outcome = SKIP

        total = value(execution, "total_requests", default=0) or 0
        passed_count = value(execution, "passed_requests", default=0) or 0
        failed_count = value(execution, "failed_requests", default=0) or 0
        summary = payload(
            {
                "total": total,
                "passed": passed_count,
                "failed": failed_count,
                "result_count": len(results),
                "outcome": outcome,
            }
        )
        source_snapshot = {
            "execution": {
                "id": value(execution, "id", default=execution_id),
                "status": status,
                "start_time": value(execution, "start_time", default=None),
                "end_time": value(execution, "end_time", default=None),
                "total_requests": total,
                "passed_requests": passed_count,
                "failed_requests": failed_count,
                "results": result_snapshots,
            },
            "suite": {
                "id": value(suite, "id", default=None),
                "name": text(value(suite, "name", default=""), 255),
                "project_id": value(project, "id", default=None),
                "project_name": text(value(project, "name", default=""), 255),
            },
            "requests": requests,
            "histories": history_snapshots,
        }
        run = NormalizedRun(
            source_type=self.source_type,
            source_scope=self.source_scope,
            source_execution_id=str(value(execution, "id", default=execution_id)),
            project_type="API",
            project_id=str(value(project, "id", default="") or ""),
            source_asset_id=str(value(suite, "id", default="") or ""),
            source_asset_name=text(value(suite, "name", default=""), 255),
            source_status=status,
            started_at=value(execution, "start_time", default=None),
            finished_at=value(execution, "end_time", default=None),
            duration_ms=duration_ms(value(execution, "start_time", default=None), value(execution, "end_time", default=None)),
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


# Public aliases retained for callers that name adapters by source rather than
# implementation detail.
APIAdapter = ApiSourceAdapter
ApiAdapter = ApiSourceAdapter

__all__ = ["ApiSourceAdapter", "APIAdapter", "ApiAdapter"]
