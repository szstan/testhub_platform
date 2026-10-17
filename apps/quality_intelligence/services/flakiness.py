"""Pure reliability and manual-quarantine helpers for quality intelligence.

The functions in this module deliberately accept both model-like objects and
plain mappings/sequences.  Callers can therefore calculate a reliability
window without loading a Django queryset or changing runner behaviour.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone as dt_timezone
from enum import Enum
from typing import Any


DEFAULT_WINDOW_SIZE = 20
MIN_ELIGIBLE_ATTEMPTS = 5

PASS_OUTCOME = "PASS"
FAIL_OUTCOMES = frozenset(("FAIL", "ERROR"))
EXCLUDED_OUTCOMES = frozenset(("SKIP", "NOT_RUN", "INCONCLUSIVE"))
VALID_OUTCOMES = frozenset((PASS_OUTCOME, *FAIL_OUTCOMES, *EXCLUDED_OUTCOMES))

INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
STABLE = "STABLE"
FLAKY = "FLAKY"

QUARANTINE_ACTIVE = "ACTIVE"
QUARANTINE_RELEASED = "RELEASED"
QUARANTINE_EXPIRED = "EXPIRED"


class ReliabilityStatus(str, Enum):
    INSUFFICIENT_DATA = INSUFFICIENT_DATA
    STABLE = STABLE
    FLAKY = FLAKY


class QuarantineState(str, Enum):
    ACTIVE = QUARANTINE_ACTIVE
    RELEASED = QUARANTINE_RELEASED
    EXPIRED = QUARANTINE_EXPIRED


class ServiceResult(dict):
    """Mapping result with attribute access, matching other QI services."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __bool__(self) -> bool:
        return bool(self.get("valid", True))

    def as_dict(self) -> dict[str, Any]:
        return dict(self)


_MISSING = object()


def _value(item: Any, *names: str, default: Any = None) -> Any:
    """Read the first present field from a mapping or model-like object."""
    for name in names:
        if isinstance(item, Mapping):
            if name in item and item[name] is not None:
                return item[name]
            continue
        try:
            value = getattr(item, name)
        except AttributeError:
            continue
        except Exception:  # pragma: no cover - defensive for lazy ORM fields
            continue
        if value is not None:
            return value
    return default


def _nested_value(item: Any, names: tuple[str, ...], *parents: str) -> Any:
    value = _value(item, *names, default=None)
    if value not in (None, ""):
        return value
    for parent_name in parents:
        parent = _value(item, parent_name, default=None)
        value = _value(parent, *names, default=None)
        if value not in (None, ""):
            return value
    return None


def _normalise_outcome(value: Any) -> str | None:
    if value is None:
        return None
    value = getattr(value, "value", value)
    try:
        value = str(value).strip().upper()
    except Exception:
        return None
    return value or None


def _normalise_identity(value: Any) -> Any:
    """Make common profile identifiers comparable without ORM queries."""
    if value is None or value == "":
        return None
    value = getattr(value, "value", value)
    if isinstance(value, Mapping):
        for name in ("profile_id", "id", "pk"):
            candidate = value.get(name)
            if candidate not in (None, ""):
                return candidate
        fields = ("source_type", "project_type", "project_id", "test_key", "environment_fingerprint")
        selected = tuple(value.get(field) for field in fields)
        return selected if any(part not in (None, "") for part in selected) else None
    for name in ("profile_id", "id", "pk"):
        try:
            candidate = getattr(value, name)
        except (AttributeError, TypeError):
            continue
        if candidate not in (None, ""):
            return candidate
    return value


def _profile_field(profile: Any, name: str) -> Any:
    if profile is None:
        return None
    return _value(profile, name, default=None)


def _candidate_metadata(item: Any) -> dict[str, Any]:
    environment = _nested_value(
        item, ("environment_fingerprint", "environment"), "item", "run"
    )
    if environment == "unknown":
        parent = _value(item, "run", default=None)
        parent_environment = _value(
            parent, "environment_fingerprint", "environment", default=None
        )
        if parent_environment not in (None, "", "unknown"):
            environment = parent_environment
    return {
        "test_key": _nested_value(item, ("test_key",), "item", "run"),
        "environment_fingerprint": environment,
        "source_type": _nested_value(item, ("source_type",), "run"),
        "project_type": _nested_value(item, ("project_type",), "run"),
        "project_id": _nested_value(item, ("project_id",), "run"),
    }


def _expected_metadata(
    profile: Any,
    *,
    test_key: Any = None,
    environment_fingerprint: Any = None,
    source_type: Any = None,
    project_type: Any = None,
    project_id: Any = None,
) -> dict[str, Any]:
    values = {
        "test_key": test_key,
        "environment_fingerprint": environment_fingerprint,
        "source_type": source_type,
        "project_type": project_type,
        "project_id": project_id,
    }
    for field, value in tuple(values.items()):
        if value in (None, ""):
            values[field] = _profile_field(profile, field)
    return values


def _is_comparable(item: Any, expected: Mapping[str, Any]) -> bool:
    candidate = _candidate_metadata(item)
    for field, expected_value in expected.items():
        if expected_value in (None, ""):
            continue
        candidate_value = candidate.get(field)
        # Simple outcome strings and old records can lack identity metadata;
        # when metadata is unavailable there is nothing to compare.
        if candidate_value not in (None, "") and candidate_value != expected_value:
            return False
    return True


def _timestamp_value(item: Any) -> float | None:
    value = _value(
        item,
        "finished_at",
        "created_at",
        "observed_at",
        "timestamp",
        "started_at",
        "id",
        default=None,
    )
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt_timezone.utc)
        return value.timestamp()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=dt_timezone.utc).timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _ordered(items: list[Any]) -> list[Any]:
    """Order records oldest-to-newest when they carry an ordering field.

    In-memory outcome strings have no timestamp and retain caller order.  This
    also matches the normal ascending ``QualityRunItem`` query order.
    """
    timestamps = [_timestamp_value(item) for item in items]
    if not any(timestamp is not None for timestamp in timestamps):
        return items
    return [
        item
        for _, item in sorted(
            zip(timestamps, items),
            key=lambda pair: (pair[0] is None, pair[0] if pair[0] is not None else 0),
        )
    ]


def _outcome(item: Any) -> str | None:
    if isinstance(item, (str, bytes)):
        value = item.decode() if isinstance(item, bytes) else item
    elif isinstance(item, Mapping):
        value = _value(item, "outcome", "result", default=None)
    else:
        value = _value(item, "outcome", "result", default=None)
    return _normalise_outcome(value)


def _outcome_class(outcome: str) -> str | None:
    if outcome == PASS_OUTCOME:
        return PASS_OUTCOME
    if outcome in FAIL_OUTCOMES:
        return "FAIL"
    return None


def eligible_outcome(outcome: Any) -> str | None:
    """Return PASS/FAIL for eligible outcomes, otherwise ``None``."""
    return _outcome_class(_normalise_outcome(outcome) or "")


def count_transitions(outcomes: Iterable[Any]) -> int:
    """Count adjacent PASS/FAIL changes after excluded outcomes are removed."""
    classes = [eligible_outcome(outcome) for outcome in outcomes]
    classes = [outcome for outcome in classes if outcome is not None]
    return sum(previous != current for previous, current in zip(classes, classes[1:]))


def calculate_flakiness(
    attempts: Iterable[Any] | None,
    window_size: int = DEFAULT_WINDOW_SIZE,
    min_attempts: int = MIN_ELIGIBLE_ATTEMPTS,
    *,
    window: int | None = None,
    profile: Any = None,
    test_key: Any = None,
    environment_fingerprint: Any = None,
    source_type: Any = None,
    project_type: Any = None,
    project_id: Any = None,
) -> dict[str, Any]:
    """Calculate reliability for the most recent comparable attempt window.

    ``PASS`` is pass; ``FAIL`` and ``ERROR`` are non-pass; ``SKIP``,
    ``NOT_RUN`` and ``INCONCLUSIVE`` are excluded from eligible counts.  A
    window is flaky only when it has enough eligible attempts, contains both
    pass and fail outcomes, and has at least two chronological transitions.
    """
    if window is not None:
        window_size = window
    try:
        window_size = int(window_size)
        min_attempts = int(min_attempts)
    except (TypeError, ValueError) as exc:
        raise ValueError("window_size and min_attempts must be integers") from exc
    if window_size < 1:
        raise ValueError("window_size must be greater than zero")
    if min_attempts < 1:
        raise ValueError("min_attempts must be greater than zero")

    if attempts is None:
        source_items: list[Any] = []
    elif isinstance(attempts, Mapping) and "attempts" in attempts:
        source_items = list(attempts.get("attempts") or [])
    else:
        source_items = list(attempts)

    expected = _expected_metadata(
        profile,
        test_key=test_key,
        environment_fingerprint=environment_fingerprint,
        source_type=source_type,
        project_type=project_type,
        project_id=project_id,
    )
    comparable = [item for item in source_items if _is_comparable(item, expected)]
    comparable = _ordered(comparable)
    window_items = comparable[-window_size:]

    all_outcomes = [_outcome(item) for item in window_items]
    eligible_outcomes = [outcome for outcome in all_outcomes if _outcome_class(outcome or "")]
    classes = [_outcome_class(outcome) for outcome in eligible_outcomes]
    classes = [outcome for outcome in classes if outcome is not None]
    pass_count = classes.count(PASS_OUTCOME)
    fail_count = classes.count("FAIL")
    transition_count = sum(
        previous != current for previous, current in zip(classes, classes[1:])
    )
    sample_count = len(classes)
    sufficient = sample_count >= min_attempts
    has_both = pass_count > 0 and fail_count > 0
    is_flaky = sufficient and has_both and transition_count >= 2
    status = FLAKY if is_flaky else (STABLE if sufficient else INSUFFICIENT_DATA)
    flaky_score = round(transition_count / (sample_count - 1), 6) if sample_count > 1 else 0.0

    if not sufficient:
        reason = "INSUFFICIENT_ATTEMPTS"
    elif is_flaky:
        reason = "FLAKY_PATTERN"
    elif not has_both:
        reason = "SINGLE_OUTCOME"
    elif transition_count < 2:
        reason = "INSUFFICIENT_TRANSITIONS"
    else:  # pragma: no cover - kept for future status rules
        reason = "STABLE_PATTERN"

    return ServiceResult({
        "status": status,
        "reliability_status": status,
        "is_flaky": is_flaky,
        "is_stable": status == STABLE,
        "window_size": window_size,
        "window_count": len(window_items),
        "window_attempts": len(window_items),
        "sample_count": sample_count,
        "sample_size": sample_count,
        "eligible_attempts": sample_count,
        "eligible_count": sample_count,
        "comparable_attempts": len(comparable),
        "total_attempts": len(source_items),
        "excluded_count": len(window_items) - sample_count,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "transition_count": transition_count,
        "transitions": transition_count,
        "flaky_score": flaky_score,
        "reason": reason,
        # Canonical labels make the result serializable and useful for APIs.
        "window": all_outcomes,
        "window_outcomes": all_outcomes,
        "eligible_outcomes": eligible_outcomes,
        "excluded_outcomes": [outcome for outcome in all_outcomes if not _outcome_class(outcome or "")],
        "excluded_statuses": sorted(
            {outcome for outcome in all_outcomes if outcome in EXCLUDED_OUTCOMES}
        ),
    })


# Names retained as small aliases so integrations can use the terminology in
# their plan without introducing a second implementation.
calculate_flakiness_window = calculate_flakiness
analyze_flakiness = calculate_flakiness
calculate_reliability = calculate_flakiness
evaluate_flakiness = calculate_flakiness
compute_flakiness = calculate_flakiness


def validate_quarantine_state(
    quarantine: Any,
    now: datetime | None = None,
    *,
    at: datetime | None = None,
    profile: Any = None,
) -> dict[str, Any]:
    """Validate a manually managed quarantine without changing its state.

    The persisted status is never rewritten when an expiry is observed.  A
    caller may use ``is_active`` to decide whether to display the quarantine;
    runner scheduling and automatic quarantine creation remain outside this
    helper.
    """
    if at is not None:
        now = at
    if now is None:
        now = datetime.now(dt_timezone.utc)
    status_value = _normalise_outcome(_value(quarantine, "status", default=None))
    starts_at = _value(quarantine, "starts_at", default=None)
    expires_at = _value(quarantine, "expires_at", default=None)
    released_at = _value(quarantine, "released_at", default=None)

    if quarantine is None:
        return ServiceResult({
            "valid": False,
            "is_valid": False,
            "is_active": False,
            "active": False,
            "status": None,
            "effective_status": None,
            "state": None,
            "reason": "MISSING",
        })

    if profile is not None:
        quarantine_profile = _value(quarantine, "profile_id", "profile", default=None)
        expected_profile = _normalise_identity(profile)
        actual_profile = _normalise_identity(quarantine_profile)
        if actual_profile is not None and expected_profile is not None and actual_profile != expected_profile:
            return ServiceResult({
                "valid": False,
                "is_valid": False,
                "is_active": False,
                "active": False,
                "status": status_value,
                "effective_status": status_value,
                "state": status_value,
                "reason": "PROFILE_MISMATCH",
            })

    if status_value not in (QUARANTINE_ACTIVE, QUARANTINE_RELEASED, QUARANTINE_EXPIRED):
        return ServiceResult({
            "valid": False,
            "is_valid": False,
            "is_active": False,
            "active": False,
            "status": status_value,
            "effective_status": status_value,
            "state": status_value,
            "reason": "INVALID_STATUS",
        })

    if status_value == QUARANTINE_RELEASED or released_at is not None:
        reason = "RELEASED"
        active = False
        valid = False
    elif status_value == QUARANTINE_EXPIRED:
        reason = "EXPIRED"
        active = False
        valid = False
    elif starts_at is None:
        reason = "MISSING_START"
        active = False
        valid = False
    elif _as_timestamp(starts_at) > _as_timestamp(now):
        reason = "NOT_STARTED"
        active = False
        valid = False
    elif expires_at is not None and _as_timestamp(expires_at) <= _as_timestamp(now):
        reason = "EXPIRED"
        active = False
        valid = False
    else:
        reason = "ACTIVE"
        active = True
        valid = True

    return ServiceResult({
        "valid": valid,
        "is_valid": valid,
        "is_active": active,
        "active": active,
        "status": status_value,
        "effective_status": status_value,
        "state": status_value,
        "reason": reason,
        "starts_at": starts_at,
        "expires_at": expires_at,
        "released_at": released_at,
    })


def _as_timestamp(value: Any) -> float:
    timestamp = _timestamp_value({"timestamp": value})
    return timestamp if timestamp is not None else float("inf")


validate_manual_quarantine = validate_quarantine_state
validate_quarantine = validate_quarantine_state
validate_manual_quarantine_state = validate_quarantine_state


def validate_quarantine_transition(
    current_status: Any,
    target_status: Any,
) -> ServiceResult:
    """Validate a manual quarantine state transition without persisting it.

    A quarantine may be released or explicitly marked expired while active.
    Released/expired records are terminal; reactivation requires a new manual
    quarantine record.  Repeating a state is a valid no-op.
    """
    current = _normalise_outcome(current_status)
    target = _normalise_outcome(target_status)
    known = {QUARANTINE_ACTIVE, QUARANTINE_RELEASED, QUARANTINE_EXPIRED}
    if current not in known or target not in known:
        return ServiceResult({
            "valid": False,
            "allowed": False,
            "current_status": current,
            "target_status": target,
            "reason": "INVALID_STATUS",
        })
    if current == target:
        reason = "NO_OP"
        allowed = True
    elif current == QUARANTINE_ACTIVE and target in (
        QUARANTINE_RELEASED,
        QUARANTINE_EXPIRED,
    ):
        reason = "ALLOWED"
        allowed = True
    else:
        reason = "TERMINAL_STATE"
        allowed = False
    return ServiceResult({
        "valid": allowed,
        "allowed": allowed,
        "current_status": current,
        "target_status": target,
        "reason": reason,
    })


validate_quarantine_transition_state = validate_quarantine_transition
check_quarantine_transition = validate_quarantine_transition


def is_quarantine_active(quarantine: Any, now: datetime | None = None) -> bool:
    """Return only the effective active flag for callers that need a boolean."""
    return bool(validate_quarantine_state(quarantine, now=now)["is_active"])


__all__ = [
    "DEFAULT_WINDOW_SIZE",
    "MIN_ELIGIBLE_ATTEMPTS",
    "EXCLUDED_OUTCOMES",
    "FAIL_OUTCOMES",
    "ReliabilityStatus",
    "QuarantineState",
    "ServiceResult",
    "validate_quarantine_transition",
    "validate_quarantine_transition_state",
    "check_quarantine_transition",
    "calculate_flakiness",
    "calculate_flakiness_window",
    "analyze_flakiness",
    "calculate_reliability",
    "evaluate_flakiness",
    "compute_flakiness",
    "eligible_outcome",
    "count_transitions",
    "validate_quarantine_state",
    "validate_manual_quarantine",
    "validate_quarantine",
    "validate_manual_quarantine_state",
    "is_quarantine_active",
]
