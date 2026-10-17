"""Transactional, idempotent ingestion for normalized source executions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from django.db import transaction

from apps.quality_intelligence.adapters import get_adapter
from apps.quality_intelligence.adapters.base import (
    AdapterError,
    SourceNotFoundError,
    safe_reference,
    text,
)
from apps.quality_intelligence.models import (
    QualityComparison,
    QualityEvidence,
    QualityEvidenceAvailability,
    QualityEvidenceType,
    QualityFailure,
    QualityFailureCategory,
    QualityFailureClassifier,
    QualityItemOutcome,
    QualityMetricAggregation,
    QualityMetricObservation,
    QualityRun,
    QualityRunItem,
    QualityRunStatus,
    QualityTestProfile,
)
from apps.quality_intelligence.services.flakiness import (
    DEFAULT_WINDOW_SIZE,
    MIN_ELIGIBLE_ATTEMPTS,
    calculate_flakiness,
)
from apps.quality_intelligence.services.hashing import canonical_json, fingerprint, signature
from apps.quality_intelligence.services.provenance import (
    PROVENANCE_VERSION,
    fingerprint_source,
)
from apps.quality_intelligence.services.redaction import (
    REDACTION_VERSION,
    bound_text,
    redact_payload,
    redact_text,
)


ADAPTER_VERSION = "1.0"
SCHEMA_VERSION = 1
_VALID_OUTCOMES = {choice for choice, _ in QualityItemOutcome.choices}
_VALID_EVIDENCE_TYPES = {choice for choice, _ in QualityEvidenceType.choices}
_VALID_AVAILABILITY = {choice for choice, _ in QualityEvidenceAvailability.choices}
_VALID_AGGREGATIONS = {choice for choice, _ in QualityMetricAggregation.choices}
_VALID_FAILURE_CATEGORIES = {choice for choice, _ in QualityFailureCategory.choices}
_VALID_CLASSIFIERS = {choice for choice, _ in QualityFailureClassifier.choices}


@dataclass
class IngestionResult:
    """Outcome of one explicit source ingestion request."""

    source_type: str
    source_execution_id: str
    quality_run: QualityRun | None = None
    created: bool = False
    updated: bool = False
    reused: bool = False
    source_missing: bool = False
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def run(self) -> QualityRun | None:
        return self.quality_run

    @property
    def quality_status(self) -> str:
        if self.quality_run is None:
            return "DRY_RUN" if self.dry_run else "SOURCE_MISSING"
        return str(self.quality_run.quality_status)

    @property
    def partial(self) -> bool:
        return self.quality_status == QualityRunStatus.PARTIAL

    def as_dict(self) -> dict[str, Any]:
        run = self.quality_run
        return {
            "source_type": self.source_type,
            "source_execution_id": self.source_execution_id,
            "quality_run_id": run.pk if run is not None else None,
            "quality_status": self.quality_status,
            "created": self.created,
            "updated": self.updated,
            "reused": self.reused,
            "source_missing": self.source_missing,
            "dry_run": self.dry_run,
            "warnings": list(self.warnings),
        }


@dataclass
class BatchIngestionResult:
    """Failure-isolated result for a backfill batch."""

    results: list[IngestionResult] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return len(self.results)

    @property
    def failed(self) -> int:
        return len(self.errors)

    def as_dict(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "failed": self.failed,
            "results": [result.as_dict() for result in self.results],
            "errors": list(self.errors),
        }


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name)
    except (AttributeError, TypeError):
        return default


def _choice(value: Any, choices: set[str], default: str) -> str:
    raw = getattr(value, "value", value)
    normalized = "" if raw is None else str(raw).upper()
    return normalized if normalized in choices else default


def _bounded_payload(value: Any, *, max_items: int = 1000) -> Any:
    return redact_payload(value, max_text_length=4096, max_items=max_items, max_depth=12)


def _bounded_snippet(value: Any) -> str:
    if isinstance(value, (Mapping, list, tuple)):
        value = canonical_json(_bounded_payload(value))
    return bound_text(redact_text(value, max_length=4096), 4096)


def _positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _as_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _read_adapter(adapter: Any, execution_id: Any) -> Any:
    for method_name in ("read", "adapt", "normalize", "collect"):
        method = getattr(adapter, method_name, None)
        if callable(method):
            return method(execution_id)
    raise AdapterError("quality source adapter has no read method")


def _normalized_lists(normalized: Any) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    return (
        list(_field(normalized, "items", []) or []),
        list(_field(normalized, "evidence", []) or []),
        list(_field(normalized, "failures", []) or []),
        list(_field(normalized, "metrics", []) or []),
    )


def _source_type_value(source_type: Any) -> str:
    value = getattr(source_type, "value", source_type)
    result = str(value or "").strip().upper()
    if not result:
        raise AdapterError("source type is required")
    return result


def _result_identity(normalized: Any, source_type: str, execution_id: Any) -> tuple[str, str]:
    normalized_type = str(_field(normalized, "source_type", source_type) or source_type).upper()
    normalized_id = str(_field(normalized, "source_execution_id", execution_id) or execution_id)
    return normalized_type, normalized_id


def _run_snapshot(normalized: Any) -> dict[str, Any]:
    snapshot = _bounded_payload(_field(normalized, "source_snapshot", {}) or {})
    return dict(snapshot) if isinstance(snapshot, Mapping) else {"value": snapshot}


def _run_defaults(normalized: Any, *, source_type: str, execution_id: str) -> dict[str, Any]:
    source_snapshot = _run_snapshot(normalized)
    project_type = text(_field(normalized, "project_type", source_type), 64) or source_type
    project_id = text(_field(normalized, "project_id", ""), 128)
    source_fingerprint = fingerprint_source(source_snapshot)
    provenance = _bounded_payload(_field(normalized, "provenance", {}) or {})
    if not isinstance(provenance, Mapping):
        provenance = {}
    provenance = dict(provenance)
    provenance.update(
        {
            "provenance_version": provenance.get("provenance_version", PROVENANCE_VERSION),
            "source_type": source_type,
            "source_execution_id": execution_id,
            "project_type": project_type,
            "project_id": project_id,
            "adapter_version": text(_field(normalized, "adapter_version", ADAPTER_VERSION), 32),
            "schema_version": _field(normalized, "schema_version", SCHEMA_VERSION),
            "redaction_version": REDACTION_VERSION,
            "source_model": {
                "API": "apps.api_testing.models.TestExecution",
                "UI": "apps.ui_automation.models.TestExecution",
                "APP": "apps.app_automation.models.AppTestExecution",
                "PERF": "apps.perf_testing.models.PerfExecution",
            }.get(source_type, ""),
            "source_fields": source_snapshot,
        }
    )
    summary = _bounded_payload(_field(normalized, "summary", {}) or {})
    if not isinstance(summary, Mapping):
        summary = {"value": summary}
    items, evidence, failures, metrics = _normalized_lists(normalized)
    warnings = [bound_text(redact_text(item, max_length=255), 255) for item in (_field(normalized, "warnings", []) or [])]
    warnings = list(dict.fromkeys(item for item in warnings if item))
    outcome = str(_field(normalized, "outcome", "INCONCLUSIVE") or "INCONCLUSIVE").upper()
    quality_status = str(_field(normalized, "quality_status", "INGESTED") or "INGESTED").upper()
    if warnings or outcome == "INCONCLUSIVE":
        quality_status = QualityRunStatus.PARTIAL
    return {
        "source_type": source_type,
        "source_scope": text(_field(normalized, "source_scope", "SUITE"), 16) or "SUITE",
        "source_execution_id": execution_id,
        "project_type": project_type,
        "project_id": project_id,
        "source_asset_id": text(_field(normalized, "source_asset_id", ""), 128),
        "source_asset_name": text(_field(normalized, "source_asset_name", ""), 255),
        "source_status": text(_field(normalized, "source_status", ""), 64),
        "quality_status": quality_status,
        "started_at": _as_datetime(_field(normalized, "started_at")),
        "finished_at": _as_datetime(_field(normalized, "finished_at")),
        "duration_ms": _positive_int(_field(normalized, "duration_ms")),
        "environment_fingerprint": text(_field(normalized, "environment_fingerprint", "unknown"), 128) or "unknown",
        "source_fingerprint": source_fingerprint,
        "adapter_version": text(_field(normalized, "adapter_version", ADAPTER_VERSION), 32) or ADAPTER_VERSION,
        "redaction_version": REDACTION_VERSION,
        "schema_version": _positive_int(_field(normalized, "schema_version", SCHEMA_VERSION)) or SCHEMA_VERSION,
        "summary": dict(summary),
        "provenance": provenance,
        "warnings": warnings,
        "_items": items,
        "_evidence": evidence,
        "_failures": failures,
        "_metrics": metrics,
    }


def _delete_derived(run: QualityRun) -> None:
    # Child rows are disposable projections. Comparisons are also projections
    # and must not survive a source snapshot replacement.
    QualityComparison.objects.filter(run=run).delete()
    run.items.all().delete()
    run.evidence.all().delete()
    run.failures.all().delete()
    run.metrics.all().delete()


def _persist_items(run: QualityRun, raw_items: Iterable[Any], *, environment: str) -> dict[str, list[QualityRunItem]]:
    by_key: dict[str, list[QualityRunItem]] = {}
    seen_attempts: dict[str, int] = {}
    for raw_item in raw_items:
        test_key = text(_field(raw_item, "test_key", _field(raw_item, "item_key", "")), 255)
        if not test_key:
            test_key = f"item:{len(by_key) + 1}"
        requested_attempt = _positive_int(_field(raw_item, "attempt", 1)) or 1
        attempt = max(requested_attempt, seen_attempts.get(test_key, 0) + 1 if test_key in seen_attempts else requested_attempt)
        seen_attempts[test_key] = attempt
        outcome = _choice(_field(raw_item, "outcome", "INCONCLUSIVE"), _VALID_OUTCOMES, "INCONCLUSIVE")
        item = QualityRunItem.objects.create(
            run=run,
            source_item_type=text(_field(raw_item, "source_item_type", ""), 64),
            source_item_id=text(_field(raw_item, "source_item_id", ""), 128),
            test_key=test_key,
            name=text(_field(raw_item, "name", ""), 255),
            source_status=text(_field(raw_item, "source_status", ""), 64),
            outcome=outcome,
            attempt=attempt,
            duration_ms=_positive_int(_field(raw_item, "duration_ms")),
            environment_fingerprint=text(_field(raw_item, "environment_fingerprint", environment), 128) or environment,
            details=_bounded_payload(_field(raw_item, "details", {}) or {}),
        )
        by_key.setdefault(test_key, []).append(item)
    return by_key


def _item_for_key(by_key: Mapping[str, list[QualityRunItem]], key: Any) -> QualityRunItem | None:
    normalized = str(key or "")
    values = by_key.get(normalized)
    return values[-1] if values else None


def _persist_evidence(run: QualityRun, raw_evidence: Iterable[Any], item_map: Mapping[str, list[QualityRunItem]]) -> None:
    seen: set[tuple[int | None, str, str]] = set()
    for raw in raw_evidence:
        item = _item_for_key(item_map, _field(raw, "item_key", _field(raw, "source_item_key", "")))
        evidence_type = _choice(_field(raw, "evidence_type", "LOG"), _VALID_EVIDENCE_TYPES, "LOG")
        snippet = _bounded_snippet(_field(raw, "snippet", ""))
        source_uri = safe_reference(_field(raw, "source_uri", ""))
        evidence_hash = text(_field(raw, "sha256", ""), 64) or fingerprint(
            {"type": evidence_type, "snippet": snippet, "source_uri": source_uri}
        )
        key = (item.pk if item is not None else None, evidence_type, evidence_hash)
        if key in seen:
            continue
        seen.add(key)
        availability = _choice(
            _field(raw, "availability", QualityEvidenceAvailability.AVAILABLE),
            _VALID_AVAILABILITY,
            QualityEvidenceAvailability.AVAILABLE,
        )
        QualityEvidence.objects.create(
            run=run,
            item=item,
            evidence_type=evidence_type,
            snippet=snippet,
            source_uri=source_uri,
            source_field=text(_field(raw, "source_field", ""), 255),
            sha256=evidence_hash,
            mime_type=text(_field(raw, "mime_type", ""), 128),
            size_bytes=_positive_int(_field(raw, "size_bytes")),
            availability=availability,
            provenance=_bounded_payload(_field(raw, "provenance", {}) or {}),
            redaction_version=REDACTION_VERSION,
            captured_at=_as_datetime(_field(raw, "captured_at")),
        )


def _persist_failures(run: QualityRun, raw_failures: Iterable[Any], item_map: Mapping[str, list[QualityRunItem]], source_type: str, execution_id: str) -> None:
    seen: set[str] = set()
    for index, raw in enumerate(raw_failures):
        item_key = _field(raw, "item_key", _field(raw, "source_item_key", ""))
        item = _item_for_key(item_map, item_key)
        taxonomy_code = text(_field(raw, "taxonomy_code", "UNKNOWN"), 32) or "UNKNOWN"
        model_category = text(_field(raw, "model_category", ""), 32)
        category = model_category if model_category in _VALID_FAILURE_CATEGORIES else (
            taxonomy_code if taxonomy_code in _VALID_FAILURE_CATEGORIES else QualityFailureCategory.UNKNOWN
        )
        initial = text(_field(raw, "initial_taxonomy_code", category), 32)
        if initial not in _VALID_FAILURE_CATEGORIES:
            initial = category
        classifier = text(_field(raw, "classifier", QualityFailureClassifier.RULE), 16)
        if classifier not in _VALID_CLASSIFIERS:
            classifier = QualityFailureClassifier.RULE
        message = _bounded_snippet(_field(raw, "message", ""))
        signature_value = text(_field(raw, "signature", ""), 128) or fingerprint(
            {"taxonomy": taxonomy_code, "message": message, "phase": _field(raw, "phase", "")}
        )
        dedupe_key = signature(
            "quality-failure", source_type, execution_id, str(item_key or ""), signature_value, index
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        details = _bounded_payload(_field(raw, "details", {}) or {})
        if isinstance(details, Mapping):
            details = dict(details)
            details.setdefault("taxonomy_code", taxonomy_code)
        confidence_value = _finite_float(_field(raw, "confidence", 1.0))
        QualityFailure.objects.create(
            run=run,
            item=item,
            dedupe_key=dedupe_key,
            taxonomy_code=category,
            subtype=text(_field(raw, "subtype", ""), 64),
            phase=text(_field(raw, "phase", ""), 64),
            signature=signature_value,
            source_error_code=text(_field(raw, "source_error_code", ""), 128),
            message=message,
            stack_excerpt=_bounded_snippet(_field(raw, "stack_excerpt", "")),
            retryable=bool(_field(raw, "retryable", False)),
            details=details,
            initial_taxonomy_code=initial,
            classifier=classifier,
            confidence=confidence_value if confidence_value is not None else 1.0,
            rule_id=text(_field(raw, "rule_id", ""), 128),
        )


def _refresh_profiles(run: QualityRun, items: Iterable[QualityRunItem]) -> dict[tuple[str, str], QualityTestProfile]:
    profiles: dict[tuple[str, str], QualityTestProfile] = {}
    for item in items:
        key = (item.test_key, item.environment_fingerprint or run.environment_fingerprint or "unknown")
        profile, _ = QualityTestProfile.objects.get_or_create(
            source_type=run.source_type,
            project_type=run.project_type,
            project_id=run.project_id,
            test_key=item.test_key,
            environment_fingerprint=key[1],
            defaults={
                "name": item.name,
                "window_size": DEFAULT_WINDOW_SIZE,
                "min_attempts": MIN_ELIGIBLE_ATTEMPTS,
            },
        )
        profiles[key] = profile

    for key, profile in profiles.items():
        queryset = QualityRunItem.objects.filter(
            run__source_type=run.source_type,
            run__project_type=run.project_type,
            run__project_id=run.project_id,
            test_key=profile.test_key,
            environment_fingerprint=profile.environment_fingerprint,
        ).select_related("run").order_by("run__created_at", "id")
        candidates = list(queryset[: max(profile.window_size, DEFAULT_WINDOW_SIZE) * 2])
        analysis = calculate_flakiness(
            candidates,
            window_size=profile.window_size,
            min_attempts=profile.min_attempts,
            test_key=profile.test_key,
            environment_fingerprint=profile.environment_fingerprint,
            source_type=run.source_type,
            project_type=run.project_type,
            project_id=run.project_id,
        )
        timestamps = [candidate.run.finished_at or candidate.run.created_at for candidate in candidates]
        timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
        profile.name = next((candidate.name for candidate in reversed(candidates) if candidate.name), profile.name)
        profile.eligible_attempts = int(analysis.get("eligible_attempts", 0))
        profile.pass_count = int(analysis.get("pass_count", 0))
        profile.fail_count = int(analysis.get("fail_count", 0))
        profile.transition_count = int(analysis.get("transition_count", 0))
        profile.flaky_score = float(analysis.get("flaky_score", 0.0))
        profile.reliability_status = str(analysis.get("reliability_status", "INSUFFICIENT_DATA"))
        profile.first_seen_at = min(timestamps) if timestamps else profile.first_seen_at
        profile.last_seen_at = max(timestamps) if timestamps else profile.last_seen_at
        profile.metadata = _bounded_payload({"reason": analysis.get("reason", ""), "window_count": analysis.get("window_count", 0)})
        profile.save()
    return profiles


def _persist_metrics(run: QualityRun, raw_metrics: Iterable[Any], item_map: Mapping[str, list[QualityRunItem]], profiles: Mapping[tuple[str, str], QualityTestProfile]) -> None:
    seen: set[tuple[str, str]] = set()
    for raw in raw_metrics:
        metric_key = text(_field(raw, "metric_key", _field(raw, "key", "")), 128)
        if not metric_key:
            continue
        item = _item_for_key(item_map, _field(raw, "item_key", ""))
        sample_key = text(_field(raw, "sample_key", _field(raw, "item_key", "")), 255)
        if not sample_key:
            sample_key = "value"
        base_sample_key = sample_key
        suffix = 1
        while (metric_key, sample_key) in seen:
            suffix += 1
            sample_key = f"{base_sample_key}#{suffix}"
        seen.add((metric_key, sample_key))
        value = _finite_float(_field(raw, "value"))
        if value is None:
            continue
        profile = None
        if item is not None:
            profile = profiles.get((item.test_key, item.environment_fingerprint or run.environment_fingerprint or "unknown"))
        aggregation = _choice(_field(raw, "aggregation", QualityMetricAggregation.VALUE), _VALID_AGGREGATIONS, QualityMetricAggregation.VALUE)
        QualityMetricObservation.objects.create(
            run=run,
            item=item,
            profile=profile,
            metric_key=metric_key,
            sample_key=sample_key,
            value=value,
            unit=text(_field(raw, "unit", ""), 32),
            aggregation=aggregation,
            dimensions=_bounded_payload(_field(raw, "dimensions", {}) or {}),
            observed_at=_as_datetime(_field(raw, "observed_at")),
        )


def _missing_run(source_type: str, execution_id: str, adapter: Any) -> IngestionResult:
    with transaction.atomic():
        run, created = QualityRun.objects.get_or_create(
            source_type=source_type,
            source_execution_id=execution_id,
            defaults={
                "source_scope": text(getattr(adapter, "source_scope", "SUITE"), 16) or "SUITE",
                "project_type": source_type,
                "project_id": "",
                "source_status": "",
                "quality_status": QualityRunStatus.SOURCE_MISSING,
                "adapter_version": text(getattr(adapter, "adapter_version", ADAPTER_VERSION), 32) or ADAPTER_VERSION,
                "redaction_version": REDACTION_VERSION,
                "schema_version": SCHEMA_VERSION,
                "summary": {"source_missing": True},
                "provenance": {
                    "provenance_version": PROVENANCE_VERSION,
                    "source_type": source_type,
                    "source_execution_id": execution_id,
                    "source_model": {
                        "API": "apps.api_testing.models.TestExecution",
                        "UI": "apps.ui_automation.models.TestExecution",
                        "APP": "apps.app_automation.models.AppTestExecution",
                        "PERF": "apps.perf_testing.models.PerfExecution",
                    }.get(source_type, ""),
                    "source_missing": True,
                },
                "warnings": ["source_missing"],
            },
        )
        if not created and run.quality_status != QualityRunStatus.SOURCE_MISSING:
            run.quality_status = QualityRunStatus.SOURCE_MISSING
            run.warnings = list(dict.fromkeys([*(run.warnings or []), "source_missing"]))
            run.save(update_fields=["quality_status", "warnings", "updated_at"])
    return IngestionResult(
        source_type=source_type,
        source_execution_id=execution_id,
        quality_run=run,
        created=created,
        updated=not created,
        source_missing=True,
        warnings=list(run.warnings or []),
    )


def ingest_source(
    source_type: Any,
    execution_id: Any,
    *,
    adapter: Any = None,
    allow_missing: bool = True,
    force: bool = False,
    dry_run: bool = False,
) -> IngestionResult:
    """Read one terminal source and transactionally materialize its projection."""

    source_key = _source_type_value(source_type)
    if execution_id is None or (isinstance(execution_id, str) and not execution_id.strip()):
        raise AdapterError("source execution id is required")
    execution_key = str(execution_id)
    adapter = adapter or get_adapter(source_key)
    try:
        normalized = _read_adapter(adapter, execution_id)
    except SourceNotFoundError:
        if dry_run:
            return IngestionResult(
                source_type=source_key,
                source_execution_id=execution_key,
                dry_run=True,
                source_missing=True,
                warnings=["source_missing"],
            )
        if not allow_missing:
            raise
        return _missing_run(source_key, execution_key, adapter)

    normalized_source, normalized_id = _result_identity(normalized, source_key, execution_key)
    if normalized_source != source_key or normalized_id != execution_key:
        raise AdapterError("adapter returned a mismatched source identity")
    defaults = _run_defaults(normalized, source_type=source_key, execution_id=execution_key)
    raw_items = defaults.pop("_items")
    raw_evidence = defaults.pop("_evidence")
    raw_failures = defaults.pop("_failures")
    raw_metrics = defaults.pop("_metrics")
    warnings = list(defaults.get("warnings") or [])

    if dry_run:
        return IngestionResult(
            source_type=source_key,
            source_execution_id=execution_key,
            dry_run=True,
            warnings=warnings,
        )

    with transaction.atomic():
        run, created = QualityRun.objects.select_for_update().get_or_create(
            source_type=source_key,
            source_execution_id=execution_key,
            defaults=defaults,
        )
        if (
            not created
            and not force
            and run.source_fingerprint == defaults["source_fingerprint"]
            and run.quality_status != QualityRunStatus.SOURCE_MISSING
        ):
            return IngestionResult(
                source_type=source_key,
                source_execution_id=execution_key,
                quality_run=run,
                reused=True,
                warnings=list(run.warnings or []),
            )

        if not created:
            _delete_derived(run)
            for field_name, value in defaults.items():
                setattr(run, field_name, value)
            run.save()
        item_map = _persist_items(
            run,
            raw_items,
            environment=defaults["environment_fingerprint"],
        )
        item_records = [item for values in item_map.values() for item in values]
        profiles = _refresh_profiles(run, item_records)
        _persist_evidence(run, raw_evidence, item_map)
        _persist_failures(run, raw_failures, item_map, source_key, execution_key)
        _persist_metrics(run, raw_metrics, item_map, profiles)

    return IngestionResult(
        source_type=source_key,
        source_execution_id=execution_key,
        quality_run=run,
        created=created,
        updated=not created,
        warnings=warnings,
    )


def ingest_many(
    source_type: Any,
    execution_ids: Iterable[Any],
    *,
    allow_missing: bool = True,
    force: bool = False,
    dry_run: bool = False,
) -> BatchIngestionResult:
    """Ingest IDs independently so one bad source cannot abort a backfill."""

    batch = BatchIngestionResult()
    for execution_id in execution_ids:
        try:
            batch.results.append(
                ingest_source(
                    source_type,
                    execution_id,
                    allow_missing=allow_missing,
                    force=force,
                    dry_run=dry_run,
                )
            )
        except Exception as exc:  # keep a batch failure isolated and redacted
            batch.errors.append(
                {
                    "source_type": _source_type_value(source_type),
                    "source_execution_id": str(execution_id),
                    "error_type": exc.__class__.__name__,
                    "message": bound_text(redact_text(str(exc), max_length=1024), 1024),
                }
            )
    return batch


__all__ = [
    "ADAPTER_VERSION",
    "SCHEMA_VERSION",
    "IngestionResult",
    "BatchIngestionResult",
    "ingest_source",
    "ingest_many",
]
