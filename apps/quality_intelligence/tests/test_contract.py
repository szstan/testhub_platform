import json
from datetime import datetime, timezone

from apps.quality_intelligence.adapters.base import (
    FAIL,
    NormalizedEvidence,
    NormalizedFailure,
    NormalizedItem,
    NormalizedMetric,
    NormalizedRun,
)


def test_normalized_run_contract_is_versioned_and_json_safe():
    run = NormalizedRun(
        source_type='API',
        source_scope='SUITE',
        source_execution_id='api-1',
        source_snapshot={'password': 'do-not-persist'},
        items=[NormalizedItem(test_key='request-1', outcome=FAIL)],
        evidence=[NormalizedEvidence(
            evidence_type='TRACE',
            item_key='request-1',
            source_uri='/reports/trace.zip',
            sha256='abc123',
            captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )],
        failures=[NormalizedFailure(
            item_key='request-1',
            taxonomy_code='ASSERTION',
            message='authorization: secret',
            stack_excerpt=b'raw-binary',
            details={'raw': b'bytes'},
        )],
        metrics=[NormalizedMetric(metric_key='duration', value=12.5, unit='ms', dimensions={'raw': b'bytes'})],
    )

    contract = run.to_contract()
    encoded = json.dumps(contract, ensure_ascii=False)

    assert contract['contract_version'] == '1.0'
    assert contract['schema_version'] == 1
    assert contract['items'][0]['test_key'] == 'request-1'
    assert contract['evidence'][0]['sha256'] == 'abc123'
    assert contract['evidence'][0]['captured_at'].startswith('2026-01-01T00:00:00')
    assert contract['failures'][0]['taxonomy_code'] == 'ASSERTION'
    assert contract['failures'][0]['message'] == 'authorization: [REDACTED]'
    assert contract['failures'][0]['stack_excerpt'] == '[BINARY]'
    assert contract['metrics'][0]['unit'] == 'ms'
    assert 'do-not-persist' not in encoded
