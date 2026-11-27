from contextlib import contextmanager
import os
from types import SimpleNamespace
from unittest.mock import Mock
import traceback

import pytest

pytest.importorskip('airtest.core.api', reason='Airtest is required to import the runner')

from apps.app_automation.runners import ui_flow_runner as flow_module
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner


PRIVATE = 'synthetic-private-path-value'


def make_runner(tmp_path):
    runner = UiFlowRunner.__new__(UiFlowRunner)
    runner.image_base_dir = str(tmp_path / 'images')
    runner.screenshots_dir = str(tmp_path / 'screenshots')
    runner.context = {'local': {}, 'global': {}, 'outputs': {}, 'steps': {}}
    runner.runtime = {'retry_times': 0, 'retry_interval': 0}
    runner._ocr_helper = None
    runner._assertions_executed = 0
    return runner


def test_oserror_filename_attributes_are_redacted_before_allure_and_logging(
    tmp_path, monkeypatch, caplog,
):
    runner = make_runner(tmp_path)
    captured = []
    attached = Mock()
    attached.file = Mock()

    @contextmanager
    def allure_step(_title):
        try:
            yield
        except Exception as error:
            captured.append((
                error,
                str(error),
                repr(error),
                ''.join(traceback.format_exception(type(error), error, error.__traceback__)),
            ))
            raise

    monkeypatch.setattr(flow_module, 'ALLURE_AVAILABLE', True)
    monkeypatch.setattr(flow_module, 'allure', SimpleNamespace(
        step=allure_step,
        attach=attached,
        attachment_type=SimpleNamespace(JSON='json', PNG='png'),
    ))
    monkeypatch.setattr(runner, '_capture_screenshot', Mock(return_value=None))
    caplog.set_level('DEBUG')

    error = FileNotFoundError(2, 'missing', PRIVATE)
    with pytest.raises(FileNotFoundError) as raised:
        runner._dispatch_step = Mock(side_effect=error)
        runner.run(
            [{'type': 'synthetic_failure'}],
            variables=[{'name': 'credential', 'value': PRIVATE, 'sensitive': True}],
            runtime={'stop_on_error': True},
        )

    assert raised.value is error
    assert error.filename != PRIVATE
    assert PRIVATE not in str(error)
    assert PRIVATE not in repr(error)
    assert len(captured) == 1
    captured_error, message, representation, formatted_chain = captured[0]
    assert captured_error is error
    assert PRIVATE not in message
    assert PRIVATE not in representation
    assert PRIVATE not in formatted_chain
    assert PRIVATE not in caplog.text
    assert '[REDACTED]' in message


def test_oserror_filename2_is_redacted(tmp_path):
    runner = make_runner(tmp_path)
    runner._init_context(variables=[
        {'name': 'credential', 'value': PRIVATE, 'sensitive': True},
    ])
    error = FileNotFoundError(2, 'missing', 'public-path', None, PRIVATE)

    runner._sanitize_exception_for_reporting(error)

    assert error.filename == 'public-path'
    assert error.filename2 != PRIVATE
    assert PRIVATE not in str(error)
    assert PRIVATE not in repr(error)


def test_custom_exception_string_and_repr_attributes_are_sanitized(tmp_path):
    class AttributeFormattedError(Exception):
        def __init__(self):
            self.detail = PRIVATE
            super().__init__('public diagnostic')

        def __str__(self):
            return f'failure: {self.detail}'

        def __repr__(self):
            return f'AttributeFormattedError(detail={self.detail!r})'

    runner = make_runner(tmp_path)
    runner._init_context(variables=[
        {'name': 'credential', 'value': PRIVATE, 'sensitive': True},
    ])
    error = AttributeFormattedError()

    runner._sanitize_exception_for_reporting(error)

    assert error.detail == '[REDACTED]'
    assert error.args == ('public diagnostic',)
    assert PRIVATE not in str(error)
    assert PRIVATE not in repr(error)


def test_screenshot_filename_redacts_rendered_sensitive_step_name(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    captured_paths = []
    monkeypatch.setattr(
        flow_module,
        'snapshot',
        Mock(side_effect=lambda filename: captured_paths.append(filename)),
    )

    result = runner.run(
        [{'type': 'screenshot', 'name': '{{local.credential}}'}],
        variables=[{'name': 'credential', 'value': PRIVATE, 'sensitive': True}],
    )

    assert result['passed'] == 1
    assert len(captured_paths) == 1
    assert PRIVATE not in captured_paths[0]
    assert PRIVATE not in os.path.basename(captured_paths[0])
    assert '[REDACTED]' not in captured_paths[0]
