from apps.ui_automation.evidence import from_bytes, manifest


def test_ui_evidence_is_content_addressed_and_deterministic():
    first = from_bytes(b'screenshot', evidence_type='SCREENSHOT', mime_type='image/png')
    second = from_bytes(b'trace', evidence_type='TRACE', mime_type='application/zip')

    output = manifest([first, second])

    assert output[0]['evidence_type'] == 'SCREENSHOT'
    assert output[0]['size_bytes'] == len(b'screenshot')
    assert len(output[0]['sha256']) == 64
    assert output == manifest([second, first])
