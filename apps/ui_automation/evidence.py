"""Content-addressed UI execution evidence helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping


EVIDENCE_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class UIEvidenceReference:
    evidence_type: str
    sha256: str
    size_bytes: int
    mime_type: str = "application/octet-stream"
    uri: str = ""
    availability: str = "AVAILABLE"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "evidence_type": self.evidence_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
            "uri": self.uri,
            "availability": self.availability,
            "metadata": dict(self.metadata),
        }


def from_bytes(
    payload: bytes,
    *,
    evidence_type: str,
    mime_type: str = "application/octet-stream",
    uri: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> UIEvidenceReference:
    """Create an evidence reference without embedding binary data in JSON."""

    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes")
    return UIEvidenceReference(
        evidence_type=evidence_type,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        mime_type=mime_type,
        uri=uri,
        metadata=dict(metadata or {}),
    )


def manifest(references: list[UIEvidenceReference]) -> list[dict[str, Any]]:
    """Return deterministic, digest-addressed evidence metadata."""

    return [item.to_contract() for item in sorted(references, key=lambda item: (item.evidence_type, item.sha256))]
