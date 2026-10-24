"""Shared contracts and safety helpers for read-only quality adapters.

Adapters deliberately depend on source models only inside their read methods.
That keeps this package importable with a minimal Django test settings module
and makes it difficult for an adapter to accidentally mutate a source ORM
instance.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, ClassVar, Mapping, Protocol, runtime_checkable


DEFAULT_MAX_PAYLOAD_DEPTH = 12
DEFAULT_MAX_PAYLOAD_ITEMS = 1000
DEFAULT_MAX_TEXT_LENGTH = 4096
REDACTION_VERSION = "1.0"


def _redaction_service() -> Any:
    from ..services import redaction

    return redaction


def _hashing_service() -> Any:
    from ..services import hashing

    return hashing


def _provenance_service() -> Any:
    from ..services import provenance

    return provenance


def _taxonomy_service() -> Any:
    from ..services import taxonomy

    return taxonomy


ADAPTER_VERSION = "1.0"
SCHEMA_VERSION = 1
RESULT_CONTRACT_VERSION = "1.0"
MAX_ITEMS = 1000
MAX_EVIDENCE = 2000
MAX_FAILURES = 1000
MAX_METRICS = 5000

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
ERROR = "ERROR"
INCONCLUSIVE = "INCONCLUSIVE"
NOT_RUN = "NOT_RUN"


def _json_safe(value: Any) -> Any:
    """Convert redacted contract values to deterministic JSON-safe primitives."""

    if isinstance(value, bytes):
        return "[BINARY]"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        pairs = [(str(key), _json_safe(item)) for key, item in value.items()]
        keys = [key for key, _value in pairs]
        if len(keys) != len(set(keys)):
            return {"__mapping_pairs__": [{"key": key, "value": item} for key, item in pairs]}
        return dict(pairs)
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "[NON_FINITE]"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class AdapterError(Exception):
    """Base error raised when a source cannot be normalized safely."""


class AdapterInputError(AdapterError):
    """Raised when an adapter invocation is missing a required identifier."""


class SourceNotFoundError(AdapterError):
    """Raised when the requested source execution does not exist."""

    def __init__(self, source_type: str, execution_id: Any) -> None:
        self.source_type = source_type
        self.execution_id = execution_id
        super().__init__(f"{source_type} execution {execution_id!r} was not found")


class SourceNotTerminalError(AdapterError):
    """Raised when a source execution is still mutable/running."""

    def __init__(self, source_type: str, execution_id: Any, status: Any) -> None:
        self.source_type = source_type
        self.execution_id = execution_id
        self.status = "" if status is None else str(status)
        super().__init__(
            f"{source_type} execution {execution_id!r} is not terminal "
            f"(status={self.status!r})"
        )


# Names used by callers in earlier design notes remain available as aliases.
MissingSourceIdError = AdapterInputError
MissingSourceIDError = AdapterInputError
MissingSourceError = SourceNotFoundError
SourceMissingError = SourceNotFoundError
NonTerminalSourceError = SourceNotTerminalError
SourceExecutionNotTerminalError = SourceNotTerminalError


@dataclass
class NormalizedItem:
    """A source test/request/case represented in the quality schema."""

    test_key: str
    outcome: str = INCONCLUSIVE
    source_item_type: str = ""
    source_item_id: str = ""
    name: str = ""
    source_status: str = ""
    attempt: int = 1
    duration_ms: int | None = None
    environment_fingerprint: str = "unknown"
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def item_key(self) -> str:
        return self.test_key

    @property
    def status(self) -> str:
        return self.outcome

    def to_contract(self) -> dict[str, Any]:
        return {
            "test_key": self.test_key,
            "outcome": self.outcome,
            "source_item_type": self.source_item_type,
            "source_item_id": self.source_item_id,
            "name": self.name,
            "source_status": self.source_status,
            "attempt": self.attempt,
            "duration_ms": self.duration_ms,
            "environment_fingerprint": self.environment_fingerprint,
            "details": _json_safe(payload(self.details)),
        }


@dataclass
class NormalizedEvidence:
    """Bounded, redacted evidence; no raw files are read by adapters."""

    evidence_type: str
    item_key: str = ""
    snippet: str = ""
    source_uri: str = ""
    source_field: str = ""
    sha256: str = ""
    mime_type: str = ""
    size_bytes: int | None = None
    availability: str = "AVAILABLE"
    provenance: dict[str, Any] = field(default_factory=dict)
    captured_at: datetime | None = None

    @property
    def source_item_key(self) -> str:
        return self.item_key

    def to_contract(self) -> dict[str, Any]:
        return {
            "evidence_type": self.evidence_type,
            "item_key": self.item_key,
            "snippet": self.snippet,
            "source_uri": self.source_uri,
            "source_field": self.source_field,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "availability": self.availability,
            "provenance": _json_safe(payload(self.provenance)),
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
        }


@dataclass
class NormalizedFailure:
    """Deterministically classified failure text and persistence metadata."""

    item_key: str = ""
    taxonomy_code: str = "UNKNOWN"
    subtype: str = ""
    phase: str = ""
    signature: str = ""
    source_error_code: str = ""
    message: str = ""
    stack_excerpt: str = ""
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    initial_taxonomy_code: str = "UNKNOWN"
    classifier: str = "RULE"
    confidence: float = 1.0
    rule_id: str = ""
    model_category: str = "UNKNOWN"

    @property
    def source_item_key(self) -> str:
        return self.item_key

    @property
    def code(self) -> str:
        return self.taxonomy_code

    def to_contract(self) -> dict[str, Any]:
        return {
            "item_key": self.item_key,
            "taxonomy_code": self.taxonomy_code,
            "subtype": self.subtype,
            "phase": self.phase,
            "signature": self.signature,
            "source_error_code": self.source_error_code,
            "message": _json_safe(payload(self.message)),
            "stack_excerpt": _json_safe(payload(self.stack_excerpt)),
            "retryable": self.retryable,
            "details": _json_safe(payload(self.details)),
            "initial_taxonomy_code": self.initial_taxonomy_code,
            "classifier": self.classifier,
            "confidence": self.confidence,
            "rule_id": self.rule_id,
            "model_category": self.model_category,
        }


@dataclass
class NormalizedMetric:
    """A deterministic numeric observation consumed by quality ingestion."""

    metric_key: str
    value: float
    item_key: str = ""
    sample_key: str = ""
    unit: str = ""
    aggregation: str = "VALUE"
    dimensions: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime | None = None

    @property
    def source_item_key(self) -> str:
        return self.item_key

    @property
    def key(self) -> str:
        return self.metric_key

    def to_contract(self) -> dict[str, Any]:
        return {
            "metric_key": self.metric_key,
            "value": self.value,
            "item_key": self.item_key,
            "sample_key": self.sample_key,
            "unit": self.unit,
            "aggregation": self.aggregation,
            "dimensions": _json_safe(payload(self.dimensions)),
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


@dataclass
class NormalizedRun:
    """Stable adapter output consumed by ``services.ingestion``.

    ``source_snapshot`` is the complete redacted input used for the source
    fingerprint. ``items``, ``evidence``, ``failures`` and ``metrics`` are
    detached lists so ingestion can persist them without touching source ORM
    instances.
    """

    source_type: str
    source_scope: str
    source_execution_id: str
    project_type: str = ""
    project_id: str = ""
    source_asset_id: str = ""
    source_asset_name: str = ""
    source_status: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    environment_fingerprint: str = "unknown"
    source_fingerprint: str = ""
    adapter_version: str = ADAPTER_VERSION
    redaction_version: str = REDACTION_VERSION
    schema_version: int = SCHEMA_VERSION
    outcome: str = INCONCLUSIVE
    quality_status: str = "INGESTED"
    summary: dict[str, Any] = field(default_factory=dict)
    source_snapshot: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    items: list[NormalizedItem] = field(default_factory=list)
    evidence: list[NormalizedEvidence] = field(default_factory=list)
    failures: list[NormalizedFailure] = field(default_factory=list)
    metrics: list[NormalizedMetric] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        """Alias useful to ingestion callers that use generic run IDs."""

        return self.source_execution_id

    @property
    def run_key(self) -> str:
        return self.source_execution_id

    @property
    def snapshot(self) -> dict[str, Any]:
        return self.source_snapshot

    @property
    def partial(self) -> bool:
        return self.quality_status == "PARTIAL" or bool(self.warnings)

    def to_contract(self) -> dict[str, Any]:
        return {
            "contract_version": RESULT_CONTRACT_VERSION,
            "schema_version": self.schema_version,
            "run": {
                "source_type": self.source_type,
                "source_scope": self.source_scope,
                "source_execution_id": self.source_execution_id,
                "project_type": self.project_type,
                "project_id": self.project_id,
                "source_asset_id": self.source_asset_id,
                "source_asset_name": self.source_asset_name,
                "source_status": self.source_status,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "duration_ms": self.duration_ms,
                "environment_fingerprint": self.environment_fingerprint,
                "source_fingerprint": self.source_fingerprint,
                "adapter_version": self.adapter_version,
                "redaction_version": self.redaction_version,
                "outcome": self.outcome,
                "quality_status": self.quality_status,
                "summary": _json_safe(payload(self.summary)),
                "source_snapshot": _json_safe(payload(self.source_snapshot)),
                "provenance": _json_safe(payload(self.provenance)),
                "warnings": list(self.warnings),
            },
            "items": [item.to_contract() for item in self.items],
            "evidence": [item.to_contract() for item in self.evidence],
            "failures": [item.to_contract() for item in self.failures],
            "metrics": [item.to_contract() for item in self.metrics],
        }


@runtime_checkable
class SourceAdapterProtocol(Protocol):
    source_type: str
    source_scope: str

    def read(self, execution_id: Any) -> NormalizedRun:
        ...


AdapterProtocol = SourceAdapterProtocol


# Explicit aliases make the protocol discoverable without forcing one naming
# convention on the ingestion service.
NormalizedRunItem = NormalizedItem
NormalizedRunEvidence = NormalizedEvidence
NormalizedRunFailure = NormalizedFailure
NormalizedRunMetric = NormalizedMetric


def value(item: Any, *names: str, default: Any = None) -> Any:
    """Read a mapping/model field without evaluating unsafe lazy attributes."""

    for name in names:
        if isinstance(item, Mapping) and name in item:
            candidate = item[name]
        else:
            try:
                candidate = getattr(item, name)
            except (AttributeError, TypeError):
                continue
            except Exception:  # pragma: no cover - lazy ORM relation edge case
                continue
        if candidate is not None:
            return candidate
    return default


def text(value_: Any, max_length: int = DEFAULT_MAX_TEXT_LENGTH) -> str:
    """Redact and bound public text snippets."""

    redaction = _redaction_service()
    return redaction.bound_text(
        redaction.redact_text(value_, max_length=max_length), max_length
    )


_PATH_FIELD_RE = re.compile(r"(?:^|_)(?:path|dir|directory|file|filename)$", re.IGNORECASE)


def _scrub_payload_paths(value_: Any, key: str = "") -> Any:
    """Remove local absolute paths from already redacted snapshots."""

    if isinstance(value_, Mapping):
        return {
            child_key: _scrub_payload_paths(child, str(child_key))
            for child_key, child in value_.items()
        }
    if isinstance(value_, list):
        return [_scrub_payload_paths(child, key) for child in value_]
    if isinstance(value_, tuple):
        return tuple(_scrub_payload_paths(child, key) for child in value_)
    if isinstance(value_, str) and _PATH_FIELD_RE.search(key):
        if _DRIVE_PATH_RE.match(value_) or value_.startswith(("\\\\", "/")):
            return "[REDACTED]"
    return value_


def payload(value_: Any, *, max_items: int = DEFAULT_MAX_PAYLOAD_ITEMS) -> Any:
    """Return a detached, recursively redacted and bounded payload."""

    redaction = _redaction_service()
    cleaned = redaction.redact_payload(
        value_,
        max_text_length=DEFAULT_MAX_TEXT_LENGTH,
        max_items=max_items,
        max_depth=DEFAULT_MAX_PAYLOAD_DEPTH,
    )
    return _scrub_payload_paths(cleaned)


_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_PUBLIC_PATH_PREFIXES = ("/media/", "/api/", "/reports/", "/static/")


def safe_reference(value_: Any, max_length: int = 1024) -> str:
    """Keep a URL or relative public reference, never a local absolute path."""

    if value_ is None:
        return ""
    candidate = text(value_, max_length=max_length).strip()
    if not candidate:
        return ""
    if candidate.lower().startswith(("data:", "file:")):
        return ""
    if _URI_RE.match(candidate):
        redaction = _redaction_service()
        return redaction.bound_text(
            redaction.redact_url(candidate, max_length=max_length), max_length
        )
    if _DRIVE_PATH_RE.match(candidate) or candidate.startswith("\\\\"):
        return ""
    if candidate.startswith("/"):
        # Root-relative web references are safe to expose only for known
        # public routes. A generic /tmp or /home path is not a URI here.
        return candidate if candidate.startswith(_PUBLIC_PATH_PREFIXES) else ""
    normalized = candidate.replace("\\", "/")
    try:
        parts = PurePosixPath(normalized).parts
    except (TypeError, ValueError):
        return ""
    if ".." in parts:
        return ""
    return "/".join(part for part in parts if part not in ("", "."))[:max_length]


def source_status(source: Any) -> str:
    """Return a normalized source status string."""

    raw = value(source, "status", "state", default="")
    raw = getattr(raw, "value", raw)
    return "" if raw is None else str(raw).strip().upper()


def require_execution_id(execution_id: Any) -> Any:
    if execution_id is None or (isinstance(execution_id, str) and not execution_id.strip()):
        raise AdapterInputError("source execution id is required")
    return execution_id


def ensure_terminal(
    source: Any,
    *,
    source_type: str,
    execution_id: Any,
    terminal_statuses: set[str] | frozenset[str],
) -> str:
    status = source_status(source)
    if status not in {str(item).upper() for item in terminal_statuses}:
        raise SourceNotTerminalError(source_type, execution_id, status or None)
    return status


def load_by_id(manager: Any, execution_id: Any, *, source_type: str) -> Any:
    """Load one row using a read-only queryset, tolerating simple test fakes."""

    try:
        queryset = manager.filter(pk=execution_id)
        source = queryset.first() if hasattr(queryset, "first") else next(iter(queryset), None)
    except (AttributeError, TypeError, ValueError):
        source = None
    if source is None:
        try:
            source = manager.get(pk=execution_id)
        except (TypeError, ValueError):
            source = None
        except Exception as exc:
            model = getattr(manager, "model", None)
            does_not_exist = getattr(model, "DoesNotExist", ()) if model is not None else ()
            if does_not_exist and isinstance(exc, does_not_exist):
                source = None
            elif exc.__class__.__name__ in {"DoesNotExist", "ObjectDoesNotExist"}:
                source = None
            else:
                raise
    if source is None:
        raise SourceNotFoundError(source_type, execution_id)
    return source


def duration_ms(started_at: Any, finished_at: Any, seconds: Any = None) -> int | None:
    if started_at is not None and finished_at is not None:
        try:
            return max(0, int((finished_at - started_at).total_seconds() * 1000))
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass
    if seconds is not None:
        try:
            return max(0, int(float(seconds) * 1000))
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def safe_number(value_: Any) -> float | None:
    if isinstance(value_, bool):
        return None
    try:
        number = float(value_)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def normalize_outcome(
    status: Any = None,
    *,
    passed: Any = None,
    failed: Any = None,
    skipped: Any = None,
    default: str = INCONCLUSIVE,
) -> str:
    """Map common runner values without inferring PASS from absence."""

    raw = status.value if isinstance(status, Enum) else status
    normalized = "" if raw is None else str(raw).strip().upper()
    if passed is True or str(passed).strip().lower() in {"true", "1", "yes", "passed", "pass"}:
        return PASS
    if failed is True or str(failed).strip().lower() in {"true", "1", "yes", "failed", "fail", "error"}:
        return FAIL
    if skipped is True or str(skipped).strip().lower() in {"true", "1", "yes", "skipped", "skip"}:
        return SKIP
    if normalized in {"PASS", "PASSED", "SUCCESS", "SUCCEEDED", "OK"}:
        return PASS
    if normalized in {"FAIL", "FAILED", "ERROR", "BROKEN", "TIMEOUT", "ABORTED"}:
        return FAIL
    if normalized in {"SKIP", "SKIPPED", "CANCELLED", "CANCELED"}:
        return SKIP
    if normalized in {"NOT_RUN", "NOTRUN", "PENDING"}:
        return NOT_RUN
    return default


def outcome_for_items(items: list[NormalizedItem], source_status: str = "") -> str:
    """Compute a conservative run outcome from normalized items."""

    outcomes = [item.outcome for item in items]
    if any(item in {FAIL, ERROR} for item in outcomes):
        return FAIL
    if outcomes and all(item == PASS for item in outcomes):
        return PASS
    if outcomes and all(item in {SKIP, NOT_RUN} for item in outcomes):
        return SKIP
    terminal = normalize_outcome(source_status)
    return terminal if terminal in {FAIL, SKIP} else INCONCLUSIVE


def evidence(
    evidence_type: str,
    *,
    item_key: str = "",
    snippet: Any = "",
    source_uri: Any = "",
    source_field: str = "",
    availability: str = "AVAILABLE",
    mime_type: str = "",
    captured_at: datetime | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> NormalizedEvidence:
    safe_snippet = text(snippet)
    safe_uri = safe_reference(source_uri)
    evidence_hash = _hashing_service().fingerprint(
        {"snippet": safe_snippet, "source_uri": safe_uri}
    )
    return NormalizedEvidence(
        evidence_type=evidence_type,
        item_key=str(item_key or ""),
        snippet=safe_snippet,
        source_uri=safe_uri,
        source_field=text(source_field, 255),
        sha256=evidence_hash,
        mime_type=text(mime_type, 128),
        size_bytes=len(safe_snippet.encode("utf-8")) if safe_snippet else None,
        availability=availability,
        provenance=dict(provenance or {}),
        captured_at=captured_at,
    )


def failure(
    error: Any,
    *,
    item_key: str = "",
    context: Mapping[str, Any] | None = None,
    phase: str = "",
    source_error_code: str = "",
    stack_excerpt: Any = "",
    details: Mapping[str, Any] | None = None,
) -> NormalizedFailure:
    classification = _taxonomy_service().classify_failure(
        error=error, context=context or {}, phase=phase
    )
    message = text(error if isinstance(error, str) else value(error, "message", default=str(error)))
    taxonomy = str(classification.get("taxonomy_code", "UNKNOWN"))
    subtype = str(classification.get("subtype", ""))
    return NormalizedFailure(
        item_key=str(item_key or ""),
        taxonomy_code=taxonomy,
        subtype=subtype,
        phase=text(phase, 64),
        signature=_hashing_service().signature(taxonomy, subtype, source_error_code, message),
        source_error_code=text(source_error_code, 128),
        message=message,
        stack_excerpt=text(stack_excerpt),
        retryable=bool(classification.get("retryable", False)),
        details=dict(payload(details or {})),
        initial_taxonomy_code=taxonomy,
        classifier=str(classification.get("classifier", "RULE")),
        confidence=float(classification.get("confidence", 1.0) or 1.0),
        rule_id=str(classification.get("rule_id", "")),
        model_category=str(classification.get("model_category", classification.get("category", "UNKNOWN"))),
    )


def finalize_run(run: NormalizedRun) -> NormalizedRun:
    """Fill stable fingerprint/provenance fields after source collection."""

    run.source_snapshot = dict(payload(run.source_snapshot or {}))
    if not run.source_fingerprint:
        run.source_fingerprint = _provenance_service().fingerprint_source(run.source_snapshot)
    if not run.provenance:
        run.provenance = _provenance_service().build_provenance(
            payload=run.source_snapshot,
            source_type=run.source_type,
            source_scope=run.source_scope,
            source_execution_id=run.source_execution_id,
            project_type=run.project_type,
            project_id=run.project_id,
            adapter_version=run.adapter_version,
            schema_version=run.schema_version,
            metadata={
                "source_status": run.source_status,
                "source_asset_id": run.source_asset_id,
                "source_asset_name": run.source_asset_name,
            },
        )
    run.warnings = list(dict.fromkeys(text(warning, 255) for warning in run.warnings if warning))
    run.quality_status = "PARTIAL" if run.warnings else "INGESTED"
    if not run.outcome:
        run.outcome = outcome_for_items(run.items, run.source_status)
    return run


class BaseSourceAdapter:
    """Read-only adapter base consumed through ``read``/``adapt``."""

    source_type: ClassVar[str] = ""
    source_scope: ClassVar[str] = ""
    terminal_statuses: ClassVar[frozenset[str]] = frozenset()
    adapter_version: ClassVar[str] = ADAPTER_VERSION

    def __init__(self, execution_id: Any = None, *, max_items: int = MAX_ITEMS) -> None:
        self.execution_id = execution_id
        self.max_items = max(1, int(max_items))

    def _resolve_execution_id(self, execution_id: Any = None) -> Any:
        resolved = self.execution_id if execution_id is None else execution_id
        require_execution_id(resolved)
        return resolved

    def read(self, execution_id: Any = None) -> NormalizedRun:
        """Normalize one terminal source execution without writing it."""

        execution_id = self._resolve_execution_id(execution_id)
        return self.normalize(execution_id)

    def adapt(self, execution_id: Any = None) -> NormalizedRun:
        return self.read(execution_id)

    def collect(self, execution_id: Any = None) -> NormalizedRun:
        return self.read(execution_id)

    def normalize(self, execution_id: Any = None) -> NormalizedRun:
        raise NotImplementedError

    def _ensure_terminal(self, source: Any, execution_id: Any) -> str:
        return ensure_terminal(
            source,
            source_type=self.source_type,
            execution_id=execution_id,
            terminal_statuses=self.terminal_statuses,
        )


__all__ = [
    "ADAPTER_VERSION",
    "SCHEMA_VERSION",
    "MAX_ITEMS",
    "MAX_EVIDENCE",
    "MAX_FAILURES",
    "MAX_METRICS",
    "PASS",
    "FAIL",
    "SKIP",
    "ERROR",
    "INCONCLUSIVE",
    "NOT_RUN",
    "AdapterError",
    "AdapterInputError",
    "SourceNotFoundError",
    "SourceNotTerminalError",
    "MissingSourceIdError",
    "MissingSourceIDError",
    "MissingSourceError",
    "SourceMissingError",
    "NonTerminalSourceError",
    "SourceExecutionNotTerminalError",
    "SourceAdapterProtocol",
    "AdapterProtocol",
    "NormalizedItem",
    "NormalizedRunItem",
    "NormalizedEvidence",
    "NormalizedRunEvidence",
    "NormalizedFailure",
    "NormalizedRunFailure",
    "NormalizedMetric",
    "NormalizedRunMetric",
    "NormalizedRun",
    "BaseSourceAdapter",
    "value",
    "text",
    "payload",
    "safe_reference",
    "source_status",
    "require_execution_id",
    "ensure_terminal",
    "load_by_id",
    "duration_ms",
    "safe_number",
    "normalize_outcome",
    "outcome_for_items",
    "evidence",
    "failure",
    "finalize_run",
]
