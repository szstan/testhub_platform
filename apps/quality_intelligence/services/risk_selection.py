"""Explainable risk-based regression selection helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .flakiness import calculate_flakiness


PRIORITY_WEIGHT = {"P0": 1.0, "P1": 0.8, "P2": 0.5, "P3": 0.25}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _priority(value: Any) -> float:
    return PRIORITY_WEIGHT.get(str(value or "P2").upper(), PRIORITY_WEIGHT["P2"])


def score_test_risk(
    candidate: Mapping[str, Any],
    history: Iterable[Any] | None = None,
    *,
    change_impact: float | None = None,
) -> dict[str, Any]:
    """Score one test using failure rate, flakiness, priority and change impact."""

    attempts = list(candidate.get("history") or []) if history is None else list(history)
    reliability = calculate_flakiness(attempts, min_attempts=1)
    sample_count = int(reliability["sample_count"] or 0)
    failure_rate = (reliability["fail_count"] / sample_count) if sample_count else 0.0
    flaky_score = _number(reliability.get("flaky_score"))
    priority_score = _priority(candidate.get("priority"))
    impact_value = candidate.get("change_impact") if change_impact is None else change_impact
    impact_score = _number(impact_value)
    score = round(
        100 * (
            0.40 * failure_rate
            + 0.25 * flaky_score
            + 0.20 * priority_score
            + 0.15 * impact_score
        ),
        2,
    )
    reasons: list[str] = []
    if failure_rate:
        reasons.append("RECENT_FAILURES")
    if reliability.get("is_flaky"):
        reasons.append("FLAKY_PATTERN")
    if priority_score >= 1.0:
        reasons.append("P0_PRIORITY")
    if impact_score:
        reasons.append("CHANGED_AREA")
    if not reasons:
        reasons.append("LOW_SIGNAL")
    level = "CRITICAL" if score >= 75 else "HIGH" if score >= 50 else "MEDIUM" if score >= 25 else "LOW"
    return {
        "test_key": str(candidate.get("test_key") or candidate.get("id") or ""),
        "risk_score": score,
        "risk_level": level,
        "reasons": reasons,
        "failure_rate": round(failure_rate, 6),
        "flaky_score": flaky_score,
        "priority_score": priority_score,
        "change_impact": impact_score,
        "reliability": reliability,
    }


def select_regression_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    histories: Mapping[str, Iterable[Any]] | None = None,
    change_impacts: Mapping[str, float] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return an explainable, deterministic high-risk regression shortlist."""

    histories = histories or {}
    change_impacts = change_impacts or {}
    scored = []
    for candidate in candidates:
        key = str(candidate.get("test_key") or candidate.get("id") or "")
        if candidate.get("quarantined"):
            continue
        scored.append(
            score_test_risk(
                candidate,
                histories.get(key),
                change_impact=(change_impacts[key] if key in change_impacts else None),
            )
        )
    scored.sort(key=lambda item: (-item["risk_score"], item["test_key"]))
    return scored[: max(0, int(limit))]
