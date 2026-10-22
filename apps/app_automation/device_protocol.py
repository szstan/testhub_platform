"""Versioned device-agent messages and lease fencing helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Mapping


DEVICE_PROTOCOL_VERSION = "1.0"


class DeviceProtocolError(ValueError):
    """Raised when an agent message cannot be accepted safely."""


@dataclass(frozen=True)
class DeviceHeartbeat:
    device_id: str
    status: str
    health_status: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    agent_id: str = ""
    agent_version: str = DEVICE_PROTOCOL_VERSION
    lease_token: str = ""

    def to_message(self) -> dict[str, Any]:
        return {
            "protocol_version": DEVICE_PROTOCOL_VERSION,
            "device_id": self.device_id,
            "status": self.status,
            "health_status": self.health_status,
            "capabilities": dict(self.capabilities),
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
        }


def parse_heartbeat(payload: Mapping[str, Any]) -> DeviceHeartbeat:
    """Validate an agent heartbeat without persisting or exposing its token."""

    if not isinstance(payload, Mapping):
        raise DeviceProtocolError("heartbeat must be an object")
    if payload.get("protocol_version") != DEVICE_PROTOCOL_VERSION:
        raise DeviceProtocolError("unsupported protocol version")
    device_id = str(payload.get("device_id") or "").strip()
    if not device_id or len(device_id) > 255:
        raise DeviceProtocolError("device_id is required")
    status = str(payload.get("status") or "offline").lower()
    health_status = str(payload.get("health_status") or "unknown").lower()
    if status not in {"available", "online", "locked", "offline", "maintenance"}:
        raise DeviceProtocolError("unsupported device status")
    if health_status not in {"healthy", "unhealthy", "unknown"}:
        raise DeviceProtocolError("unsupported health status")
    capabilities = payload.get("capabilities") or {}
    if not isinstance(capabilities, Mapping):
        raise DeviceProtocolError("capabilities must be an object")
    return DeviceHeartbeat(
        device_id=device_id,
        status=status,
        health_status=health_status,
        capabilities=dict(capabilities),
        agent_id=str(payload.get("agent_id") or "")[:255],
        agent_version=str(payload.get("agent_version") or DEVICE_PROTOCOL_VERSION)[:50],
        lease_token=str(payload.get("lease_token") or ""),
    )


def capability_fingerprint(capabilities: Mapping[str, Any]) -> str:
    """Return a stable digest for scheduling and evidence correlation."""

    normalized = json.dumps(dict(capabilities), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def lease_fence_matches(expected_token: str, presented_token: str) -> bool:
    """Compare fencing tokens without leaking timing information."""

    return bool(expected_token) and hmac.compare_digest(expected_token, presented_token or "")
