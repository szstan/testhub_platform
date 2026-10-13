"""QI-04 adapter and ingestion boundary tests."""

from __future__ import annotations

from copy import deepcopy

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.quality_intelligence.adapters.base import SourceNotTerminalError
from apps.quality_intelligence.models import (
    QualityEvidence,
    QualityFailure,
    QualityItemOutcome,
    QualityMetricObservation,
    QualityRun,
    QualityRunStatus,
    QualityRunItem,
)
from apps.quality_intelligence.services.ingestion import ingest_many, ingest_source


@pytest.fixture
def owner(db):
    user_model = get_user_model()
    return user_model.objects.create_user(username="qi-owner", password="test-password")


@pytest.fixture
def api_execution(owner):
    from apps.api_testing.models import ApiProject, TestExecution, TestSuite

    project = ApiProject.objects.create(
        name="API quality project",
        project_type="HTTP",
        status="IN_PROGRESS",
        owner=owner,
    )
    suite = TestSuite.objects.create(project=project, name="Checkout API", created_by=owner)
    execution = TestExecution.objects.create(
        test_suite=suite,
        status="COMPLETED",
        total_requests=1,
        passed_requests=0,
        failed_requests=1,
        results=[
            {
                "name": "checkout",
                "method": "POST",
                "url": "https://example.test/checkout?token=private-token",
                "status_code": 500,
                "response_time": 42.5,
                "passed": False,
                "error": "password=source-secret",
            }
        ],
        executed_by=owner,
    )
    return execution


@pytest.fixture
def ui_execution(owner):
    from apps.ui_automation.models import TestExecution, TestSuite, UiProject

    project = UiProject.objects.create(
        name="UI quality project",
        base_url="https://example.test",
        owner=owner,
    )
    suite = TestSuite.objects.create(project=project, name="Checkout UI")
    execution = TestExecution.objects.create(
        project=project,
        test_suite=suite,
        status="SUCCESS",
        total_cases=1,
        passed_cases=1,
        result_data={
            "test_cases": [
                {
                    "test_case_id": 77,
                    "test_case_name": "checkout form",
                    "status": "passed",
                    "steps": [{"message": "Authorization: Bearer ui-secret"}],
                    "screenshots": [{"path": r"C:\\private\\checkout.png", "description": "checkout"}],
                }
            ]
        },
        started_at=timezone.now(),
        finished_at=timezone.now(),
    )
    return execution


@pytest.fixture
def app_execution(owner):
    from apps.app_automation.models import AppProject, AppTestCase, AppTestExecution

    project = AppProject.objects.create(name="APP quality project", owner=owner)
    test_case = AppTestCase.objects.create(
        project=project,
        name="device checkout",
        ui_flow={},
        variables=[],
    )
    execution = AppTestExecution.objects.create(
        test_case=test_case,
        status="COMPLETED",
        result="failed",
        total_steps=3,
        passed_steps=2,
        failed_steps=1,
        error_message="ADB device offline: token=app-secret",
        started_at=timezone.now(),
        finished_at=timezone.now(),
        duration=3.2,
    )
    return execution


@pytest.fixture
def perf_execution(owner):
    from apps.perf_testing.models import PerfExecution, PerfProject, PerfRequestStat, PerfScenario

    project = PerfProject.objects.create(name="PERF quality project", owner=owner)
    scenario = PerfScenario.objects.create(name="checkout load", project=project, created_by=owner)
    execution = PerfExecution.objects.create(
        scenario=scenario,
        project=project,
        execution_no="PERF-QI-001",
        status="COMPLETED",
        sla_result="PASSED",
        verdict="PASSED",
        summary={
            "tps": 12.5,
            "p95_rt": 120,
            "p99_rt": 180,
            "error_rate": 0.5,
            "total_requests": 100,
            "success_requests": 99,
            "failed_requests": 1,
        },
        load_snapshot={"concurrency": 4},
        worker_host="runner-1",
    )
    PerfRequestStat.objects.create(
        execution=execution,
        step_name="checkout request",
        method="POST",
        url="https://example.test/checkout",
        total=100,
        success=99,
        failed=1,
        error_rate=1,
        tps=12.5,
        avg_rt=80,
        p95_rt=120,
        error_detail=[{"message": "HTTP 500"}],
    )
    return execution


@pytest.mark.django_db
def test_all_four_source_adapters_ingest_without_writing_source_rows(
    api_execution, ui_execution, app_execution, perf_execution
):
    sources = [
        ("API", api_execution),
        ("UI", ui_execution),
        ("APP", app_execution),
        ("PERF", perf_execution),
    ]
    before = {
        (source_type, execution.pk): {
            field.name: deepcopy(getattr(execution, field.name))
            for field in execution._meta.concrete_fields
        }
        for source_type, execution in sources
    }

    results = [ingest_source(source_type, execution.pk) for source_type, execution in sources]

    assert {result.quality_run.source_type for result in results} == {"API", "UI", "APP", "PERF"}
    assert all(result.created for result in results)
    assert QualityRun.objects.count() == 4
    assert QualityRunItem.objects.count() >= 4
    assert QualityMetricObservation.objects.count() >= 4
    assert all(
        {
            field.name: deepcopy(getattr(execution, field.name))
            for field in execution._meta.concrete_fields
        }
        == before[(source_type, execution.pk)]
        for source_type, execution in sources
    )

    serialized = str(list(QualityRun.objects.values("summary", "provenance", "warnings")))
    serialized += str(list(QualityRunItem.objects.values("details")))
    serialized += str(list(QualityEvidence.objects.values("snippet", "source_uri")))
    serialized += str(list(QualityFailure.objects.values("message", "details")))
    assert "private-token" not in serialized
    assert "source-secret" not in serialized
    assert "ui-secret" not in serialized
    assert "app-secret" not in serialized


@pytest.mark.django_db
def test_repeated_ingestion_reuses_run_and_does_not_duplicate_children(api_execution):
    first = ingest_source("API", api_execution.pk)
    first_counts = (
        QualityRun.objects.count(),
        QualityRunItem.objects.count(),
        QualityEvidence.objects.count(),
        QualityFailure.objects.count(),
        QualityMetricObservation.objects.count(),
    )

    second = ingest_source("API", api_execution.pk)
    second_counts = (
        QualityRun.objects.count(),
        QualityRunItem.objects.count(),
        QualityEvidence.objects.count(),
        QualityFailure.objects.count(),
        QualityMetricObservation.objects.count(),
    )

    assert first.quality_run.pk == second.quality_run.pk
    assert second.reused is True
    assert second_counts == first_counts


@pytest.mark.django_db
def test_running_source_is_rejected_without_quality_projection(api_execution):
    api_execution.status = "RUNNING"
    api_execution.save(update_fields=["status"])

    with pytest.raises(SourceNotTerminalError):
        ingest_source("API", api_execution.pk)

    assert not QualityRun.objects.exists()


@pytest.mark.django_db
def test_batch_ingestion_is_failure_isolated(api_execution):
    api_execution.status = "RUNNING"
    api_execution.save(update_fields=["status"])

    batch = ingest_many("API", [api_execution.pk, 999999], allow_missing=False)

    assert batch.succeeded == 0
    assert batch.failed == 2
    assert QualityRun.objects.count() == 0


@pytest.mark.django_db
def test_missing_source_is_retained_as_source_missing_and_dry_run_is_read_only():
    dry_run = ingest_source("APP", "missing-1", dry_run=True)
    assert dry_run.dry_run is True
    assert dry_run.source_missing is True
    assert not QualityRun.objects.exists()

    stored = ingest_source("APP", "missing-1")
    assert stored.source_missing is True
    assert stored.quality_run.quality_status == QualityRunStatus.SOURCE_MISSING
    assert stored.quality_run.warnings == ["source_missing"]


@pytest.mark.django_db
def test_partial_source_without_result_is_not_promoted_to_pass(api_execution):
    api_execution.results = []
    api_execution.total_requests = 1
    api_execution.passed_requests = 0
    api_execution.failed_requests = 0
    api_execution.save(update_fields=["results", "total_requests", "passed_requests", "failed_requests"])

    result = ingest_source("API", api_execution.pk)

    assert result.quality_run.quality_status == QualityRunStatus.PARTIAL
    assert result.quality_run.warnings
    assert result.quality_run.items.first() is None
    assert result.quality_run.summary["outcome"] == QualityItemOutcome.INCONCLUSIVE
