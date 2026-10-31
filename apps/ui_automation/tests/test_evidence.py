import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.test import override_settings

from apps.ui_automation.evidence import from_bytes, from_data_url, manifest, project_screenshots
from apps.ui_automation.mcp_runner import _finish_execution
from apps.ui_automation.views import (
    AICaseViewSet,
    AIExecutionRecordViewSet,
    TestCaseViewSet as UiTestCaseViewSet,
)


def test_screenshot_projection_carries_safe_content_addressed_metadata():
    payload = b'png-bytes'
    data_url = 'data:image/png;base64,' + base64.b64encode(payload).decode('ascii')
    projected = project_screenshots(
        [{
            'url': data_url,
            'description': 'failure screenshot',
            'step_number': 4,
            'timestamp': '2026-10-31T00:00:00Z',
        }],
        producer='ui_test_executor',
        execution_id=42,
        item_id=7,
    )

    reference = projected[0]['evidence_reference']
    assert projected[0]['url'] == data_url
    assert reference['sha256'] == hashlib.sha256(payload).hexdigest()
    assert reference['size_bytes'] == len(payload)
    assert reference['mime_type'] == 'image/png'
    assert reference['uri'] == f"urn:sha256:{reference['sha256']}"
    assert reference['availability'] == 'AVAILABLE'
    assert set(reference) == {
        'schema_version', 'evidence_type', 'sha256', 'size_bytes', 'mime_type',
        'uri', 'availability', 'metadata',
    }
    assert reference['metadata'] == {
        'producer': 'ui_test_executor',
        'execution_id': '42',
        'item_id': '7',
        'step_number': 4,
        'captured_at': '2026-10-31T00:00:00Z',
    }


def test_mcp_screenshot_producer_persists_evidence_projection():
    payload = b'mcp-screenshot'
    data_url = 'data:image/png;base64,' + base64.b64encode(payload).decode('ascii')
    execution = SimpleNamespace(
        id=24,
        test_case_id=9,
        save=Mock(),
    )

    _finish_execution(
        execution,
        'passed',
        [],
        [{'url': data_url, 'step_number': 2, 'timestamp': '2026-10-31T00:00:00Z'}],
        '',
        time.time(),
    )

    reference = execution.screenshots[0]['evidence_reference']
    assert reference['sha256'] == hashlib.sha256(payload).hexdigest()
    assert reference['size_bytes'] == len(payload)
    assert reference['mime_type'] == 'image/png'
    assert reference['uri'] == f"urn:sha256:{reference['sha256']}"
    assert reference['metadata'] == {
        'producer': 'mcp_runner',
        'execution_id': '24',
        'item_id': '9',
        'step_number': 2,
        'captured_at': '2026-10-31T00:00:00Z',
    }
    execution.save.assert_called_once()


@pytest.mark.parametrize('viewset_class', [AICaseViewSet, AIExecutionRecordViewSet])
def test_ai_gif_producers_persist_content_addressed_reference(
    tmp_path, monkeypatch, viewset_class
):
    payload = b'GIF89a test animation'
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'agent_history.gif').write_bytes(payload)
    media_root = tmp_path / 'media'
    captured_at = datetime(2026, 10, 31, tzinfo=timezone.utc)
    execution = SimpleNamespace(
        id=55,
        pk=55,
        ai_case_id=13,
        case_name='checkout / flow',
        end_time=captured_at,
        gif_path=None,
        screenshots_sequence=[{'description': 'existing frame'}],
    )

    with override_settings(MEDIA_ROOT=str(media_root)):
        viewset_class()._process_gif_recording(execution, history=None)

    assert execution.gif_path.startswith('media/ai_recording/')
    assert len(execution.screenshots_sequence) == 2
    reference = execution.screenshots_sequence[-1]['evidence_reference']
    assert reference['evidence_type'] == 'RECORDING'
    assert reference['sha256'] == hashlib.sha256(payload).hexdigest()
    assert reference['size_bytes'] == len(payload)
    assert reference['mime_type'] == 'image/gif'
    assert reference['uri'] == f"urn:sha256:{reference['sha256']}"
    assert reference['availability'] == 'AVAILABLE'
    assert reference['metadata'] == {
        'producer': 'ui_ai_execution.gif',
        'execution_id': '55',
        'item_id': '13',
        'step_number': None,
        'captured_at': captured_at.isoformat(),
    }
    assert str(tmp_path) not in json.dumps(reference)
    stored_file = media_root / 'ai_recording' / execution.gif_path.rsplit('/', 1)[-1]
    assert stored_file.read_bytes() == payload


def test_view_screenshot_producer_persists_evidence_projection(monkeypatch):
    from apps.ui_automation import selenium_engine, views
    from apps.ui_automation.models import TestCaseExecution

    payload = b'view-screenshot'
    data_url = 'data:image/png;base64,' + base64.b64encode(payload).decode('ascii')
    step = SimpleNamespace(
        action_type='screenshot',
        description='capture current page',
        input_value='',
        wait_time=0,
        assert_type='',
        assert_value='',
        element=None,
    )

    class Steps:
        def all(self):
            return self

        def order_by(self, _field):
            return [step]

    test_case = SimpleNamespace(
        id=7,
        name='screenshot case',
        project=SimpleNamespace(base_url=''),
        steps=Steps(),
    )
    execution = SimpleNamespace(
        id=91,
        test_case_id=7,
        status='running',
        save=Mock(),
    )

    class FakeSeleniumEngine:
        @staticmethod
        def check_execution_environment(_browser):
            return True, ''

        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def execute_step(self, _step, _element_data):
            return True, 'step passed', data_url

        def stop(self):
            pass

    class FakeSerializer:
        def __init__(self, _execution):
            self.data = {}

    monkeypatch.setattr(selenium_engine, 'SeleniumTestEngine', FakeSeleniumEngine)
    monkeypatch.setattr(TestCaseExecution.objects, 'create', lambda **_kwargs: execution)
    monkeypatch.setattr(views, 'TestCaseExecutionSerializer', FakeSerializer)
    monkeypatch.setattr(views, 'log_operation', lambda *_args: None)
    viewset = UiTestCaseViewSet()
    viewset.get_object = lambda: test_case
    request = SimpleNamespace(
        data={'engine': 'selenium', 'browser': 'chrome', 'headless': True},
        user=SimpleNamespace(username='evidence-test'),
    )

    response = viewset.run(request)

    reference = execution.screenshots[0]['evidence_reference']
    assert response.status_code == 200
    assert reference['sha256'] == hashlib.sha256(payload).hexdigest()
    assert reference['size_bytes'] == len(payload)
    assert reference['mime_type'] == 'image/png'
    assert reference['uri'] == f"urn:sha256:{reference['sha256']}"
    assert reference['metadata'] == {
        'producer': 'ui_views.run_case',
        'execution_id': '91',
        'item_id': '7',
        'step_number': 1,
        'captured_at': execution.screenshots[0]['timestamp'],
    }
    execution.save.assert_called_once()


def test_screenshot_projection_leaves_non_data_url_untouched():
    screenshots = [{'url': '/media/private/screenshots/run.png'}]
    assert project_screenshots(screenshots, producer='ui_views') == screenshots


def test_from_data_url_rejects_invalid_payload():
    with pytest.raises(ValueError, match='invalid base64'):
        from_data_url('data:image/png;base64,not-base64!')


def test_ui_evidence_is_content_addressed_and_deterministic():
    first = from_bytes(b'screenshot', evidence_type='SCREENSHOT', mime_type='image/png')
    second = from_bytes(b'trace', evidence_type='TRACE', mime_type='application/zip')

    output = manifest([first, second])

    assert output[0]['evidence_type'] == 'SCREENSHOT'
    assert output[0]['size_bytes'] == len(b'screenshot')
    assert len(output[0]['sha256']) == 64
    assert output == manifest([second, first])
