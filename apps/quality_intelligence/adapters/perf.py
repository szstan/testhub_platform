"""Read-only adapter for performance test executions and bounded metrics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import (
    FAIL,
    INCONCLUSIVE,
    MAX_EVIDENCE,
    MAX_FAILURES,
    MAX_METRICS,
    MAX_ITEMS,
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


_TERMINAL = frozenset({"COMPLETED", "FAILED", "STOPPED", "TIMEOUT"})
_MAX_STATS = 500
_MAX_SAMPLES = 500

_SUMMARY_METRICS = {
    "tps": ("tps", "count/s"),
    "peak_tps": ("peak_tps", "count/s"),
    "avg_rt": ("avg_rt", "ms"),
    "p50_rt": ("p50_rt", "ms"),
    "p90_rt": ("p90_rt", "ms"),
    "p95_rt": ("p95_rt", "ms"),
    "p99_rt": ("p99_rt", "ms"),
    "max_rt": ("max_rt", "ms"),
    "min_rt": ("min_rt", "ms"),
    "error_rate": ("error_rate", "%"),
    "total_requests": ("total_requests", "count"),
    "success_requests": ("success_requests", "count"),
    "failed_requests": ("failed_requests", "count"),
    "max_concurrency": ("max_concurrency", "count"),
}
_STAT_METRICS = {
    "total": ("total", "count"),
    "success": ("success", "count"),
    "failed": ("failed", "count"),
    "error_rate": ("error_rate", "%"),
    "tps": ("tps", "count/s"),
    "avg_rt": ("avg_rt", "ms"),
    "min_rt": ("min_rt", "ms"),
    "max_rt": ("max_rt", "ms"),
    "p50_rt": ("p50_rt", "ms"),
    "p90_rt": ("p90_rt", "ms"),
    "p95_rt": ("p95_rt", "ms"),
    "p99_rt": ("p99_rt", "ms"),
    "sent_bytes": ("sent_bytes", "bytes"),
    "recv_bytes": ("recv_bytes", "bytes"),
}
_SAMPLE_METRICS = {
    "active_users": ("active_users", "count"),
    "tps": ("tps", "count/s"),
    "avg_rt": ("avg_rt", "ms"),
    "p90_rt": ("p90_rt", "ms"),
    "p95_rt": ("p95_rt", "ms"),
    "p99_rt": ("p99_rt", "ms"),
    "error_rate": ("error_rate", "%"),
    "total_requests": ("total_requests", "count"),
    "cpu_percent": ("cpu_percent", "%"),
    "memory_mb": ("memory_mb", "MB"),
}


def _models():
    from apps.perf_testing.models import PerfExecution, PerfMetricSample, PerfRequestStat

    return PerfExecution, PerfRequestStat, PerfMetricSample


def _rows(manager: Any, *, order: str, limit: int) -> list[Any]:
    try:
        queryset = manager.all()
        if hasattr(queryset, "order_by"):
            queryset = queryset.order_by(order)
        return list(queryset[:limit])
    except Exception:
        return []


def _failed_detail(details: Any) -> list[Mapping[str, Any]]:
    if not isinstance(details, list):
        return []
    return [item for item in details if isinstance(item, Mapping) and item.get("passed") is not True][:100]


class PerfSourceAdapter(BaseSourceAdapter):
    """Normalize performance summaries, bounded rows, and safe report refs."""

    source_type = "PERF"
    source_scope = "SCENARIO"
    terminal_statuses = _TERMINAL

    def __init__(
        self,
        *,
        max_items: int = MAX_ITEMS,
        max_stats: int = _MAX_STATS,
        max_samples: int = _MAX_SAMPLES,
        max_metrics: int = MAX_METRICS,
    ) -> None:
        super().__init__(max_items=max_items)
        self.max_stats = max(1, int(max_stats))
        self.max_samples = max(1, int(max_samples))
        self.max_metrics = max(1, int(max_metrics))

    def normalize(self, execution_id: Any) -> NormalizedRun:
        PerfExecution, PerfRequestStat, PerfMetricSample = _models()
        execution = load_by_id(PerfExecution.objects, execution_id, source_type=self.source_type)
        status = self._ensure_terminal(execution, execution_id)
        scenario = value(execution, "scenario", default=None)
        project = value(execution, "project", default=None)
        summary = value(execution, "summary", default={}) or {}
        if not isinstance(summary, Mapping):
            summary = {}
        sla_result = text(value(execution, "sla_result", default=""), 32)
        verdict = text(value(execution, "verdict", default=""), 32)
        sla_detail = value(execution, "sla_detail", default=[]) or []
        verdict_details = value(execution, "verdict_details", default=[]) or []
        request_stats = _rows(
            PerfRequestStat.objects.filter(execution_id=value(execution, "id", default=execution_id)),
            order="id",
            limit=self.max_stats + 1,
        )
        samples = _rows(
            PerfMetricSample.objects.filter(execution_id=value(execution, "id", default=execution_id)),
            order="ts_offset",
            limit=self.max_samples + 1,
        )
        warnings: list[str] = []
        if len(request_stats) > self.max_stats:
            warnings.append("perf_request_stats_truncated")
            request_stats = request_stats[: self.max_stats]
        if len(samples) > self.max_samples:
            warnings.append("perf_metric_samples_truncated")
            samples = samples[: self.max_samples]
        if not summary:
            warnings.append("perf_summary_unavailable")

        items: list[NormalizedItem] = []
        normalized_evidence: list[Any] = []
        failures: list[Any] = []
        metrics: list[Any] = []
        stats_snapshot: list[dict[str, Any]] = []
        sample_snapshot: list[dict[str, Any]] = []

        for stat in request_stats:
            stat_id = value(stat, "id", default=None)
            step_name = text(value(stat, "step_name", default="") or f"request-{stat_id}", 255)
            item_key = f"request:{stat_id or step_name}"
            total = value(stat, "total", default=0) or 0
            failed_count = value(stat, "failed", default=0) or 0
            stat_outcome = INCONCLUSIVE if not total else (FAIL if failed_count else PASS)
            item = NormalizedItem(
                test_key=item_key,
                outcome=stat_outcome,
                source_item_type="REQUEST",
                source_item_id=str(stat_id or ""),
                name=step_name,
                source_status="FAILED" if failed_count else ("PASSED" if total else "NOT_RUN"),
                details=payload(
                    {
                        "method": value(stat, "method", default=""),
                        "url": value(stat, "url", default=""),
                        "total": total,
                        "success": value(stat, "success", default=0),
                        "failed": failed_count,
                        "error_rate": value(stat, "error_rate", default=0),
                    }
                ),
            )
            items.append(item)
            error_detail = value(stat, "error_detail", default=[]) or []
            if error_detail:
                normalized_evidence.append(
                    evidence("LOG", item_key=item_key, snippet=payload(error_detail), source_field="error_detail")
                )
            if failed_count:
                top_error = error_detail[0] if isinstance(error_detail, list) and error_detail else "performance request failed"
                failures.append(
                    failure(
                        top_error,
                        item_key=item_key,
                        phase="request",
                        context={"error_rate": value(stat, "error_rate", default=0)},
                    )
                )
            for field_name, (metric_key, unit) in _STAT_METRICS.items():
                number = safe_number(value(stat, field_name, default=None))
                if number is None:
                    continue
                metrics.append(
                    NormalizedMetric(
                        metric_key=metric_key,
                        value=number,
                        item_key=item_key,
                        sample_key=item_key,
                        unit=unit,
                        dimensions={"step": step_name},
                    )
                )
            stats_snapshot.append(
                payload(
                    {
                        "id": stat_id,
                        "step_name": value(stat, "step_name", default=""),
                        "method": value(stat, "method", default=""),
                        "url": value(stat, "url", default=""),
                        **{
                            field_name: value(stat, field_name, default=None)
                            for field_name in _STAT_METRICS
                        },
                        "error_detail": error_detail,
                    }
                )
            )

        for sample in samples:
            sample_id = value(sample, "id", default=None)
            offset = value(sample, "ts_offset", default=0) or 0
            sample_key = f"sample:{offset}:{sample_id or ''}"
            for field_name, (metric_key, unit) in _SAMPLE_METRICS.items():
                number = safe_number(value(sample, field_name, default=None))
                if number is None:
                    continue
                metrics.append(
                    NormalizedMetric(
                        metric_key=metric_key,
                        value=number,
                        item_key="",
                        sample_key=sample_key,
                        unit=unit,
                        aggregation="VALUE",
                        dimensions={"ts_offset": offset},
                    )
                )
            sample_snapshot.append(
                payload(
                    {
                        "id": sample_id,
                        **{field_name: value(sample, field_name, default=None) for field_name in _SAMPLE_METRICS},
                        "ts_offset": offset,
                    }
                )
            )

        if not items:
            run_key = f"scenario:{value(scenario, 'id', default=execution_id)}"
            if verdict.upper() == "FAILED" or sla_result.upper() == "FAILED" or status in {"FAILED", "TIMEOUT"}:
                run_outcome = FAIL
            elif status == "STOPPED":
                run_outcome = SKIP
            elif status == "COMPLETED" and verdict.upper() == "PASSED" and sla_result.upper() in {"PASSED", "NOT_EVALUATED", ""}:
                run_outcome = PASS if summary else INCONCLUSIVE
            else:
                run_outcome = INCONCLUSIVE
            items.append(
                NormalizedItem(
                    test_key=run_key,
                    outcome=run_outcome,
                    source_item_type="SCENARIO",
                    source_item_id=str(value(scenario, "id", default="") or ""),
                    name=text(value(scenario, "name", default=""), 255),
                    source_status=status,
                    details=payload({"verdict": verdict, "sla_result": sla_result}),
                )
            )
        if summary:
            normalized_evidence.append(
                evidence("METRIC", snippet=payload(summary), source_field="summary")
            )
        if sla_detail:
            normalized_evidence.append(
                evidence("METRIC", snippet=payload(sla_detail), source_field="sla_detail")
            )
        if verdict_details:
            normalized_evidence.append(
                evidence("ASSERTION", snippet=payload(verdict_details), source_field="verdict_details")
            )

        if sla_result.upper() == "FAILED":
            failed_sla = _failed_detail(sla_detail)
            failures.append(
                failure(
                    failed_sla[0] if failed_sla else "SLA breach",
                    phase="sla",
                    context={"sla_breach": True, "sla_result": sla_result},
                )
            )
        if verdict.upper() == "FAILED":
            failed_target = _failed_detail(verdict_details)
            failures.append(
                failure(
                    failed_target[0] if failed_target else "Performance acceptance target failed",
                    phase="verdict",
                    context={"verdict": verdict},
                )
            )
        execution_error = value(execution, "error_message", default="") or ""
        if status in {"FAILED", "TIMEOUT"}:
            failures.append(
                failure(
                    execution_error or f"Performance execution {status.lower()}",
                    phase="execution",
                    context={"status": status},
                )
            )

        # Numeric summary values are deterministic observations in addition to
        # the redacted summary evidence.
        for field_name, (metric_key, unit) in _SUMMARY_METRICS.items():
            number = safe_number(summary.get(field_name))
            if number is not None:
                metrics.append(
                    NormalizedMetric(
                        metric_key=metric_key,
                        value=number,
                        sample_key="summary",
                        unit=unit,
                    )
                )
        metrics = metrics[: self.max_metrics]
        if len(metrics) >= self.max_metrics:
            warnings.append("perf_metrics_truncated")

        outcome = outcome_for_items(items, status)
        if verdict.upper() == "FAILED" or sla_result.upper() == "FAILED" or status in {"FAILED", "TIMEOUT"}:
            outcome = FAIL
        elif status == "STOPPED" and not failures:
            outcome = SKIP
        elif outcome == PASS and not summary:
            outcome = INCONCLUSIVE
            items[0].outcome = INCONCLUSIVE
            warnings.append("perf_outcome_evidence_unavailable")

        report_url = safe_reference(value(execution, "report_url", default=""))
        artifact_dir = safe_reference(value(execution, "artifact_dir", default=""))
        if report_url:
            normalized_evidence.append(
                evidence("ARTIFACT", snippet="Performance report reference", source_uri=report_url, source_field="report_url")
            )
        if artifact_dir:
            normalized_evidence.append(
                evidence("ARTIFACT", snippet="Performance artifact reference", source_uri=artifact_dir, source_field="artifact_dir")
            )

        source_snapshot = {
            "execution": {
                "id": value(execution, "id", default=execution_id),
                "execution_no": text(value(execution, "execution_no", default=""), 64),
                "status": status,
                "sla_result": sla_result,
                "verdict": verdict,
                "summary": payload(summary),
                "sla_detail": payload(sla_detail),
                "verdict_details": payload(verdict_details),
                "load_snapshot": payload(value(execution, "load_snapshot", default={}) or {}),
                "steps_snapshot": payload(value(execution, "steps_snapshot", default=[]) or []),
                "script_ref": payload(value(execution, "script_ref", default={}) or {}),
                "worker_host": text(value(execution, "worker_host", default=""), 100),
                "report_reference": report_url,
                "artifact_reference": artifact_dir,
                "error_message": execution_error,
            },
            "scenario": {
                "id": value(scenario, "id", default=None),
                "name": text(value(scenario, "name", default=""), 255),
                "engine": text(value(scenario, "engine", default=""), 32),
                "project_id": value(project, "id", default=None),
            },
            "request_stats": stats_snapshot,
            "metric_samples": sample_snapshot,
        }
        from ..services.provenance import fingerprint_source

        environment_fingerprint = fingerprint_source(
            {
                "worker_host": source_snapshot["execution"]["worker_host"],
                "engine": source_snapshot["scenario"]["engine"],
                "load_snapshot": source_snapshot["execution"]["load_snapshot"],
            }
        )
        run = NormalizedRun(
            source_type=self.source_type,
            source_scope=self.source_scope,
            source_execution_id=str(value(execution, "id", default=execution_id)),
            project_type="PERF",
            project_id=str(value(project, "id", default="") or ""),
            source_asset_id=str(value(scenario, "id", default="") or ""),
            source_asset_name=text(value(scenario, "name", default=""), 255),
            source_status=status,
            started_at=value(execution, "start_time", default=None),
            finished_at=value(execution, "end_time", default=None),
            duration_ms=duration_ms(value(execution, "start_time", default=None), value(execution, "end_time", default=None), value(execution, "duration", default=None)),
            environment_fingerprint=environment_fingerprint,
            summary=payload(
                {
                    **dict(summary),
                    "sla_result": sla_result,
                    "verdict": verdict,
                    "outcome": outcome,
                    "request_stat_count": len(request_stats),
                    "sample_count": len(samples),
                }
            ),
            source_snapshot=source_snapshot,
            warnings=warnings,
            items=items[: self.max_items],
            evidence=normalized_evidence[:MAX_EVIDENCE],
            failures=failures[:MAX_FAILURES],
            metrics=metrics,
            outcome=outcome,
        )
        return finalize_run(run)


PERFAdapter = PerfSourceAdapter
PerfAdapter = PerfSourceAdapter
PerformanceSourceAdapter = PerfSourceAdapter

__all__ = [
    "PerfSourceAdapter",
    "PerformanceSourceAdapter",
    "PERFAdapter",
    "PerfAdapter",
]
