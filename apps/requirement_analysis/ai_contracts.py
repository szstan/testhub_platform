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


def _case_field(label: str) -> str | None:
    normalized = re.sub(r"[\s_*-]+", "", label).strip("：:").lower()
    if any(value in normalized for value in ("caseid", "用例id", "用例编号", "编号", "序号")):
        return "case_id"
    if any(value in normalized for value in ("title", "scenario", "测试目标", "测试场景", "用例名称", "场景", "标题")):
        return "title"
    if any(value in normalized for value in ("precondition", "前置条件", "前提条件", "前置", "前提")):
        return "preconditions"
    if any(value in normalized for value in ("steps", "teststeps", "测试步骤", "操作步骤", "执行步骤", "步骤")):
        return "steps"
    if any(value in normalized for value in ("expectedresult", "expected", "预期结果", "期望结果", "预期")):
        return "expected_result"
    if "priority" in normalized or "优先级" in normalized:
        return "priority"
    return None


def _priority(value: Any) -> str:
    normalized = str(value or "P2").strip().lower()
    return {
        "p0": "P0", "critical": "P0", "highest": "P0", "最高": "P0", "紧急": "P0",
        "p1": "P1", "high": "P1", "高": "P1",
        "p2": "P2", "medium": "P2", "normal": "P2", "中": "P2",
        "p3": "P3", "low": "P3", "低": "P3",
    }.get(normalized, "P2")


def _case_steps(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        source = [str(item) for item in value]
    else:
        source = re.split(r"\r?\n|<br\s*/?>", str(value or ""), flags=re.IGNORECASE)
    steps = []
    for item in source:
        step = re.sub(r"^\s*(?:[-*+]\s*|\d+[.、)）]\s*)", "", item).strip()
        if step:
            steps.append(step)
    return steps[:100]


def _canonical_cases(raw_cases: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases, start=1):
        title = str(raw_case.get("title") or raw_case.get("scenario") or "").strip()
        if not title:
            continue
        case_id = str(raw_case.get("case_id") or raw_case.get("caseId") or "").strip()
        if not case_id:
            case_id = f"CASE-{index:03d}"
        base_case_id = case_id
        suffix = 2
        while case_id in seen_ids:
            case_id = f"{base_case_id}-{suffix}"
            suffix += 1
        seen_ids.add(case_id)
        preconditions = raw_case.get("preconditions", raw_case.get("precondition", ""))
        if isinstance(preconditions, Sequence) and not isinstance(preconditions, (str, bytes, bytearray)):
            preconditions = "\n".join(str(item).strip() for item in preconditions if str(item).strip())
        cases.append({
            "case_id": case_id,
            "title": title,
            "priority": _priority(raw_case.get("priority")),
            "preconditions": str(preconditions or "").strip(),
            "steps": _case_steps(raw_case.get("steps", raw_case.get("test_steps", ""))),
            "expected_result": str(raw_case.get("expected_result", raw_case.get("expected", "")) or "").strip(),
        })
    return cases


def _table_cells(line: str) -> list[str]:
    escaped_pipe = "___CASE_PIPE___"
    processed = line.strip().replace(r"\|", escaped_pipe)
    if processed.startswith("|"):
        processed = processed[1:]
    if processed.endswith("|"):
        processed = processed[:-1]
    return [cell.replace(escaped_pipe, "|").replace("&#124;", "|").strip() for cell in processed.split("|")]


def _parse_case_table(lines: list[str]) -> list[dict[str, Any]]:
    for index, line in enumerate(lines[:-1]):
        if "|" not in line or "|" not in lines[index + 1]:
            continue
        headers = _table_cells(line)
        fields = [_case_field(header) for header in headers]
        separator = re.fullmatch(r"\s*\|?\s*:?-{2,}[-:\s|]*\|?\s*", lines[index + 1])
        if "title" not in fields or not separator:
            continue
        raw_cases = []
        for row in lines[index + 2:]:
            if "|" not in row:
                break
            cells = _table_cells(row)
            if len(cells) < len(fields):
                continue
            raw_case = {}
            for field, value in zip(fields, cells):
                if field:
                    raw_case[field] = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
            if raw_case.get("title"):
                raw_cases.append(raw_case)
        if raw_cases:
            return raw_cases
    return []


def _read_case_fields(lines: Sequence[str]) -> dict[str, Any]:
    values: dict[str, list[str]] = {}
    active_field: str | None = None
    for raw_line in lines:
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", raw_line).strip()
        if not line:
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            active_field = _case_field(heading.group(1))
            continue
        label = re.match(r"^(?:[-*+]\s*)?([^:：]{1,40})\s*[:：]\s*(.*)$", line)
        if label:
            field = _case_field(label.group(1))
            if field:
                active_field = field
                if label.group(2).strip():
                    values.setdefault(field, []).append(label.group(2).strip())
                continue
        if active_field:
            values.setdefault(active_field, []).append(line)
    result = {field: "\n".join(items).strip() for field, items in values.items()}
    if "steps" in result:
        result["steps"] = _case_steps(result["steps"])
    return result


def _parse_markdown_cases(lines: list[str]) -> list[dict[str, Any]]:
    heading_rows = []
    container_titles = {"test cases", "test case", "测试用例", "测试用例列表", "测试结果", "用例列表"}
    for index, line in enumerate(lines):
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line.strip())
        if not heading:
            continue
        level, label = len(heading.group(1)), heading.group(2).strip()
        case_id = ""
        title = label
        id_match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,79})\s*[:：]\s*(.+)$", label)
        if id_match:
            case_id, title = id_match.groups()
        marker = bool(re.search(r"测试用例|test\s*case|用例\s*\d+", label, re.IGNORECASE))
        if level <= 2 and label.casefold() not in container_titles:
            heading_rows.append((index, case_id, title))
        elif level == 3 and (case_id or marker):
            heading_rows.append((index, case_id, title))

    raw_cases = []
    for position, (line_index, case_id, title) in enumerate(heading_rows):
        end = heading_rows[position + 1][0] if position + 1 < len(heading_rows) else len(lines)
        fields = _read_case_fields(lines[line_index + 1:end])
        if not case_id and not (fields.get("steps") or fields.get("expected_result") or fields.get("preconditions")):
            continue
        raw_cases.append({"case_id": case_id, "title": title, **fields})
    return raw_cases


def _parse_legacy_text(lines: list[str]) -> list[dict[str, Any]]:
    raw_cases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    active_field: str | None = None
    known_labels = {"case_id", "title", "preconditions", "steps", "expected_result", "priority"}
    for raw_line in lines:
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", raw_line).strip()
        if not line:
            continue
        start = re.match(r"^(?:测试用例|用例|test\s*case)\s*(?:编号|id)?\s*([A-Za-z0-9_-]+)?\s*[:：-]\s*(.+)$", line, re.IGNORECASE)
        id_start = re.match(r"^((?:TC|AI|CASE|TEST)[-_]?[A-Za-z0-9_-]+)\s*[:：]\s*(.+)$", line, re.IGNORECASE)
        numbered_start = re.match(r"^(\d{1,3})[.、)]\s+(.+)$", line)
        new_case = start or id_start
        if new_case:
            if current:
                raw_cases.append(current)
            if start:
                current = {"case_id": start.group(1) or "", "title": start.group(2).strip()}
            else:
                current = {"case_id": id_start.group(1), "title": id_start.group(2).strip()}
            active_field = None
            continue
        if numbered_start and (current is None or active_field != "steps"):
            if current:
                raw_cases.append(current)
            current = {"case_id": "", "title": numbered_start.group(2).strip()}
            active_field = None
            continue
        label = re.match(r"^(?:[-*+]\s*)?([^:：]{1,40})\s*[:：]\s*(.*)$", line)
        if label:
            field = _case_field(label.group(1))
            if field in known_labels:
                if current is None:
                    continue
                active_field = field
                current.setdefault(field, "")
                if label.group(2).strip():
                    current[field] = (str(current[field]) + "\n" + label.group(2).strip()).strip()
                continue
        if current and active_field:
            if active_field == "steps":
                existing_steps = current.get(active_field, [])
                if not isinstance(existing_steps, list):
                    existing_steps = [str(existing_steps)] if existing_steps else []
                current[active_field] = existing_steps + [line]
            else:
                current[active_field] = (str(current.get(active_field, "")) + "\n" + line).strip()
    if current:
        raw_cases.append(current)
    return raw_cases


def parse_generated_cases(content: Any) -> list[dict[str, Any]]:
    """Parse current Markdown and supported legacy output into canonical cases."""

    if isinstance(content, (Mapping, list)):
        try:
            draft = parse_draft(content)
        except AIContractError:
            return []
        return _canonical_cases(draft["cases"])
    if not isinstance(content, str) or not content.strip():
        return []

    try:
        return _canonical_cases(parse_draft(content)["cases"])
    except AIContractError:
        pass

    text = re.sub(r"^\s*```(?:markdown|md|text)?\s*|\s*```\s*$", "", content.strip(), flags=re.IGNORECASE)
    lines = [line.rstrip() for line in text.splitlines()]
    for raw_cases in (_parse_case_table(lines), _parse_markdown_cases(lines), _parse_legacy_text(lines)):
        if raw_cases:
            return _canonical_cases(raw_cases)
    return []


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
