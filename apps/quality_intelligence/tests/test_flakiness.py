from datetime import datetime, timedelta, timezone

from apps.quality_intelligence.services.flakiness import (
    calculate_flakiness,
    is_quarantine_active,
    validate_quarantine_state,
    validate_quarantine_transition,
)


def test_insufficient_data_excludes_non_eligible_outcomes_and_errors_are_failures():
    result = calculate_flakiness(
        ["PASS", "ERROR", "SKIP", "NOT_RUN", "INCONCLUSIVE", "FAIL"]
    )

    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["sample_count"] == 3
    assert result["pass_count"] == 1
    assert result["fail_count"] == 2
    assert result["excluded_count"] == 3


def test_recent_window_and_minimum_eligible_attempts_are_exposed():
    result = calculate_flakiness(
        ["PASS"] * 20 + ["SKIP", "PASS", "FAIL", "PASS", "FAIL"],
        window_size=5,
    )

    assert result["window_size"] == 5
    assert result["window_count"] == 5
    assert result["sample_count"] == 4
    assert result["eligible_attempts"] == 4
    assert result["status"] == "INSUFFICIENT_DATA"


def test_flaky_requires_both_outcomes_and_two_transitions():
    result = calculate_flakiness(["PASS", "FAIL", "PASS", "FAIL", "PASS"])

    assert result["status"] == "FLAKY"
    assert result["reliability_status"] == "FLAKY"
    assert result["pass_count"] == 3
    assert result["fail_count"] == 2
    assert result["transition_count"] == 4


def test_stable_when_sufficient_but_single_outcome_or_too_few_transitions():
    all_pass = calculate_flakiness(["PASS"] * 5)
    one_transition = calculate_flakiness(["PASS", "PASS", "PASS", "FAIL", "FAIL"])

    assert all_pass["status"] == "STABLE"
    assert all_pass["is_stable"] is True
    assert one_transition["status"] == "STABLE"
    assert one_transition["transition_count"] == 1


def test_comparable_metadata_filters_other_test_and_environment():
    result = calculate_flakiness(
        [
            {"test_key": "target", "environment_fingerprint": "env", "outcome": "PASS"},
            {"test_key": "other", "environment_fingerprint": "env", "outcome": "FAIL"},
            {"test_key": "target", "environment_fingerprint": "other", "outcome": "FAIL"},
            {"test_key": "target", "environment_fingerprint": "env", "outcome": "PASS"},
            {"test_key": "target", "environment_fingerprint": "env", "outcome": "PASS"},
        ],
        test_key="target",
        environment_fingerprint="env",
    )

    assert result["comparable_attempts"] == 3
    assert result["sample_count"] == 3
    assert result["status"] == "INSUFFICIENT_DATA"


def test_quarantine_active_state_is_read_only():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    quarantine = {
        "status": "ACTIVE",
        "starts_at": now - timedelta(hours=1),
        "expires_at": now + timedelta(hours=1),
    }

    result = validate_quarantine_state(quarantine, now=now)

    assert result["valid"] is True
    assert result["is_active"] is True
    assert quarantine["status"] == "ACTIVE"
    assert is_quarantine_active(quarantine, now=now) is True


def test_quarantine_transition_validation_is_manual_and_read_only():
    release = validate_quarantine_transition("ACTIVE", "RELEASED")
    reactivate = validate_quarantine_transition("RELEASED", "ACTIVE")
    invalid = validate_quarantine_transition("unknown", "ACTIVE")

    assert release.valid is True
    assert bool(release) is True
    assert reactivate.valid is False
    assert reactivate.reason == "TERMINAL_STATE"
    assert invalid["reason"] == "INVALID_STATUS"


def test_quarantine_released_expired_and_not_started_states_are_invalid():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    base = {"starts_at": now - timedelta(hours=2), "expires_at": now + timedelta(hours=2)}

    released = validate_quarantine_state({**base, "status": "RELEASED"}, now=now)
    expired = validate_quarantine_state(
        {"status": "ACTIVE", "starts_at": now - timedelta(hours=2), "expires_at": now},
        now=now,
    )
    future = validate_quarantine_state(
        {"status": "ACTIVE", "starts_at": now + timedelta(hours=1), "expires_at": None},
        now=now,
    )

    assert released["reason"] == "RELEASED"
    assert released["is_active"] is False
    assert expired["reason"] == "EXPIRED"
    assert expired["is_active"] is False
    assert future["reason"] == "NOT_STARTED"
    assert future["is_active"] is False
