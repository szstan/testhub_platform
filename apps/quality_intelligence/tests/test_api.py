"""QI-05 API permissions, ingestion and governance checks."""

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from apps.quality_intelligence.models import QualityFailure, QualityRun


@pytest.fixture
def api_users(db):
    user_model = get_user_model()
    owner = user_model.objects.create_user(username="qi-api-owner", password="pw")
    member = user_model.objects.create_user(username="qi-api-member", password="pw")
    outsider = user_model.objects.create_user(username="qi-api-outsider", password="pw")
    return owner, member, outsider


@pytest.fixture
def api_source(api_users):
    from apps.api_testing.models import ApiProject, TestExecution, TestSuite

    owner, member, _ = api_users
    project = ApiProject.objects.create(
        name="QI API project",
        project_type="HTTP",
        status="IN_PROGRESS",
        owner=owner,
    )
    project.members.add(member)
    suite = TestSuite.objects.create(project=project, name="QI suite", created_by=owner)
    execution = TestExecution.objects.create(
        test_suite=suite,
        status="COMPLETED",
        total_requests=1,
        passed_requests=0,
        failed_requests=1,
        results=[
            {
                "name": "checkout",
                "method": "GET",
                "url": "https://example.test/checkout",
                "status_code": 500,
                "response_time": 40,
                "passed": False,
                "error": "Assertion failed: password=should-not-leak",
            }
        ],
        executed_by=owner,
    )
    return execution


@pytest.fixture
def authenticated_client(api_users):
    client = APIClient()
    client.force_authenticate(user=api_users[0])
    return client


@pytest.mark.django_db
@override_settings(QUALITY_INTELLIGENCE_ENABLED=False)
def test_quality_api_is_hidden_when_feature_is_disabled():
    response = APIClient().get("/api/quality-intelligence/runs/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_quality_api_requires_authentication():
    response = APIClient().get("/api/quality-intelligence/runs/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_ingest_rejects_malformed_requests_with_400(authenticated_client):
    missing = authenticated_client.post("/api/quality-intelligence/ingest/", {}, format="json")
    assert missing.status_code == 400
    assert missing.data["error_code"] == "INVALID_INGEST_REQUEST"

    invalid_id = authenticated_client.post(
        "/api/quality-intelligence/ingest/",
        {"source_type": "API", "source_execution_id": "not-an-integer"},
        format="json",
    )
    assert invalid_id.status_code == 400
    assert invalid_id.data["error_code"] == "INVALID_SOURCE_EXECUTION"


@pytest.mark.django_db
def test_ingest_is_idempotent_and_running_source_returns_409(authenticated_client, api_source):
    payload = {"source_type": "API", "source_execution_id": str(api_source.pk)}
    first = authenticated_client.post("/api/quality-intelligence/ingest/", payload, format="json")
    second = authenticated_client.post("/api/quality-intelligence/ingest/", payload, format="json")

    assert first.status_code == 201
    assert "should-not-leak" not in str(first.data)
    assert second.status_code == 200
    assert second.data["ingestion"]["reused"] is True
    assert QualityRun.objects.count() == 1

    api_source.status = "RUNNING"
    api_source.save(update_fields=["status"])
    running = authenticated_client.post("/api/quality-intelligence/ingest/", payload, format="json")
    assert running.status_code == 409
    assert running.data["error_code"] == "SOURCE_RUNNING"


@pytest.mark.django_db
def test_quality_project_members_can_read_but_outsiders_get_404(api_users, api_source):
    owner, member, outsider = api_users
    owner_client = APIClient()
    owner_client.force_authenticate(owner)
    ingest = owner_client.post(
        "/api/quality-intelligence/ingest/",
        {"source_type": "API", "source_execution_id": str(api_source.pk)},
        format="json",
    )
    run_id = ingest.data["run"]["id"]

    member_client = APIClient()
    member_client.force_authenticate(member)
    assert member_client.get(f"/api/quality-intelligence/runs/{run_id}/").status_code == 200

    outsider_client = APIClient()
    outsider_client.force_authenticate(outsider)
    assert outsider_client.get(f"/api/quality-intelligence/runs/{run_id}/").status_code == 404
    assert outsider_client.get("/api/quality-intelligence/runs/").data["count"] == 0


@pytest.mark.django_db
def test_run_items_evidence_summary_and_failure_review_are_safe(authenticated_client, api_source):
    ingest = authenticated_client.post(
        "/api/quality-intelligence/ingest/",
        {"source_type": "API", "source_execution_id": str(api_source.pk)},
        format="json",
    )
    run_id = ingest.data["run"]["id"]
    item_response = authenticated_client.get(f"/api/quality-intelligence/runs/{run_id}/items/")
    assert item_response.status_code == 200
    item_id = item_response.data["results"][0]["id"]
    assert authenticated_client.get(f"/api/quality-intelligence/items/{item_id}/evidence/").status_code == 200

    summary = authenticated_client.get("/api/quality-intelligence/summary/")
    trend = authenticated_client.get("/api/quality-intelligence/trend/")
    assert summary.status_code == 200
    assert summary.data["failure_count"] == 1
    assert trend.status_code == 200
    assert trend.data["classification_mode"] == "deterministic"

    failure = QualityFailure.objects.get(run_id=run_id)
    reviewed = authenticated_client.patch(
        f"/api/quality-intelligence/failures/{failure.pk}/review/",
        {"taxonomy_code": "TEST", "review_note": "checked"},
        format="json",
    )
    assert reviewed.status_code == 200
    assert reviewed.data["classifier"] == "HUMAN"
    assert "should-not-leak" not in str(reviewed.data)


@pytest.mark.django_db
def test_baseline_recompute_and_manual_quarantine(authenticated_client, api_source):
    ingest = authenticated_client.post(
        "/api/quality-intelligence/ingest/",
        {"source_type": "API", "source_execution_id": str(api_source.pk)},
        format="json",
    )
    run_id = ingest.data["run"]["id"]
    project_id = str(api_source.test_suite.project_id)
    profiles = authenticated_client.get("/api/quality-intelligence/profiles/")
    assert profiles.status_code == 200
    profile_id = profiles.data["results"][0]["id"]

    baseline = authenticated_client.post(
        "/api/quality-intelligence/baselines/",
        {
            "source_type": "API",
            "project_type": "API",
            "project_id": project_id,
            "profile_id": profile_id,
            "metric_key": "response_time_ms",
            "environment_fingerprint": "unknown",
            "value": 50,
            "unit": "ms",
            "tolerance": 10,
            "direction": "LOWER_IS_BETTER",
            "snapshot": {"token": "must-not-leak", "path": "C:\\private\\baseline.json"},
        },
        format="json",
    )
    assert baseline.status_code == 201
    assert "must-not-leak" not in str(baseline.data)
    assert "C:\\private\\baseline.json" not in str(baseline.data)

    recompute = authenticated_client.post(f"/api/quality-intelligence/runs/{run_id}/recompute/")
    assert recompute.status_code == 200
    assert recompute.data["comparisons"][0]["verdict"] == "WITHIN_TOLERANCE"

    profiles = authenticated_client.get("/api/quality-intelligence/profiles/")
    assert profiles.status_code == 200
    profile_id = profiles.data["results"][0]["id"]
    quarantine = authenticated_client.post(
        f"/api/quality-intelligence/profiles/{profile_id}/quarantine/",
        {"reason": "manual review"},
        format="json",
    )
    assert quarantine.status_code == 201
    quarantine_id = quarantine.data["id"]
    release = authenticated_client.post(f"/api/quality-intelligence/quarantines/{quarantine_id}/release/")
    assert release.status_code == 200
    assert release.data["status"] == "RELEASED"
