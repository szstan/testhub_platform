import pytest

from apps.executions.models import ExecutionJob
from apps.executions.run_comparison import evaluate_release_gate
from apps.projects.models import Project
from apps.users.models import User


@pytest.mark.django_db
def test_release_gate_fails_closed_on_metric_threshold_and_baseline_delta():
    user = User.objects.create_user(username='metric-gate-owner', password='pass')
    project = Project.objects.create(name='Metric Gate Project', owner=user)
    baseline = ExecutionJob.objects.create(
        project=project,
        source_type='performance',
        source_id='performance:scenario:1',
        requested_by=user,
        result={'failed': 0, 'unmatched': 0, 'metrics': {'p95': 100, 'error_rate': 1}},
    )
    current = ExecutionJob.objects.create(
        project=project,
        source_type='performance',
        source_id='performance:scenario:1',
        requested_by=user,
        result={'failed': 0, 'unmatched': 0, 'metrics': {'p95': 130, 'error_rate': 2}},
    )

    gate = evaluate_release_gate(
        current,
        baseline=baseline,
        metric_thresholds={
            'p95': {'max': 120, 'max_delta_pct': 20},
            'error_rate': {'max': 1},
        },
        evaluated_by=user,
    )
    assert gate['passed'] is False
    assert any(item['metric'] == 'metric:p95' for item in gate['failures'])
    assert any(item['metric'] == 'metric:error_rate' for item in gate['failures'])


@pytest.mark.django_db
def test_release_gate_missing_metric_fails_closed():
    user = User.objects.create_user(username='missing-metric-owner', password='pass')
    project = Project.objects.create(name='Missing Metric Project', owner=user)
    job = ExecutionJob.objects.create(
        project=project,
        source_type='performance',
        source_id='performance:scenario:2',
        requested_by=user,
        result={'failed': 0, 'unmatched': 0, 'metrics': {}},
    )
    gate = evaluate_release_gate(
        job,
        metric_thresholds={'p99': {'max': 500}},
        evaluated_by=user,
    )
    assert gate['passed'] is False
    assert {'metric': 'metric:p99', 'reason': 'missing_metric'} in gate['failures']
