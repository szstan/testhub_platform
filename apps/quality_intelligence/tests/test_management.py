"""Backfill command and cross-module safety checks for QI-07."""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError

from apps.quality_intelligence.models import QualityRun


@pytest.fixture
def command_execution(db):
    from apps.api_testing.models import ApiProject, TestExecution, TestSuite

    owner = get_user_model().objects.create_user(username="qi-command-owner", password="pw")
    project = ApiProject.objects.create(name="QI command project", project_type="HTTP", status="IN_PROGRESS", owner=owner)
    suite = TestSuite.objects.create(project=project, name="QI command suite", created_by=owner)
    return TestExecution.objects.create(
        test_suite=suite,
        status="COMPLETED",
        total_requests=1,
        passed_requests=1,
        failed_requests=0,
        results=[
            {
                "name": "health",
                "method": "GET",
                "url": "https://example.test/health",
                "status_code": 200,
                "response_time": 12,
                "passed": True,
            }
        ],
        executed_by=owner,
    )


@pytest.mark.django_db
def test_ingest_quality_dry_run_does_not_write(command_execution):
    output = StringIO()
    call_command(
        "ingest_quality",
        "--source",
        "API",
        "--ids",
        str(command_execution.pk),
        "--dry-run",
        stdout=output,
    )

    assert "[DRY-RUN] API: processed=1, failed=0" in output.getvalue()
    assert QualityRun.objects.count() == 0


@pytest.mark.django_db
def test_ingest_quality_repeat_is_idempotent_and_reports_success(command_execution):
    first_output = StringIO()
    call_command("ingest_quality", "--source", "API", "--ids", str(command_execution.pk), stdout=first_output)
    second_output = StringIO()
    call_command("ingest_quality", "--source", "API", "--ids", str(command_execution.pk), stdout=second_output)

    assert "API: processed=1, failed=0" in first_output.getvalue()
    assert "API: processed=1, failed=0" in second_output.getvalue()
    assert QualityRun.objects.count() == 1
    assert QualityRun.objects.get().source_execution_id == str(command_execution.pk)


@pytest.mark.django_db
def test_ingest_quality_requires_source_for_explicit_ids():
    with pytest.raises(CommandError, match="--source is required"):
        call_command("ingest_quality", "--ids", "1")
