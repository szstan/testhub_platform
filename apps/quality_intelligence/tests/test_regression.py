"""Cross-module regression and bounded performance-signal checks."""

import pytest
from django.contrib.auth import get_user_model

from apps.quality_intelligence.adapters.perf import PerfSourceAdapter
from apps.quality_intelligence.models import QualityRun
from apps.quality_intelligence.services.ingestion import ingest_source


@pytest.fixture
def performance_execution(db):
    from apps.perf_testing.models import PerfExecution, PerfMetricSample, PerfProject, PerfScenario

    owner = get_user_model().objects.create_user(username="qi-perf-owner", password="pw")
    project = PerfProject.objects.create(name="QI perf project", owner=owner)
    scenario = PerfScenario.objects.create(name="QI perf scenario", project=project, created_by=owner)
    execution = PerfExecution.objects.create(
        scenario=scenario,
        project=project,
        execution_no="QI-PERF-BOUND-001",
        status="COMPLETED",
        sla_result="PASSED",
        verdict="PASSED",
        summary={"p95_rt": 80, "tps": 10},
        executed_by=owner,
    )
    PerfMetricSample.objects.bulk_create([
        PerfMetricSample(execution=execution, ts_offset=index, tps=10 + index, p95_rt=80 + index)
        for index in range(3)
    ])
    return execution


@pytest.mark.django_db
def test_performance_adapter_caps_samples_and_emits_warning(performance_execution):
    normalized = PerfSourceAdapter(max_samples=2).normalize(performance_execution.pk)

    assert "perf_metric_samples_truncated" in normalized.warnings
    assert len(normalized.source_snapshot["metric_samples"]) == 2
    assert [entry["ts_offset"] for entry in normalized.source_snapshot["metric_samples"]] == [0, 1]


@pytest.mark.django_db
def test_quality_projection_is_idempotent_rebuildable_and_source_is_immutable(performance_execution):
    from apps.perf_testing.models import PerfExecution, PerfMetricSample

    source_before = PerfExecution.objects.values(
        "status", "sla_result", "verdict", "summary", "error_message"
    ).get(pk=performance_execution.pk)
    sample_count_before = PerfMetricSample.objects.filter(execution=performance_execution).count()

    first = ingest_source("PERF", performance_execution.pk)
    second = ingest_source("PERF", performance_execution.pk)
    assert first.created is True
    assert second.reused is True
    assert QualityRun.objects.filter(source_type="PERF", source_execution_id=str(performance_execution.pk)).count() == 1

    rebuilt = ingest_source("PERF", performance_execution.pk, force=True)
    assert rebuilt.updated is True
    assert QualityRun.objects.filter(source_type="PERF", source_execution_id=str(performance_execution.pk)).count() == 1

    QualityRun.objects.filter(pk=first.quality_run.pk).delete()
    assert not QualityRun.objects.filter(source_type="PERF", source_execution_id=str(performance_execution.pk)).exists()
    recreated = ingest_source("PERF", performance_execution.pk)
    assert recreated.created is True

    assert PerfExecution.objects.values(
        "status", "sla_result", "verdict", "summary", "error_message"
    ).get(pk=performance_execution.pk) == source_before
    assert PerfMetricSample.objects.filter(execution=performance_execution).count() == sample_count_before
