import json
from io import StringIO

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_quality_scale_report_is_read_only_json():
    output = StringIO()
    call_command('quality_scale_report', '--json', stdout=output)
    payload = json.loads(output.getvalue())
    assert 'execution_jobs' in payload
    assert 'execution_artifact_bytes' in payload
    assert 'expired_execution_artifacts' in payload
    assert 'thresholds' in payload
    assert 'warnings' in payload
    assert 'unmapped_quality_runs' in payload
    assert 'unmapped_quality_clusters' in payload
