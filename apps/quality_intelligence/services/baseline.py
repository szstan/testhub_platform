"""Read-only API and performance baseline comparison helpers.

The comparison core works on numbers, mappings, and model-like objects.  It
never calls ``save`` and does not assign fields on the supplied baseline or
execution, which keeps comparison suitable for reporting and tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from numbers import Real
from typing import Any


WITHIN_TOLERANCE = "WITHIN_TOLERANCE"
REGRESSED = "REGRESSED"
NO_BASELINE = "NO_BASELINE"
NOT_COMPARABLE = "NOT_COMPARABLE"
ERROR = "ERROR"

LOWER_IS_BETTER = "LOWER_IS_BETTER"
HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
EQUAL = "EQUAL"

class ComparisonVerdict(str, Enum):
    WITHIN_TOLERANCE = WITHIN_TOLERANCE
    REGRESSED = REGRESSED
    NO_BASELINE = NO_BASELINE
    NOT_COMPARABLE = NOT_COMPARABLE
    ERROR = ERROR


class ComparisonResult(dict):
    """Mapping result with attribute access for service/API adapters."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def as_dict(self) -> dict[str, Any]:
        return dict(self)


_MISSING = object()


def _get(item: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(item, Mapping):
            if name in item and item[name] is not None:
                return item[name]
            continue
        try:
            value = getattr(item, name)
        except (AttributeError, TypeError):
            continue
        except Exception:  # pragma: no cover - lazy ORM field safeguards
            continue
        if value is not None:
            return value
    return default


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        try:
            if isinstance(value, str) and value.strip() != "":
                value = float(value.strip())
            else:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _source_type(value: Any) -> str | None:
    if not _present(value):
        return None
    value = getattr(value, "value", value)
    return str(value).strip().upper() or None


def _profile_identity(value: Any) -> Any:
    if not _present(value):
        return None
    value = getattr(value, "value", value)
    if isinstance(value, Mapping):
        for name in ("profile_id", "id", "pk"):
            candidate = value.get(name)
            if _present(candidate):
                return ("id", str(candidate))
        fields = ("source_type", "project_type", "project_id", "test_key", "environment_fingerprint")
        selected = tuple(value.get(field) for field in fields)
        if any(_present(part) for part in selected):
            return ("fields",) + tuple(str(part) if _present(part) else None for part in selected)
        return None
    for name in ("profile_id", "id", "pk"):
        try:
            candidate = getattr(value, name)
        except (AttributeError, TypeError):
            continue
        if _present(candidate):
            return ("id", str(candidate))
    return ("id", str(value))


def _profile_from_side(side: Any) -> Any:
    profile = _get(side, "profile_id", "profile_key", "profile", default=None)
    if _present(profile):
        return profile
    # PerfExecution/PerfBaseline identify a profile by scenario.
    scenario = _get(side, "scenario", "scenario_id", default=None)
    return scenario if _present(scenario) else None


def _profile_display(side: Any) -> Any:
    value = _profile_from_side(side)
    if isinstance(value, Mapping):
        return value.get("profile_id", value.get("id", value.get("pk", value)))
    for name in ("profile_id", "id", "pk"):
        try:
            candidate = getattr(value, name)
        except (AttributeError, TypeError):
            continue
        if _present(candidate):
            return candidate
    return value


def _environment_from_side(side: Any) -> Any:
    value = _get(
        side,
        "environment_fingerprint",
        "environment",
        "env_fingerprint",
        "environment_key",
        default=None,
    )
    if _present(value):
        return value
    for parent_name in ("run", "item", "execution", "scenario"):
        parent = _get(side, parent_name, default=None)
        value = _get(
            parent,
            "environment_fingerprint",
            "environment",
            "env_fingerprint",
            "environment_key",
            default=None,
        )
        if _present(value):
            return value
    return None


def _nested_side_value(side: Any, field_names: tuple[str, ...], parents: tuple[str, ...]) -> Any:
    value = _get(side, *field_names, default=None)
    if _present(value):
        return value
    for parent_name in parents:
        parent = _get(side, parent_name, default=None)
        value = _get(parent, *field_names, default=None)
        if _present(value):
            return value
    return None


def _side_identity(side: Any, *, default_source_type: str | None = None) -> dict[str, Any]:
    source = _nested_side_value(
        side,
        ("source_type", "source", "source_kind"),
        ("run", "execution"),
    )
    if not _present(source):
        source = default_source_type
    profile = _profile_from_side(side)
    return {
        "source_type": _source_type(source),
        "profile": _profile_identity(profile),
        "environment_fingerprint": _environment_from_side(side),
        "source_asset_id": _nested_side_value(
            side,
            ("source_asset_id", "asset_id", "source_asset"),
            ("run", "execution"),
        ),
        "project_type": _nested_side_value(side, ("project_type",), ("run", "execution")),
        "project_id": _nested_side_value(side, ("project_id",), ("run", "execution")),
        "unit": _get(side, "unit", default=None),
    }


def _looks_like_baseline(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        return any(
            key in value
            for key in ("baseline_value", "tolerance", "direction", "active", "metrics", "baseline_type")
        )
    return any(_present(_get(value, key, default=None)) for key in ("tolerance", "direction", "metrics", "active"))


def _metric_key(baseline: Any, actual: Any, requested: str | None) -> str | None:
    if _present(requested):
        return str(requested)
    value = _get(baseline, "metric_key", "metric", default=None)
    if _present(value):
        return str(value)
    value = _get(actual, "metric_key", "metric", default=None)
    if _present(value):
        return str(value)
    metrics = _get(baseline, "metrics", default=None)
    if isinstance(metrics, Mapping) and len(metrics) == 1:
        return str(next(iter(metrics)))
    if isinstance(baseline, Mapping) and "value" in baseline:
        return "value"
    return None


def _metric_value(side: Any, metric_key: str | None, *, baseline: bool) -> Any:
    if side is None:
        return None
    direct_names = ("baseline_value", "value") if baseline else ("actual_value", "value")
    value = _get(side, *direct_names, default=_MISSING)
    # A plain number or a mapping with an explicit value wins over nested data.
    if value is not _MISSING and value is not None:
        return value
    if metric_key:
        value = _get(side, metric_key, default=_MISSING)
        if value is not _MISSING:
            return value
    nested_names = ("metrics", "summary", "snapshot", "data")
    for nested_name in nested_names:
        nested = _get(side, nested_name, default=None)
        if isinstance(nested, Mapping):
            if metric_key and metric_key in nested:
                return nested[metric_key]
            if not metric_key and len(nested) == 1:
                return next(iter(nested.values()))
    if isinstance(side, (Real, str)) and not isinstance(side, bool):
        return side
    return None


def _tolerance_value(baseline: Any, metric_key: str | None, explicit: Any) -> Any:
    """Return a ratio; legacy *_pct fields are normalized to fractions."""
    if explicit is not None:
        return explicit
    ratio = _get(baseline, "tolerance_ratio", default=_MISSING)
    if ratio is not _MISSING:
        return ratio
    percent_value = _get(baseline, "tolerance_percent", default=_MISSING)
    if percent_value is not _MISSING:
        number = _number(percent_value)
        return number / 100.0 if number is not None else percent_value
    value = _get(baseline, "tolerance", default=0.0)
    if not isinstance(value, Mapping):
        mode = str(_get(baseline, "tolerance_mode", default="RATIO")).upper()
        if mode in ("ABSOLUTE", "VALUE", "UNIT"):
            return value
        number = _number(value)
        return number / 100.0 if number is not None and abs(number) > 1 else value
    keys = []
    if metric_key:
        keys.extend((metric_key, f"{metric_key}_ratio", f"{metric_key}_tolerance"))
        lower = metric_key.lower()
        if "tps" in lower or "throughput" in lower:
            keys.extend(("tps_degrade_pct", "throughput_degrade_pct"))
        if "rt" in lower or "latency" in lower or "response" in lower:
            keys.extend(("rt_degrade_pct", "response_time_degrade_pct"))
        if "error" in lower:
            keys.append("error_rate_degrade_pct")
    for key in keys:
        if key in value:
            number = _number(value[key])
            if number is None:
                return value[key]
            return number / 100.0 if "pct" in key.lower() or abs(number) > 1 else number
    return 0.0


def _direction_value(baseline: Any, metric_key: str | None, explicit: Any) -> str:
    direction = explicit if explicit is not None else _get(baseline, "direction", default=None)
    if isinstance(direction, Mapping):
        direction = direction.get(metric_key) or direction.get("default")
    if not _present(direction):
        if metric_key and ("tps" in metric_key.lower() or "throughput" in metric_key.lower()):
            return HIGHER_IS_BETTER
        return LOWER_IS_BETTER
    normalized = str(getattr(direction, "value", direction)).strip().upper()
    aliases = {
        "LOWER": LOWER_IS_BETTER,
        "MIN": LOWER_IS_BETTER,
        "DECREASE": LOWER_IS_BETTER,
        "LOWER_IS_BETTER": LOWER_IS_BETTER,
        "LOWER_BETTER": LOWER_IS_BETTER,
        "HIGHER": HIGHER_IS_BETTER,
        "MAX": HIGHER_IS_BETTER,
        "INCREASE": HIGHER_IS_BETTER,
        "HIGHER_IS_BETTER": HIGHER_IS_BETTER,
        "HIGHER_BETTER": HIGHER_IS_BETTER,
        "EQUAL": EQUAL,
        "EXACT": EQUAL,
        "NONE": EQUAL,
    }
    return aliases.get(normalized, normalized)


def _tolerance_mode(baseline: Any, explicit: Any) -> str:
    mode = explicit if explicit is not None else _get(
        baseline, "tolerance_mode", "tolerance_unit", default="RATIO"
    )
    if _get(baseline, "tolerance_is_ratio", default=False):
        return "RATIO"
    return str(mode).strip().upper() if _present(mode) else "PERCENT"


def _result(
    verdict: str,
    *,
    baseline_value: float | None = None,
    actual_value: float | None = None,
    delta: float | None = None,
    delta_pct: float | None = None,
    tolerance: float | None = None,
    allowed_delta: float | None = None,
    direction: str | None = None,
    metric_key: str | None = None,
    reason: str | None = None,
    mismatch_fields: list[str] | None = None,
    source_type: str | None = None,
    profile_id: Any = None,
    environment_fingerprint: Any = None,
    unit: Any = None,
    source_asset_id: Any = None,
) -> dict[str, Any]:
    return ComparisonResult({
        "verdict": verdict,
        "status": verdict,
        "comparable": verdict in (WITHIN_TOLERANCE, REGRESSED),
        "within_tolerance": verdict == WITHIN_TOLERANCE,
        "baseline_value": baseline_value,
        "actual_value": actual_value,
        "delta": delta,
        "delta_pct": delta_pct,
        "tolerance": tolerance,
        "tolerance_ratio": tolerance,
        "allowed_delta": allowed_delta,
        "direction": direction,
        "metric_key": metric_key,
        "reason": reason,
        "mismatch_fields": mismatch_fields or [],
        "source_type": source_type,
        "profile_id": profile_id,
        "environment_fingerprint": environment_fingerprint,
        "unit": unit,
        "source_asset_id": source_asset_id,
        "details": {
            "reason": reason,
            "delta_pct": delta_pct,
            "allowed_delta": allowed_delta,
            "tolerance_ratio": tolerance,
            "mismatch_fields": mismatch_fields or [],
        },
    })


_METRIC_CONTEXT_KEYS = frozenset(
    {
        "source_type", "source", "source_kind", "source_asset_id", "asset_id",
        "source_asset", "profile", "profile_id", "profile_key", "environment",
        "environment_fingerprint", "env_fingerprint", "environment_key",
        "project_type", "project_id", "unit", "tolerance", "tolerance_ratio",
        "tolerance_percent", "tolerance_mode", "tolerance_unit",
        "tolerance_is_ratio", "direction", "metric_key",
        "metric", "active", "baseline_type", "source_baseline_key",
    }
)


def _metric_values(side: Any, *, baseline: bool) -> dict[str, Any]:
    """Extract a metric mapping without requiring a model/queryset."""
    for name in ("metrics", "summary", "values"):
        nested = _get(side, name, default=None)
        if isinstance(nested, Mapping):
            return dict(nested)
    if not isinstance(side, Mapping):
        return {}
    direct_names = {"value", "baseline_value"} if baseline else {"value", "actual_value"}
    return {
        str(key): value
        for key, value in side.items()
        if key not in _METRIC_CONTEXT_KEYS and key not in direct_names
    }


def _is_metric_set(actual: Any, baseline: Any, metric_key: str | None) -> bool:
    if metric_key is not None:
        return False
    if isinstance(_get(actual, "metrics", default=None), Mapping):
        return True
    if isinstance(_get(baseline, "metrics", default=None), Mapping):
        return True
    actual_values = _metric_values(actual, baseline=False)
    baseline_values = _metric_values(baseline, baseline=True)
    if not actual_values and not baseline_values:
        return False
    actual_has_scalar = _present(_get(actual, "value", "actual_value", default=None))
    baseline_has_scalar = _present(_get(baseline, "value", "baseline_value", default=None))
    return not actual_has_scalar and not baseline_has_scalar


def _metric_item(side: Any, value: Any, *, key: str) -> dict[str, Any]:
    item: dict[str, Any] = {"value": value, "metric_key": key}
    if isinstance(side, Mapping):
        for field in _METRIC_CONTEXT_KEYS:
            if field in side:
                item[field] = side[field]
    return item


def _compare_metric_set(
    actual: Any,
    baseline: Any,
    source_context: Any,
    **kwargs: Any,
) -> ComparisonResult:
    context_source = kwargs.get("source_type") or _get(
        source_context, "source_type", "source", default=None
    )
    context_profile = kwargs.get("profile") or kwargs.get("profile_id") or _get(
        source_context, "profile", "profile_id", default=None
    )
    context_environment = kwargs.get("environment_fingerprint") or _get(
        source_context, "environment_fingerprint", "environment", default=None
    )
    if not all(_present(value) for value in (context_source, context_profile, context_environment)):
        return _result(ERROR, reason="MISSING_SOURCE_CONTEXT")

    actual_values = _metric_values(actual, baseline=False)
    baseline_values = _metric_values(baseline, baseline=True)
    keys = list(dict.fromkeys((*baseline_values.keys(), *actual_values.keys())))
    if not keys:
        return _result(NO_BASELINE, reason="NO_METRICS")

    actual_identity = _side_identity(actual)
    baseline_identity = _side_identity(baseline)
    comparisons: dict[str, ComparisonResult] = {}
    for key in keys:
        if key not in baseline_values:
            comparisons[key] = _result(
                NO_BASELINE, metric_key=key, reason="NO_METRIC_BASELINE"
            )
            continue
        comparisons[key] = compare_baseline(
            _metric_item(actual, actual_values.get(key), key=key),
            _metric_item(baseline, baseline_values[key], key=key),
            source_context=source_context,
            metric_key=key,
            actual_source_type=actual_identity["source_type"],
            baseline_source_type=baseline_identity["source_type"],
            actual_profile=_profile_from_side(actual),
            baseline_profile=_profile_from_side(baseline),
            actual_environment_fingerprint=actual_identity["environment_fingerprint"],
            baseline_environment_fingerprint=baseline_identity["environment_fingerprint"],
            actual_source_asset_id=actual_identity["source_asset_id"],
            baseline_source_asset_id=baseline_identity["source_asset_id"],
            **kwargs,
        )

    statuses = [result["verdict"] for result in comparisons.values()]
    if ERROR in statuses:
        verdict = ERROR
    elif NOT_COMPARABLE in statuses:
        verdict = NOT_COMPARABLE
    elif REGRESSED in statuses:
        verdict = REGRESSED
    elif NO_BASELINE in statuses:
        verdict = NO_BASELINE
    else:
        verdict = WITHIN_TOLERANCE
    result = _result(
        verdict,
        reason="METRIC_SET",
        source_type=_source_type(
            kwargs.get("source_type")
            or _get(source_context, "source_type", "source", default=None)
            or actual_identity["source_type"]
            or baseline_identity["source_type"]
        ),
        profile_id=_profile_display(actual) or _get(
            source_context, "profile_id", "profile", default=None
        ),
        environment_fingerprint=(
            kwargs.get("environment_fingerprint")
            or _get(source_context, "environment_fingerprint", "environment", default=None)
            or actual_identity["environment_fingerprint"]
            or baseline_identity["environment_fingerprint"]
        ),
        source_asset_id=(
            kwargs.get("source_asset_id")
            or _get(source_context, "source_asset_id", "asset_id", default=None)
            or actual_identity["source_asset_id"]
            or baseline_identity["source_asset_id"]
        ),
    )
    result["comparisons"] = {key: dict(value) for key, value in comparisons.items()}
    result["metric_results"] = result["comparisons"]
    result["details"] = result["comparisons"]
    result["metric_count"] = len(comparisons)
    return result


def compare_baseline(
    first: Any = None,
    second: Any = None,
    source_context: Any = None,
    *,
    actual: Any = None,
    baseline: Any = None,
    actual_value: Any = None,
    baseline_value: Any = None,
    metric_key: str | None = None,
    source_type: Any = None,
    actual_source_type: Any = None,
    baseline_source_type: Any = None,
    profile: Any = None,
    profile_id: Any = None,
    actual_profile: Any = None,
    baseline_profile: Any = None,
    actual_profile_id: Any = None,
    baseline_profile_id: Any = None,
    environment_fingerprint: Any = None,
    actual_environment_fingerprint: Any = None,
    baseline_environment_fingerprint: Any = None,
    environment: Any = None,
    actual_environment: Any = None,
    baseline_environment: Any = None,
    source: Any = None,
    source_asset_id: Any = None,
    actual_source_asset_id: Any = None,
    baseline_source_asset_id: Any = None,
    asset_id: Any = None,
    actual_asset_id: Any = None,
    baseline_asset_id: Any = None,
    profile_key: Any = None,
    actual_profile_key: Any = None,
    baseline_profile_key: Any = None,
    tolerance_ratio: Any = None,
    tolerance: Any = None,
    tolerance_percent: Any = None,
    direction: Any = None,
    comparison_direction: Any = None,
    tolerance_mode: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    """Compare one measured metric to a baseline without mutating either input.

    The canonical baseline field/argument is ``tolerance_ratio`` (for example
    ``0.20`` for 20 percent); ``delta`` is the original-value difference and
    ``delta_pct`` is relative.  Legacy ``tolerance``/``*_pct`` inputs remain
    accepted at adapters for compatibility.
    """
    if source_context is not None:
        context_source = _get(source_context, "source_type", "source", "source_kind", default=None)
        context_asset = _get(
            source_context,
            "source_asset_id",
            "asset_id",
            "source_asset",
            default=None,
        )
        context_profile = _get(
            source_context,
            "profile",
            "profile_id",
            "profile_key",
            default=None,
        )
        context_environment = _get(
            source_context,
            "environment_fingerprint",
            "environment",
            "env_fingerprint",
            default=None,
        )
        if source_type is None:
            source_type = context_source
        if source_asset_id is None:
            source_asset_id = context_asset
        if profile is None and profile_id is None:
            profile = context_profile
        if environment_fingerprint is None:
            environment_fingerprint = context_environment
    if source_type is None:
        source_type = source
    if profile_id is None:
        profile_id = profile_key
    if source_asset_id is None:
        source_asset_id = asset_id
    if actual_source_asset_id is None:
        actual_source_asset_id = actual_asset_id
    if baseline_source_asset_id is None:
        baseline_source_asset_id = baseline_asset_id
    if environment_fingerprint is None:
        environment_fingerprint = environment
    if actual_profile_id is None:
        actual_profile_id = actual_profile_key
    if baseline_profile_id is None:
        baseline_profile_id = baseline_profile_key
    if actual_environment_fingerprint is None:
        actual_environment_fingerprint = actual_environment
    if baseline_environment_fingerprint is None:
        baseline_environment_fingerprint = baseline_environment
    if tolerance is not None and tolerance_ratio is None:
        tolerance_ratio = tolerance
    if tolerance_percent is not None and tolerance_ratio is None:
        percent = _number(tolerance_percent)
        tolerance_ratio = percent / 100.0 if percent is not None else tolerance_percent
    if direction is None:
        direction = comparison_direction
    if actual is None and baseline is None:
        if _looks_like_baseline(first) and not _looks_like_baseline(second):
            baseline, actual = first, second
        else:
            actual, baseline = first, second
    else:
        if actual is None:
            actual = first if first is not None else actual_value
        if baseline is None:
            baseline = second if second is not None else baseline_value

    # Scalar baseline_value/actual_value calls remain pure and convenient.
    if baseline is None and baseline_value is not None:
        baseline = {
            "value": baseline_value,
            "tolerance_ratio": tolerance_ratio if tolerance_ratio is not None else 0.0,
            "direction": direction if direction is not None else LOWER_IS_BETTER,
        }
    if actual_value is not None:
        actual = {"value": actual_value} if actual is None or isinstance(actual, (Real, str)) else actual

    if baseline is None or baseline is False or baseline == {}:
        return _result(
            NO_BASELINE,
            reason="NO_ACTIVE_BASELINE",
            metric_key=metric_key,
            source_type=_source_type(source_type),
            environment_fingerprint=environment_fingerprint,
        )
    if _get(baseline, "active", default=True) is False or _get(baseline, "active", default=True) == 0:
        return _result(
            NO_BASELINE,
            reason="INACTIVE_BASELINE",
            metric_key=metric_key,
            source_type=_source_type(source_type),
            environment_fingerprint=environment_fingerprint,
        )

    if _is_metric_set(actual, baseline, metric_key):
        return _compare_metric_set(
            actual,
            baseline,
            source_context,
            source_type=source_type,
            source_asset_id=source_asset_id,
            profile=profile,
            profile_id=profile_id,
            environment_fingerprint=environment_fingerprint,
            tolerance_ratio=tolerance_ratio,
            tolerance_percent=tolerance_percent,
            direction=direction,
            comparison_direction=comparison_direction,
            tolerance_mode=tolerance_mode,
        )


    metric_key = _metric_key(baseline, actual, metric_key)
    base_identity = _side_identity(baseline)
    actual_identity = _side_identity(actual)

    # Shared context fills omitted side metadata, while side-specific values
    # retain precedence so mismatches remain observable.
    shared_source = _source_type(source_type)
    base_identity["source_type"] = _source_type(
        baseline_source_type if baseline_source_type is not None else base_identity["source_type"]
    ) or shared_source
    actual_identity["source_type"] = _source_type(
        actual_source_type if actual_source_type is not None else actual_identity["source_type"]
    ) or shared_source

    base_identity["source_asset_id"] = (
        baseline_source_asset_id
        if baseline_source_asset_id is not None
        else base_identity["source_asset_id"]
    )
    actual_identity["source_asset_id"] = (
        actual_source_asset_id
        if actual_source_asset_id is not None
        else actual_identity["source_asset_id"]
    )
    if source_asset_id is not None:
        base_identity["source_asset_id"] = base_identity["source_asset_id"] or source_asset_id
        actual_identity["source_asset_id"] = actual_identity["source_asset_id"] or source_asset_id

    shared_profile = profile if profile is not None else profile_id
    base_profile = baseline_profile if baseline_profile is not None else baseline_profile_id
    actual_profile_value = actual_profile if actual_profile is not None else actual_profile_id
    base_identity["profile"] = _profile_identity(base_profile) or base_identity["profile"] or _profile_identity(shared_profile)
    actual_identity["profile"] = _profile_identity(actual_profile_value) or actual_identity["profile"] or _profile_identity(shared_profile)

    shared_environment = environment_fingerprint
    base_identity["environment_fingerprint"] = (
        baseline_environment_fingerprint
        if baseline_environment_fingerprint is not None
        else base_identity["environment_fingerprint"]
    )
    actual_identity["environment_fingerprint"] = (
        actual_environment_fingerprint
        if actual_environment_fingerprint is not None
        else actual_identity["environment_fingerprint"]
    )
    if shared_environment is not None:
        base_identity["environment_fingerprint"] = base_identity["environment_fingerprint"] or shared_environment
        actual_identity["environment_fingerprint"] = actual_identity["environment_fingerprint"] or shared_environment

    mismatch_fields: list[str] = []
    for field in ("source_type", "source_asset_id", "profile", "environment_fingerprint"):
        left = base_identity[field]
        right = actual_identity[field]
        if _present(left) or _present(right):
            if not _present(left) or not _present(right) or left != right:
                mismatch_fields.append(field)
    # Units are part of metric comparability whenever either side declares one.
    left_unit = base_identity.get("unit")
    right_unit = actual_identity.get("unit")
    comparison_unit = right_unit if _present(right_unit) else left_unit
    if _present(left_unit) or _present(right_unit):
        if not _present(left_unit) or not _present(right_unit) or left_unit != right_unit:
            mismatch_fields.append("unit")

    if mismatch_fields:
        return _result(
            NOT_COMPARABLE,
            metric_key=metric_key,
            reason="IDENTITY_MISMATCH",
            mismatch_fields=mismatch_fields,
            source_type=actual_identity["source_type"],
            profile_id=_profile_display(actual),
            environment_fingerprint=actual_identity["environment_fingerprint"],
            source_asset_id=actual_identity["source_asset_id"],
            unit=comparison_unit,
        )

    raw_baseline = baseline_value if baseline_value is not None else _metric_value(
        baseline, metric_key, baseline=True
    )
    raw_actual = actual_value if actual_value is not None else _metric_value(actual, metric_key, baseline=False)
    numeric_baseline = _number(raw_baseline)
    numeric_actual = _number(raw_actual)
    if numeric_baseline is None:
        return _result(
            ERROR,
            metric_key=metric_key,
            reason="INVALID_BASELINE_VALUE",
            direction=_direction_value(baseline, metric_key, direction),
        )
    if numeric_actual is None:
        return _result(
            ERROR,
            baseline_value=numeric_baseline,
            metric_key=metric_key,
            reason="INVALID_ACTUAL_VALUE",
            direction=_direction_value(baseline, metric_key, direction),
        )

    raw_tolerance = _tolerance_value(baseline, metric_key, tolerance_ratio)
    numeric_tolerance = _number(raw_tolerance)
    if numeric_tolerance is None or numeric_tolerance < 0:
        return _result(
            ERROR,
            baseline_value=numeric_baseline,
            actual_value=numeric_actual,
            metric_key=metric_key,
            reason="INVALID_TOLERANCE",
            direction=_direction_value(baseline, metric_key, direction),
        )
    comparison_direction = _direction_value(baseline, metric_key, direction)
    mode = _tolerance_mode(baseline, tolerance_mode)
    if comparison_direction not in (LOWER_IS_BETTER, HIGHER_IS_BETTER, EQUAL):
        return _result(
            ERROR,
            baseline_value=numeric_baseline,
            actual_value=numeric_actual,
            tolerance=numeric_tolerance,
            metric_key=metric_key,
            reason="INVALID_DIRECTION",
            direction=comparison_direction,
        )
    if mode in ("PERCENT", "PCT", "%"):
        allowed_delta = abs(numeric_baseline) * numeric_tolerance / 100.0
        if numeric_baseline == 0:
            allowed_delta = numeric_tolerance
    elif mode in ("RATIO", "RELATIVE"):
        allowed_delta = abs(numeric_baseline) * numeric_tolerance
    elif mode in ("ABSOLUTE", "VALUE", "UNIT"):
        allowed_delta = numeric_tolerance
    else:
        return _result(
            ERROR,
            baseline_value=numeric_baseline,
            actual_value=numeric_actual,
            tolerance=numeric_tolerance,
            metric_key=metric_key,
            reason="INVALID_TOLERANCE_MODE",
            direction=comparison_direction,
        )

    delta = numeric_actual - numeric_baseline
    delta_pct = round(delta / numeric_baseline * 100.0, 6) if numeric_baseline else None
    if comparison_direction == LOWER_IS_BETTER:
        within = delta <= allowed_delta
    elif comparison_direction == HIGHER_IS_BETTER:
        within = delta >= -allowed_delta
    else:
        within = abs(delta) <= allowed_delta
    verdict = WITHIN_TOLERANCE if within else REGRESSED
    return _result(
        verdict,
        baseline_value=numeric_baseline,
        actual_value=numeric_actual,
        delta=round(delta, 6),
        delta_pct=delta_pct,
        tolerance=numeric_tolerance,
        allowed_delta=round(allowed_delta, 6),
        direction=comparison_direction,
        metric_key=metric_key,
        reason="WITHIN_TOLERANCE" if within else "OUTSIDE_TOLERANCE",
        source_type=actual_identity["source_type"],
        profile_id=_profile_display(actual),
        environment_fingerprint=actual_identity["environment_fingerprint"],
        source_asset_id=actual_identity["source_asset_id"],
        unit=comparison_unit,
    )


def compare_metric(
    actual_value: Any,
    baseline_value: Any,
    tolerance_ratio: Any = None,
    direction: Any = LOWER_IS_BETTER,
    **kwargs: Any,
) -> ComparisonResult:
    """Compare scalar values using a fractional tolerance ratio.

    ``tolerance_ratio=0.20`` means a 20 percent allowance.  ``delta`` in the
    result is the original-value difference and ``delta_pct`` is relative.
    The legacy ``tolerance=20`` keyword remains accepted as 20 percent.
    """
    if baseline_value is None:
        return _result(NO_BASELINE, reason="NO_ACTIVE_BASELINE")
    if tolerance_ratio is None:
        legacy_tolerance = kwargs.pop("tolerance", None)
        if legacy_tolerance is not None:
            number = _number(legacy_tolerance)
            tolerance_ratio = number / 100.0 if number is not None and abs(number) > 1 else legacy_tolerance
        else:
            tolerance_ratio = 0.0
    elif _number(tolerance_ratio) is not None and abs(float(tolerance_ratio)) > 1:
        # Preserve callers of the pre-ratio positional helper.
        tolerance_ratio = float(tolerance_ratio) / 100.0
    baseline_payload = {
        "value": baseline_value,
        "tolerance_ratio": tolerance_ratio,
        "direction": direction,
    }
    return compare_baseline(actual_value, baseline_payload, **kwargs)


compare_metric_value = compare_metric


def compare_to_baseline(actual: Any, baseline: Any, **kwargs: Any) -> dict[str, Any]:
    return compare_baseline(actual, baseline, **kwargs)


def compare_baseline_to_actual(baseline: Any, actual: Any, **kwargs: Any) -> dict[str, Any]:
    return compare_baseline(baseline, actual, **kwargs)


def compare_performance_baseline(
    execution: Any,
    baseline: Any,
    *,
    metric_key: str | None = None,
    environment_fingerprint: Any = None,
    profile: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compare a ``PerfExecution`` summary to a read-only ``PerfBaseline``.

    PerfBaseline stores ``*_degrade_pct`` values (for example ``20``), while
    the pure comparison core uses fractional ``tolerance_ratio`` values.  The
    adapter normalizes those percentages without changing either model.
    """
    if _looks_like_baseline(execution) and not _looks_like_baseline(baseline):
        execution, baseline = baseline, execution
    if baseline is None:
        return compare_baseline(None, None, metric_key=metric_key)
    scenario = _get(execution, "scenario_id", "scenario", default=None)
    baseline_scenario = _get(baseline, "scenario_id", "scenario", default=None)
    profile_value = profile if profile is not None else scenario
    env_value = environment_fingerprint
    if env_value is None:
        env_value = _environment_from_side(execution)
    if profile_value is None or env_value is None:
        return _result(ERROR, reason="MISSING_SOURCE_CONTEXT", source_type="PERF")
    # PerfBaseline stores metric/tolerance snapshots, while PerfExecution stores
    # current values under summary.  Materialize only a transient mapping.
    baseline_payload = {
        "value": _metric_value(baseline, metric_key, baseline=True),
        "metrics": _get(baseline, "metrics", default=None),
        "tolerance": _get(baseline, "tolerance", default=0.0),
        "metric_key": metric_key,
        "direction": _get(baseline, "direction", default=None),
        "active": _get(baseline, "active", default=True),
        "source_type": "PERF",
        "profile": baseline_scenario,
        "environment_fingerprint": _environment_from_side(baseline) or env_value,
    }
    actual_payload = {
        "summary": _get(execution, "summary", default=None),
        "metric_key": metric_key,
        "source_type": "PERF",
        "profile": profile_value,
        "environment_fingerprint": env_value,
    }
    # A missing scenario is left to the generic strict identity check only if
    # a caller supplied a profile; otherwise both sides may still be scalar.
    if baseline_scenario is None and profile_value is not None:
        baseline_payload["profile"] = profile_value
    return compare_baseline(
        actual_payload,
        baseline_payload,
        metric_key=metric_key,
        source_type="PERF",
        profile=profile_value,
        environment_fingerprint=env_value,
        **kwargs,
    )


compare_perf_baseline = compare_performance_baseline
compare_execution_to_baseline = compare_performance_baseline


def compare_api_baseline(actual: Any, baseline: Any, **kwargs: Any) -> dict[str, Any]:
    """Compare an API metric using the API source identity by default."""
    kwargs.setdefault("source_type", "API")
    return compare_baseline(actual, baseline, **kwargs)


compare_api_to_baseline = compare_api_baseline
compare_metric_to_baseline = compare_baseline
compare_quality_baseline = compare_baseline
compare_baseline_metric = compare_baseline


__all__ = [
    "WITHIN_TOLERANCE",
    "REGRESSED",
    "NO_BASELINE",
    "NOT_COMPARABLE",
    "ERROR",
    "LOWER_IS_BETTER",
    "HIGHER_IS_BETTER",
    "EQUAL",
    "ComparisonVerdict",
    "ComparisonResult",
    "compare_baseline",
    "compare_metric",
    "compare_metric_value",
    "compare_to_baseline",
    "compare_baseline_to_actual",
    "compare_performance_baseline",
    "compare_perf_baseline",
    "compare_execution_to_baseline",
    "compare_api_baseline",
    "compare_api_to_baseline",
    "compare_metric_to_baseline",
    "compare_quality_baseline",
    "compare_baseline_metric",
]
