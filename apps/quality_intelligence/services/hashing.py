"""Canonical JSON and SHA-256 helpers used by quality intelligence.

All functions are pure and deterministic.  Canonicalization is intentionally
small: it normalizes the JSON-compatible values emitted by runners and gives
common Python-only values (datetime, Decimal, Enum, bytes, sets) a stable
representation without consulting process state or an external provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


CANONICAL_JSON_KWARGS = {
    "ensure_ascii": False,
    "allow_nan": False,
    "separators": (",", ":"),
    "sort_keys": True,
}


def _canonical_value(value: Any) -> Any:
    """Convert a supported value to a JSON-safe deterministic structure."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return {"__float__": repr(value)}
        return value
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, (datetime, date, time)):
        # A timezone-naive datetime is left explicit rather than silently
        # assuming a machine-local timezone.
        if isinstance(value, datetime) and value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.isoformat()
    if isinstance(value, Decimal):
        return {"__decimal__": format(value, "f")}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "__bytes__": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, Mapping):
        # JSON object keys are strings.  Stringifying here avoids dependence on
        # insertion order and supports enum/integer keys consistently.
        entries = [
            (str(key), _canonical_value(child)) for key, child in value.items()
        ]
        entries.sort(key=lambda entry: entry[0])
        return {key: child for key, child in entries}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(child) for child in value]
    if isinstance(value, (set, frozenset)):
        values = [_canonical_value(child) for child in value]
        values.sort(key=lambda child: json.dumps(child, **CANONICAL_JSON_KWARGS))
        return values
    if hasattr(value, "__dict__"):
        return _canonical_value(vars(value))
    return str(value)


def canonicalize(value: Any) -> Any:
    """Return a new JSON-safe canonical representation of ``value``."""

    return _canonical_value(value)


def canonical_json(value: Any) -> str:
    """Serialize a value with stable key ordering and no insignificant space."""

    return json.dumps(canonicalize(value), **CANONICAL_JSON_KWARGS)


def canonical_bytes(value: Any) -> bytes:
    """UTF-8 bytes for :func:`canonical_json`."""

    return canonical_json(value).encode("utf-8")


def sha256_bytes(value: bytes | bytearray | memoryview) -> str:
    """Return a lowercase SHA-256 digest for bytes."""

    return hashlib.sha256(bytes(value)).hexdigest()


def sha256_hex(value: Any) -> str:
    """Hash text/bytes directly and structured values canonically."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        return sha256_bytes(value)
    if isinstance(value, str):
        return sha256_bytes(value.encode("utf-8"))
    return sha256_bytes(canonical_bytes(value))


def fingerprint(value: Any) -> str:
    """Return the SHA-256 fingerprint of a canonical structured value."""

    return sha256_bytes(canonical_bytes(value))


def fingerprint_payload(value: Any, *, redact: bool = True) -> str:
    """Fingerprint a payload, redacting sensitive fields by default.

    Generic :func:`fingerprint` remains available for callers that already
    control their input.  Ingestion code should use this payload helper so a
    secret-bearing mapping and its safe redacted equivalent have the same
    provenance semantics without ever serializing the secret into a record.
    """

    if redact:
        # Lazy import keeps this low-level module independent for callers that
        # only need canonicalization while avoiding an import cycle.
        from .redaction import redact_payload

        value = redact_payload(value)
    return fingerprint(value)


def signature(*parts: Any) -> str:
    """Build a stable SHA-256 signature from one or more structured parts.

    Multiple parts are represented as a list, which keeps boundaries explicit
    (``signature("ab", "c")`` cannot collide with an ad-hoc concatenation of
    ``"a", "bc"``).
    """

    if len(parts) == 1:
        return fingerprint(parts[0])
    return fingerprint(list(parts))


def sha256_fingerprint(value: Any) -> str:
    """Compatibility alias for :func:`fingerprint`."""

    return fingerprint(value)


def make_fingerprint(value: Any) -> str:
    """Compatibility alias for :func:`fingerprint`."""

    return fingerprint(value)


def make_signature(*parts: Any) -> str:
    """Compatibility alias for :func:`signature`."""

    return signature(*parts)


__all__ = [
    "CANONICAL_JSON_KWARGS",
    "canonical_bytes",
    "canonical_json",
    "canonicalize",
    "fingerprint",
    "fingerprint_payload",
    "make_fingerprint",
    "make_signature",
    "sha256_bytes",
    "sha256_fingerprint",
    "sha256_hex",
    "signature",
]
