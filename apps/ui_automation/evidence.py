"""Content-addressed UI execution evidence helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
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


def from_file(
    path: str | Path,
    *,
    evidence_type: str,
    mime_type: str = "application/octet-stream",
    uri_prefix: str = "urn:sha256:",
    metadata: Mapping[str, Any] | None = None,
) -> UIEvidenceReference:
    """Create a content-addressed reference for a file without retaining its path."""

    digest = hashlib.sha256()
    size_bytes = 0
    with Path(path).open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(64 * 1024), b""):
            digest.update(chunk)
            size_bytes += len(chunk)
    sha256 = digest.hexdigest()
    return UIEvidenceReference(
        evidence_type=evidence_type,
        sha256=sha256,
        size_bytes=size_bytes,
        mime_type=mime_type,
        uri=f"{uri_prefix}{sha256}",
        metadata=dict(metadata or {}),
    )


def from_data_url(
    data_url: str,
    *,
    evidence_type: str = "SCREENSHOT",
    uri_prefix: str = "urn:sha256:",
    metadata: Mapping[str, Any] | None = None,
) -> UIEvidenceReference:
    """Decode a base64 data URL and identify its bytes without exposing a path."""

    match = re.fullmatch(
        r"data:([^;,]+);base64,(.*)",
        str(data_url or ""),
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise ValueError("expected a base64 data URL")
    try:
        payload = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 evidence data") from exc
    reference = from_bytes(
        payload,
        evidence_type=evidence_type,
        mime_type=match.group(1).lower(),
        metadata=metadata,
    )
    return UIEvidenceReference(
        evidence_type=reference.evidence_type,
        sha256=reference.sha256,
        size_bytes=reference.size_bytes,
        mime_type=reference.mime_type,
        uri=f"{uri_prefix}{reference.sha256}",
        availability=reference.availability,
        metadata=reference.metadata,
    )


def project_screenshots(screenshots, *, producer: str, execution_id=None, item_id=None):
    """Attach content-addressed metadata to stored screenshot records."""

    projected = []
    for screenshot in screenshots if isinstance(screenshots, list) else []:
        if not isinstance(screenshot, Mapping):
            projected.append(screenshot)
            continue
        item = dict(screenshot)
        data_url = item.get("url")
        if isinstance(data_url, str) and data_url.lower().startswith("data:"):
            metadata = {
                "producer": str(producer),
                "execution_id": str(execution_id) if execution_id is not None else "",
                "item_id": str(item_id) if item_id is not None else "",
                "step_number": item.get("step_number"),
                "captured_at": item.get("timestamp", ""),
            }
            try:
                item["evidence_reference"] = from_data_url(
                    data_url,
                    metadata=metadata,
                ).to_contract()
            except ValueError:
                pass
        projected.append(item)
    return projected


def manifest(references: list[UIEvidenceReference]) -> list[dict[str, Any]]:
    """Return deterministic, digest-addressed evidence metadata."""

    return [item.to_contract() for item in sorted(references, key=lambda item: (item.evidence_type, item.sha256))]
