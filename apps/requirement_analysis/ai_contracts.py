"""Versioned, redacted contracts for AI-generated test-case drafts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from apps.quality_intelligence.services.redaction import redact_payload


AI_CONTRACT_VERSION = "1.0"


class AIContractError(ValueError):
    """Raised when an AI response cannot be accepted as a structured draft."""


def _bounded_text(value: Any, *, field: str, limit: int = 8000) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise AIContractError(f"{field} is required")
    if len(text) > limit:
        raise AIContractError(f"{field} exceeds {limit} characters")
    return text


def _steps(value: Any) -> list[str]:
    if isinstance(value, str):
        rows = [line.strip() for line in value.splitlines() if line.strip()]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        rows = [str(item).strip() for item in value if str(item).strip()]
    else:
        rows = []
    if not rows:
        raise AIContractError("steps must contain at least one item")
    return rows[:100]


def _string_list(value: Any, *, field: str, limit: int = 20, item_limit: int = 200) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, Mapping)):
        values = list(value)
    else:
        raise AIContractError(f"{field} must be a string or array")
    return [str(item).strip()[:item_limit] for item in values[:limit] if str(item).strip()]


def _confidence(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError) as exc:
        raise AIContractError("confidence must be a number") from exc


def _extract_json(text: str) -> Any:
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.IGNORECASE | re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AIContractError("AI output is not valid JSON") from exc


def parse_draft(raw: Any) -> dict[str, Any]:
    """Parse and validate an AI draft without allowing arbitrary model fields."""

    payload = _extract_json(raw) if isinstance(raw, str) else raw
    if isinstance(payload, Mapping):
        raw_cases = payload.get("test_cases", payload.get("cases"))
        coverage_notes = payload.get("coverage_notes", [])
        source_refs = payload.get("source_refs", [])
    elif isinstance(payload, list):
        raw_cases = payload
        coverage_notes = []
        source_refs = []
    else:
        raise AIContractError("draft must be an object or list")

    if not isinstance(raw_cases, list) or not raw_cases:
        raise AIContractError("draft must contain test_cases")

    cases: list[dict[str, Any]] = []
    for index, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, Mapping):
            raise AIContractError(f"test_cases[{index - 1}] must be an object")
        case_id = str(raw_case.get("case_id") or f"AI-{index:03d}").strip()
        priority = str(raw_case.get("priority") or "P2").upper().strip()
        if priority not in {"P0", "P1", "P2", "P3"}:
            priority = "P2"
        cases.append(
            {
                "case_id": _bounded_text(case_id, field="case_id", limit=80),
                "title": _bounded_text(raw_case.get("title"), field="title", limit=300),
                "priority": priority,
                "precondition": _bounded_text(
                    raw_case.get("precondition", "无"), field="precondition", limit=2000
                ),
                "steps": _steps(raw_case.get("steps", raw_case.get("test_steps"))),
                "expected_result": _bounded_text(
                    raw_case.get("expected_result"), field="expected_result", limit=4000
                ),
                "source_refs": _string_list(raw_case.get("source_refs"), field="source_refs"),
                "confidence": _confidence(raw_case.get("confidence")),
            }
        )

    safe = redact_payload(
        {
            "contract_version": AI_CONTRACT_VERSION,
            "cases": cases,
            "coverage_notes": coverage_notes,
            "source_refs": source_refs,
        },
        max_text_length=8000,
        max_items=1000,
        max_depth=12,
    )
    fingerprint = hashlib.sha256(
        json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **safe,
        "contract_version": AI_CONTRACT_VERSION,
        "draft_fingerprint": fingerprint,
    }


def render_markdown(draft: Mapping[str, Any]) -> str:
    """Render an accepted draft deterministically for legacy text fields/UI."""

    lines: list[str] = []
    for case in draft.get("cases", []):
        lines.extend(
            [
                f"## {case['case_id']}: {case['title']}",
                f"- Priority: {case['priority']}",
                "",
                "### Preconditions",
                case["precondition"],
                "",
                "### Steps",
            ]
        )
        lines.extend(f"{index}. {step}" for index, step in enumerate(case["steps"], start=1))
        lines.extend(["", "### Expected result", case["expected_result"], ""])
    return "\n".join(lines).strip()


class NormalizedGenerationOutput(str):
    """String-compatible output carrying source and audit metadata."""

    def __new__(cls, value: str, *, raw_output: str, metadata: dict[str, Any]):
        instance = str.__new__(cls, value)
        instance.raw_output = raw_output
        instance.metadata = metadata
        return instance


def normalize_generation_output(raw: Any) -> tuple[NormalizedGenerationOutput, dict[str, Any] | None]:
    """Return compatible text plus metadata without losing the raw model output."""

    if not isinstance(raw, str):
        raise AIContractError("generation output must be text")
    try:
        draft = parse_draft(raw)
    except AIContractError:
        metadata = {
            "contract_version": "legacy",
            "structured": False,
            "output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }
        return NormalizedGenerationOutput(raw, raw_output=raw, metadata=metadata), None
    metadata = {
        "contract_version": draft["contract_version"],
        "structured": True,
        "draft_fingerprint": draft["draft_fingerprint"],
        "case_count": len(draft["cases"]),
    }
    return NormalizedGenerationOutput(
        render_markdown(draft), raw_output=raw, metadata=metadata
    ), draft


def summarize_generation_output(raw: str) -> dict[str, Any]:
    """Return audit metadata without persisting model output or secrets."""

    if isinstance(raw, NormalizedGenerationOutput):
        return dict(raw.metadata)
    try:
        draft = parse_draft(raw)
    except AIContractError:
        return {
            "contract_version": "legacy",
            "structured": False,
            "output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }
    return {
        "contract_version": draft["contract_version"],
        "structured": True,
        "draft_fingerprint": draft["draft_fingerprint"],
        "case_count": len(draft["cases"]),
    }
