"""Focused, DB-free tests for QI-03A domain services."""

import hashlib
import json

from apps.quality_intelligence.services.hashing import (
    canonical_json,
    fingerprint,
    sha256_hex,
    signature,
)
from apps.quality_intelligence.services.provenance import (
    build_provenance,
    fingerprint_source,
)
from apps.quality_intelligence.services.redaction import (
    REDACTION_MARKER,
    TRUNCATION_MARKER,
    bound_text,
    redact_headers,
    redact_payload,
    redact_text,
    redact_url,
)
from apps.quality_intelligence.services.taxonomy import (
    TAXONOMY_PRIORITY,
    classify_failure,
)


def test_recursive_redaction_preserves_safe_metadata_and_removes_secret_values():
    payload = {
        "name": "checkout",
        "attempt": 2,
        "headers": {
            "Authorization": "Bearer top-secret-token",
            "X-Trace-Id": "trace-123",
            "Cookie": "session=private-cookie",
        },
        "items": [
            {"apiKey": "private-api-key", "label": "safe label"},
            {"message": "password=embedded-password"},
        ],
        "url": "https://user:private-password@example.test/run?token=url-token&ok=yes",
    }

    redacted = redact_payload(payload)
    rendered = json.dumps(redacted, sort_keys=True)

    assert redacted["name"] == "checkout"
    assert redacted["attempt"] == 2
    assert redacted["headers"]["X-Trace-Id"] == "trace-123"
    assert redacted["headers"]["Authorization"] == REDACTION_MARKER
    assert redacted["items"][0]["label"] == "safe label"
    for secret in (
        "top-secret-token",
        "private-cookie",
        "private-api-key",
        "embedded-password",
        "private-password",
        "url-token",
    ):
        assert secret not in rendered


def test_text_url_and_header_helpers_are_bounded_and_redacted():
    assert "do-not-leak" not in redact_text("Authorization: Bearer do-not-leak")
    assert "session-secret" not in redact_text("session_id=session-secret")
    assert "query-secret" not in redact_url(
        "https://example.test/path?password=query-secret&ok=1"
    )
    headers = redact_headers({"x-api-key": "header-secret", "Accept": "application/json"})
    assert headers["x-api-key"] == REDACTION_MARKER
    assert headers["Accept"] == "application/json"
    assert bound_text("abcdef", 4) == TRUNCATION_MARKER[:4]


def test_canonicalization_and_hashes_are_stable():
    first = {"z": [3, 2, 1], "a": {"b": True, "a": "value"}}
    second = {"a": {"a": "value", "b": True}, "z": [3, 2, 1]}

    assert canonical_json(first) == canonical_json(second)
    assert fingerprint(first) == fingerprint(second)
    assert signature(first) == signature(second)
    assert sha256_hex("hello") == hashlib.sha256(b"hello").hexdigest()


def test_provenance_fingerprints_redacted_payload_without_storing_payload():
    payload = {"safe": "value", "nested": {"secret": "do-not-store"}}
    provenance = build_provenance(
        {"source_type": "API", "source_execution_id": "run-7"},
        payload,
        metadata={"status": "FAILED", "token": "also-secret"},
    )

    assert provenance["source_type"] == "API"
    assert provenance["source_execution_id"] == "run-7"
    assert provenance["source_fingerprint"] == fingerprint_source(payload)
    serialized = json.dumps(provenance, sort_keys=True)
    assert "do-not-store" not in serialized
    assert "also-secret" not in serialized
    assert "safe" not in serialized


def test_taxonomy_priority_and_explicit_rule_metadata():
    # A single evidence record intentionally matches several rules.  The
    # documented priority must select the first matching code.
    evidence = {
        "sla_breach": True,
        "status_code": 503,
        "message": "connection timed out; selector locator unavailable",
    }
    result = classify_failure(evidence)

    assert result.taxonomy_code == TAXONOMY_PRIORITY[0] == "SLA_BREACH"
    assert result.classifier == "RULE"
    assert result.rule_id == "QI-03A.TAXONOMY.SLA_BREACH"
    assert result["initial_taxonomy_code"] == "SLA_BREACH"
    assert result["category"] == "PERFORMANCE"
    assert "connection" not in json.dumps(result)


def test_taxonomy_covers_each_priority_rule_without_external_calls():
    examples = {
        "SLA_BREACH": {"sla_breach": True},
        "ASSERTION": {"message": "AssertionError: expected 1 actual 2"},
        "HTTP_STATUS": {"status_code": 404},
        "TIMEOUT": {"message": "request timed out"},
        "NETWORK": {"message": "connection refused"},
        "LOCATOR": {"message": "no such element for locator"},
        "DEVICE": {"message": "device offline"},
        "DRIVER": {"message": "WebDriver failed"},
        "ENVIRONMENT": {"message": "environment dependency missing"},
        "RESOURCE": {"message": "out of memory"},
        "HARNESS": {"message": "test runner fixture failed"},
        "UNKNOWN": {"message": "unclassified result"},
    }

    for expected, evidence in examples.items():
        result = classify_failure(evidence)
        assert result.taxonomy_code == expected
        assert result.classifier == "RULE"
        assert result.rule_id.startswith("QI-03A.TAXONOMY.")
