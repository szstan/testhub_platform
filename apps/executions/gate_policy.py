"""Pure normalization and evaluation helpers for execution quality gates."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any


RULE_BOUNDS = ("max", "min", "max_delta_pct", "min_delta_pct")
_COUNT_METRICS = {"failed", "unmatched"}
_MISSING = object()


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return 0.0 if number == 0 else number


def _validate_rule(metric: str, rule: Mapping[str, float]) -> None:
    if "min" in rule and "max" in rule and rule["min"] > rule["max"]:
        raise ValueError(f"{metric} has contradictory min/max bounds")
    if "min_delta_pct" in rule and "max_delta_pct" in rule and rule["min_delta_pct"] > rule["max_delta_pct"]:
        raise ValueError(f"{metric} has contradictory delta bounds")


def normalize_metric_rules(rules: Any, *, source: str = "rules") -> dict[str, dict[str, float]]:
    """Validate and sort metric rules into a stable, finite representation."""
    if rules is None:
        return {}
    if not isinstance(rules, Mapping):
        raise ValueError(f"{source} must be an object")

    normalized: dict[str, dict[str, float]] = {}
    for raw_metric, raw_rule in rules.items():
        if not isinstance(raw_metric, str) or not raw_metric.strip():
            raise ValueError(f"{source} contains an invalid metric name")
        metric = raw_metric.strip()
        if metric in normalized:
            raise ValueError(f"{source} contains a duplicate metric name")
        if not isinstance(raw_rule, Mapping) or not raw_rule:
            raise ValueError(f"{metric} must define at least one bound")
        unknown = set(raw_rule) - set(RULE_BOUNDS)
        if unknown:
            raise ValueError(f"{metric} contains an unsupported bound")
        if not set(raw_rule).intersection(RULE_BOUNDS):
            raise ValueError(f"{metric} must define at least one bound")
        rule = {
            bound: _finite_number(value, f"{metric}.{bound}")
            for bound, value in raw_rule.items()
        }
        _validate_rule(metric, rule)
        normalized[metric] = rule
    return {metric: normalized[metric] for metric in sorted(normalized)}


def _count_threshold(value: Any, name: str) -> int:
    number = _finite_number(value, name)
    if number < 0 or not number.is_integer():
        raise ValueError(f"{name} must be a non-negative integer")
    return int(number)


def merge_metric_rules(*rule_sets: Any) -> dict[str, dict[str, float]]:
    """Combine rules conjunctively, retaining the strictest bound per metric."""
    merged: dict[str, dict[str, float]] = {}
    for index, rules in enumerate(rule_sets):
        for metric, rule in normalize_metric_rules(rules, source=f"rules[{index}]").items():
            target = merged.setdefault(metric, {})
            for bound, value in rule.items():
                if bound in target:
                    if bound in {"max", "max_delta_pct"}:
                        target[bound] = min(target[bound], value)
                    else:
                        target[bound] = max(target[bound], value)
                else:
                    target[bound] = value
    for metric, rule in merged.items():
        _validate_rule(metric, rule)
    return {metric: merged[metric] for metric in sorted(merged)}


def build_effective_policy(
    profile_rules: Any,
    request_rules: Any,
    *,
    max_failed: Any,
    max_unmatched: Any,
) -> dict[str, Any]:
    """Resolve profile and request constraints without allowing relaxation."""
    failed_limit = _count_threshold(max_failed, "max_failed")
    unmatched_limit = _count_threshold(max_unmatched, "max_unmatched")
    rules = merge_metric_rules(
        profile_rules,
        request_rules,
        {"failed": {"max": failed_limit}, "unmatched": {"max": unmatched_limit}},
    )
    return {
        "rules": rules,
        "max_failed": failed_limit,
        "max_unmatched": unmatched_limit,
    }


def extract_metrics(summary: Any) -> dict[str, Any]:
    """Flatten supported summary layouts with top-level values taking priority."""
    if not isinstance(summary, Mapping):
        return {}
    flattened: dict[str, Any] = {}
    for key in ("summary", "metrics"):
        nested = summary.get(key)
        if isinstance(nested, Mapping):
            flattened.update(nested)
    flattened.update({key: value for key, value in summary.items() if key not in {"summary", "metrics"}})
    return flattened


def evaluate_metric_rules(
    rules: Mapping[str, Mapping[str, float]],
    summary: Any,
    *,
    baseline_summary: Any = None,
) -> list[dict[str, Any]]:
    """Evaluate normalized rules, failing closed on missing or non-finite data."""
    metrics = extract_metrics(summary)
    baseline_metrics = extract_metrics(baseline_summary)
    failures: list[dict[str, Any]] = []

    for metric, rule in rules.items():
        actual = metrics.get(metric, _MISSING)
        failure_metric = metric
        if actual is _MISSING or actual is None or actual == "":
            failures.append({"metric": failure_metric, "reason": "missing_metric"})
            continue
        try:
            actual_number = _finite_number(actual, metric)
        except ValueError:
            failures.append({"metric": failure_metric, "reason": "invalid_metric"})
            continue
        if metric in _COUNT_METRICS and (actual_number < 0 or not actual_number.is_integer()):
            failures.append({"metric": failure_metric, "reason": "invalid_count"})
            continue

        if "max" in rule and actual_number > rule["max"]:
            failures.append({"metric": failure_metric, "actual": actual_number, "max": rule["max"]})
        if "min" in rule and actual_number < rule["min"]:
            failures.append({"metric": failure_metric, "actual": actual_number, "min": rule["min"]})

        delta_bounds = {key: value for key, value in rule.items() if key in {"max_delta_pct", "min_delta_pct"}}
        if not delta_bounds:
            continue
        baseline = baseline_metrics.get(metric, _MISSING)
        if baseline is _MISSING or baseline is None or baseline == "":
            failures.append({"metric": failure_metric, "reason": "missing_baseline"})
            continue
        try:
            baseline_number = _finite_number(baseline, f"baseline.{metric}")
        except ValueError:
            failures.append({"metric": failure_metric, "reason": "invalid_baseline"})
            continue

        if baseline_number == 0:
            if actual_number == 0:
                delta_pct = 0.0
            else:
                failures.append({"metric": failure_metric, "reason": "undefined_zero_baseline_delta"})
                continue
        else:
            delta_pct = ((actual_number - baseline_number) / abs(baseline_number)) * 100.0
            if not math.isfinite(delta_pct):
                failures.append({"metric": failure_metric, "reason": "non_finite_delta"})
                continue

        if "max_delta_pct" in delta_bounds and delta_pct > delta_bounds["max_delta_pct"]:
            failures.append({
                "metric": failure_metric,
                "actual": actual_number,
                "baseline": baseline_number,
                "delta_pct": delta_pct,
                "max_delta_pct": delta_bounds["max_delta_pct"],
            })
        if "min_delta_pct" in delta_bounds and delta_pct < delta_bounds["min_delta_pct"]:
            failures.append({
                "metric": failure_metric,
                "actual": actual_number,
                "baseline": baseline_number,
                "delta_pct": delta_pct,
                "min_delta_pct": delta_bounds["min_delta_pct"],
            })

    return failures


def finite_json_value(value: Any) -> Any:
    """Replace non-finite floats before gate summaries are persisted as JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, Mapping):
        return {str(key): finite_json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json_value(child) for child in value]
    return value


def policy_version_for_snapshot(snapshot: Mapping[str, Any], explicit_version: str = "") -> str:
    """Return the supplied version or a deterministic digest of the snapshot."""
    version = str(explicit_version or "").strip()[:128]
    if version:
        return version
    encoded = json.dumps(
        snapshot,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "gate-" + hashlib.sha256(encoded).hexdigest()[:16]
