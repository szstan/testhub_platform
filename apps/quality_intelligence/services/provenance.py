"""Provenance records for quality-intelligence evidence.

Provenance is metadata, not a copy of the captured payload.  Payloads are
redacted and bounded before their fingerprint is calculated; safe source
metadata is retained in a separate bounded mapping.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .hashing import fingerprint
from .redaction import (
    DEFAULT_MAX_PAYLOAD_DEPTH,
    DEFAULT_MAX_PAYLOAD_ITEMS,
    DEFAULT_MAX_TEXT_LENGTH,
    REDACTION_VERSION,
    redact_payload,
)


PROVENANCE_VERSION = "1.0"
_MISSING = object()


def _clean_metadata(
    value: Any,
    *,
    max_text_length: int,
    max_items: int,
    max_depth: int,
) -> Any:
    """Redact and bound metadata while retaining non-sensitive fields."""

    if value is None:
        return {}
    cleaned = redact_payload(
        value,
        max_text_length=max_text_length,
        max_items=max_items,
        max_depth=max_depth,
    )
    if isinstance(cleaned, Mapping):
        return dict(cleaned)
    return {"value": cleaned}


def fingerprint_source(
    payload: Any,
    *,
    max_text_length: int = DEFAULT_MAX_TEXT_LENGTH,
    max_items: int = DEFAULT_MAX_PAYLOAD_ITEMS,
    max_depth: int = DEFAULT_MAX_PAYLOAD_DEPTH,
) -> str:
    """Redact/bound a source payload, then return its canonical SHA-256 hash."""

    safe_payload = redact_payload(
        payload,
        max_text_length=max_text_length,
        max_items=max_items,
        max_depth=max_depth,
    )
    return fingerprint(safe_payload)


def fingerprint_payload(
    payload: Any,
    *,
    max_text_length: int = DEFAULT_MAX_TEXT_LENGTH,
    max_items: int = DEFAULT_MAX_PAYLOAD_ITEMS,
    max_depth: int = DEFAULT_MAX_PAYLOAD_DEPTH,
) -> str:
    """Return a redacted, bounded payload fingerprint for provenance."""

    return fingerprint_source(
        payload,
        max_text_length=max_text_length,
        max_items=max_items,
        max_depth=max_depth,
    )


def build_provenance(
    source: Mapping[str, Any] | str | None = None,
    payload: Any = _MISSING,
    *,
    source_type: str | None = None,
    source_scope: str | None = None,
    source_execution_id: str | None = None,
    source_item_id: str | None = None,
    project_type: str | None = None,
    project_id: str | None = None,
    adapter_version: str | None = None,
    schema_version: int | None = None,
    redaction_version: str = REDACTION_VERSION,
    metadata: Mapping[str, Any] | None = None,
    max_text_length: int = DEFAULT_MAX_TEXT_LENGTH,
    max_items: int = DEFAULT_MAX_PAYLOAD_ITEMS,
    max_depth: int = DEFAULT_MAX_PAYLOAD_DEPTH,
    **safe_metadata: Any,
) -> dict[str, Any]:
    """Build a stable, redacted provenance mapping.

    ``source`` may be a source-type string or a mapping of source metadata.
    Explicit keyword fields take precedence over the mapping.  The payload is
    never returned; when supplied, only its redacted canonical fingerprint is
    included.
    """

    known_source_fields = {
        "source_type",
        "source_scope",
        "source_execution_id",
        "source_item_id",
        "project_type",
        "project_id",
        "adapter_version",
        "schema_version",
        "metadata",
        "source_fingerprint",
    }
    # Also accept the natural ``build_provenance(payload, ...)`` form when
    # the first positional value is clearly payload rather than source data.
    if payload is _MISSING and source is not None:
        if not isinstance(source, (Mapping, str)):
            payload, source = source, None
        elif isinstance(source, Mapping) and not set(source).intersection(known_source_fields):
            payload, source = source, None

    source_values: dict[str, Any] = {}
    if isinstance(source, Mapping):
        source_values.update(source)
    elif source is not None:
        source_values["source_type"] = source

    def choose(explicit: Any, key: str) -> Any:
        if explicit is not None:
            return explicit
        return source_values.get(key)

    result: dict[str, Any] = {
        "provenance_version": PROVENANCE_VERSION,
        "redaction_version": redaction_version,
    }

    field_values = {
        "source_type": choose(source_type, "source_type"),
        "source_scope": choose(source_scope, "source_scope"),
        "source_execution_id": choose(source_execution_id, "source_execution_id"),
        "source_item_id": choose(source_item_id, "source_item_id"),
        "project_type": choose(project_type, "project_type"),
        "project_id": choose(project_id, "project_id"),
        "adapter_version": choose(adapter_version, "adapter_version"),
        "schema_version": choose(schema_version, "schema_version"),
    }
    for key, value in field_values.items():
        if value is not None and value != "":
            # These are identifiers, but applying the same text bound keeps a
            # malformed source record from producing an unbounded JSON field.
            result[key] = (
                int(value)
                if key == "schema_version" and isinstance(value, (int, float))
                else str(value)[:max_text_length]
            )

    supplied_metadata: dict[str, Any] = {}
    if isinstance(source_values.get("metadata"), Mapping):
        supplied_metadata.update(source_values["metadata"])
    # Preserve source-specific safe metadata (runner name, shard, region,
    # correlation id, and similar fields) without allowing it to bypass the
    # same redaction and bounds as explicit metadata.
    supplied_metadata.update(
        {
            key: value
            for key, value in source_values.items()
            if key not in known_source_fields
        }
    )
    if isinstance(metadata, Mapping):
        supplied_metadata.update(metadata)
    supplied_metadata.update(safe_metadata)
    if supplied_metadata:
        result["metadata"] = _clean_metadata(
            supplied_metadata,
            max_text_length=max_text_length,
            max_items=max_items,
            max_depth=max_depth,
        )

    if payload is not _MISSING:
        result["source_fingerprint"] = fingerprint_source(
            payload,
            max_text_length=max_text_length,
            max_items=max_items,
            max_depth=max_depth,
        )
        # ``fingerprint`` is a convenient generic name for adapters that do
        # not distinguish source and evidence fingerprints.
        result["fingerprint"] = result["source_fingerprint"]
    elif source_values.get("source_fingerprint"):
        result["source_fingerprint"] = str(source_values["source_fingerprint"])
        result["fingerprint"] = result["source_fingerprint"]

    return result


def provenance_for(
    payload: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build provenance for a payload (named convenience wrapper)."""

    return build_provenance(payload=payload, **kwargs)


def make_provenance(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for :func:`build_provenance`."""

    return build_provenance(*args, **kwargs)


__all__ = [
    "PROVENANCE_VERSION",
    "build_provenance",
    "fingerprint_payload",
    "fingerprint_source",
    "make_provenance",
    "provenance_for",
]
