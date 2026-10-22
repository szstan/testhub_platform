from apps.quality_intelligence.services.risk_selection import (
    score_test_risk,
    select_regression_candidates,
)


def test_risk_score_explains_failure_flaky_priority_and_change_impact():
    result = score_test_risk(
        {'test_key': 'login', 'priority': 'P0'},
        ['PASS', 'FAIL', 'PASS', 'FAIL', 'PASS'],
        change_impact=1.0,
    )

    assert result['test_key'] == 'login'
    assert result['risk_level'] == 'CRITICAL'
    assert set(result['reasons']) == {'RECENT_FAILURES', 'FLAKY_PATTERN', 'P0_PRIORITY', 'CHANGED_AREA'}
    assert result['risk_score'] > 75


def test_regression_selection_is_deterministic_and_excludes_quarantine():
    candidates = [
        {'test_key': 'stable', 'priority': 'P3'},
        {'test_key': 'critical', 'priority': 'P0'},
        {'test_key': 'quarantined', 'priority': 'P0', 'quarantined': True},
    ]

    selected = select_regression_candidates(
        candidates,
        histories={'critical': ['PASS', 'FAIL', 'PASS', 'FAIL', 'PASS']},
        change_impacts={'critical': 1.0},
        limit=2,
    )

    assert [item['test_key'] for item in selected] == ['critical', 'stable']
    assert all(item['test_key'] != 'quarantined' for item in selected)
