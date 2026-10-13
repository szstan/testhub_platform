from apps.quality_intelligence.services.baseline import (
    compare_baseline,
    compare_metric,
    compare_performance_baseline,
)


def _baseline(**overrides):
    value = {
        "value": 100,
        "tolerance_ratio": 0.10,
        "direction": "LOWER_IS_BETTER",
        "source_type": "API",
        "profile_id": "profile-1",
        "environment_fingerprint": "env-1",
    }
    value.update(overrides)
    return value


def _actual(value=105, **overrides):
    result = {
        "value": value,
        "source_type": "API",
        "profile_id": "profile-1",
        "environment_fingerprint": "env-1",
    }
    result.update(overrides)
    return result


def test_lower_is_better_uses_tolerance_ratio_and_reports_delta():
    within = compare_baseline(_actual(109), _baseline())
    regressed = compare_baseline(_actual(111), _baseline())

    assert within["verdict"] == "WITHIN_TOLERANCE"
    assert within["delta"] == 9
    assert within["allowed_delta"] == 10
    assert within["tolerance_ratio"] == 0.1
    assert regressed["verdict"] == "REGRESSED"
    assert regressed["delta_pct"] == 11


def test_compare_metric_exposes_scalar_contract():
    within = compare_metric(105, 100, 0.10)
    regressed = compare_metric(111, 100, 0.10)

    assert within.verdict == "WITHIN_TOLERANCE"
    assert within["delta"] == 5
    assert regressed["verdict"] == "REGRESSED"


def test_higher_is_better_reverses_regression_direction():
    baseline = _baseline(direction="HIGHER_IS_BETTER")

    assert compare_baseline(_actual(91), baseline)["verdict"] == "WITHIN_TOLERANCE"
    assert compare_baseline(_actual(89), baseline)["verdict"] == "REGRESSED"


def test_absolute_tolerance_can_be_selected_for_unit_values():
    baseline = _baseline(tolerance_ratio=None, tolerance=5, tolerance_mode="ABSOLUTE")

    assert compare_baseline(_actual(105), baseline)["verdict"] == "WITHIN_TOLERANCE"
    assert compare_baseline(_actual(106), baseline)["verdict"] == "REGRESSED"


def test_source_profile_and_environment_mismatch_is_not_comparable():
    result = compare_baseline(
        _actual(source_type="PERF"),
        _baseline(),
    )

    assert result["verdict"] == "NOT_COMPARABLE"
    assert "source_type" in result["mismatch_fields"]

    profile_result = compare_baseline(_actual(profile_id="profile-2"), _baseline())
    environment_result = compare_baseline(
        _actual(environment_fingerprint="env-2"), _baseline()
    )
    assert profile_result["verdict"] == "NOT_COMPARABLE"
    assert environment_result["verdict"] == "NOT_COMPARABLE"


def test_multi_metric_comparison_returns_per_metric_details_and_checks_source_asset():
    actual = {
        "latency": 105,
        "throughput": 95,
        "source_asset_id": "asset-1",
    }
    baseline = {
        "metrics": {"latency": 100, "throughput": 100},
        "tolerance_ratio": 0.10,
        "source_type": "API",
        "source_asset_id": "asset-1",
        "profile_id": "profile-1",
        "environment_fingerprint": "env-1",
    }
    context = {
        "source_type": "API",
        "source_asset_id": "asset-1",
        "profile_id": "profile-1",
        "environment_fingerprint": "env-1",
    }

    result = compare_baseline(actual, baseline, context)
    mismatch = compare_baseline(actual, {**baseline, "source_asset_id": "asset-2"}, context)

    assert result["verdict"] == "WITHIN_TOLERANCE"
    assert set(result["comparisons"]) == {"latency", "throughput"}
    assert mismatch["verdict"] == "NOT_COMPARABLE"
    assert "source_asset_id" in mismatch["comparisons"]["latency"]["mismatch_fields"]


def test_no_baseline_is_explicit_and_does_not_require_a_database():
    result = compare_baseline(_actual(101), None)

    assert result["verdict"] == "NO_BASELINE"
    assert result["status"] == "NO_BASELINE"


def test_missing_metric_context_is_error():
    result = compare_baseline(
        {"latency": 105},
        {
            "metrics": {"latency": 100},
            "tolerance_ratio": 0.10,
            "source_type": "API",
            "profile_id": "profile-1",
            "environment_fingerprint": "env-1",
        },
    )

    assert result["verdict"] == "ERROR"
    assert result["reason"] == "MISSING_SOURCE_CONTEXT"


def test_invalid_metric_or_direction_returns_error():
    invalid_value = compare_baseline(_actual("not-a-number"), _baseline())
    invalid_direction = compare_baseline(_actual(101), _baseline(direction="SIDEWAYS"))

    assert invalid_value["verdict"] == "ERROR"
    assert invalid_direction["verdict"] == "ERROR"


def test_performance_adapter_reads_summary_and_does_not_mutate_inputs():
    baseline = {
        "metrics": {"p95_rt": 100},
        "tolerance": {"rt_degrade_pct": 10},
        "scenario_id": 7,
        "environment_fingerprint": "env-1",
    }
    execution = {
        "summary": {"p95_rt": 108},
        "scenario_id": 7,
        "environment_fingerprint": "env-1",
    }

    result = compare_performance_baseline(execution, baseline, metric_key="p95_rt")

    assert result["verdict"] == "WITHIN_TOLERANCE"
    assert result["source_type"] == "PERF"
    assert result["tolerance_ratio"] == 0.1
    assert baseline == {
        "metrics": {"p95_rt": 100},
        "tolerance": {"rt_degrade_pct": 10},
        "scenario_id": 7,
        "environment_fingerprint": "env-1",
    }
    assert execution == {
        "summary": {"p95_rt": 108},
        "scenario_id": 7,
        "environment_fingerprint": "env-1",
    }
