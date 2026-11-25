import logging
import os
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest
from django.test import override_settings

pytest.importorskip('airtest.core.api', reason='Airtest is required to import the runner')

from apps.app_automation.runners import ui_flow_runner as flow_module
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner
from apps.app_automation.serializers import AppTestCaseSerializer


def make_runner(tmp_path):
    runner = UiFlowRunner.__new__(UiFlowRunner)
    runner.image_base_dir = str(tmp_path / 'images')
    runner.screenshots_dir = str(tmp_path / 'screenshots')
    runner.context = {'local': {}, 'global': {}, 'outputs': {}, 'steps': {}}
    runner.runtime = {'retry_times': 0, 'retry_interval': 0}
    runner._ocr_helper = None
    runner._assertions_executed = 0
    return runner


def test_every_legacy_component_type_dispatches_to_a_runner_handler(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    methods = {
        'click': '_action_click', 'input': '_action_input', 'swipe': '_action_swipe',
        'api_request': '_action_api_request', 'image_exists_click': '_action_image_exists_click',
        'image_exists_click_chain': '_action_image_exists_click_chain',
        'long_press': '_action_long_press', 'double_click': '_action_double_click',
        'drag': '_action_drag', 'swipe_to': '_action_swipe_to', 'assert': '_action_assert',
        'foreach_assert': '_action_foreach_assert', 'wait': '_action_wait', 'if': '_action_if',
        'loop': '_action_loop', 'sequence': '_action_sequence', 'try': '_action_try',
        'set_variable': '_action_set_variable', 'unset_variable': '_action_unset_variable',
        'extract_output': '_action_extract_output', 'screenshot': '_action_screenshot',
    }
    dispatched = []
    for action, method in methods.items():
        monkeypatch.setattr(runner, method, lambda _step, action=action: dispatched.append(action))
    monkeypatch.setattr(runner, '_try_execute_as_custom', lambda *_args: False)

    for action in methods:
        runner._dispatch_step({'type': action}, action)

    assert set(dispatched) == set(methods)


def test_scoped_variables_traverse_nested_dicts_and_lists_and_preserve_falsy_values(tmp_path):
    runner = make_runner(tmp_path)
    runner._init_context([
        {'name': 'profile', 'scope': 'local', 'value': {'items': [{'enabled': False, 'count': 0}]}},
        {'name': 'settings', 'scope': 'global', 'value': {'labels': ['first', 'second']}},
        {'name': 'empty', 'scope': 'local', 'value': ''},
    ], runtime={'retry_times': 0})
    runner.context['outputs']['last'] = {'body': {'items': [{'value': 0}]}}
    runner.context['steps']['signup'] = {'response': {'roles': ['reader', 'admin']}}

    assert runner._render_value('{{local.profile.items[0].enabled}}') is False
    assert runner._render_value('{{profile.items[0].enabled}}') is False
    assert runner._render_value('{{local.profile.items[0].count}}') == 0
    assert runner._render_value('{{local.empty}}') == ''
    assert runner._render_value('{{global.settings.labels[1]}}') == 'second'
    assert runner._render_value('{{outputs.last.body.items[0].value}}') == 0
    assert runner._render_value('{{steps.signup.response.roles[1]}}') == 'admin'
    assert runner._render_value('count={{local.profile.items[0].count}}') == 'count=0'
    assert runner._render_value('${global.settings.labels[0]}') == 'first'


def test_unresolved_variable_references_fail_with_the_reference_name(tmp_path):
    runner = make_runner(tmp_path)
    runner._init_context(runtime={'retry_times': 0})

    with pytest.raises(ValueError, match=r'local\.missing'):
        runner._render_value('{{local.missing}}')


def test_api_response_is_redacted_in_outputs_without_sensitive_logs(tmp_path, caplog):
    runner = make_runner(tmp_path)
    body = {'account': {'token': 'body-private-token', 'active': False}}
    response = SimpleNamespace(
        status_code=200,
        headers={'Content-Type': 'application/json', 'Set-Cookie': 'cookie-private-value'},
        json=lambda: body,
        text='',
        content=b'',
    )
    url = 'https://user:url-private-password@example.invalid/login?key=url-private-token'
    step = {
        'id': 'login-step',
        'type': 'api_request',
        'method': 'GET',
        'url': url,
        'response_type': 'json',
        'extracts': [{'path': 'body.account.token', 'name': 'token'}],
    }

    caplog.set_level(logging.INFO, logger=flow_module.__name__)
    with (
        patch.object(flow_module, 'validate_outbound_url', return_value=url),
        patch.object(flow_module, 'guarded_request', return_value=response) as request,
    ):
        result = runner.run([step], runtime={'retry_times': 0})

    request.assert_called_once()
    assert result['outputs']['last']['body'] == {
        'account': {'token': '[REDACTED]', 'active': False},
    }
    assert result['outputs']['last']['headers']['Set-Cookie'] == '[REDACTED]'
    assert runner.context['steps']['login-step']['body'] == body
    assert runner.context['local']['token'] == 'body-private-token'
    assert runner._render_value('{{outputs.last.body.account.active}}') is False
    assert runner._render_value('{{steps.login-step.body.account.token}}') == 'body-private-token'
    for secret in ('url-private-password', 'url-private-token', 'body-private-token', 'cookie-private-value'):
        assert secret not in caplog.text


@pytest.mark.parametrize(('left', 'operator', 'right', 'expected'), [
    ('a', '==', 'a', True), ('a', '!=', 'b', True),
    ('3', '>', '2', True), ('3', '>=', '3', True),
    ('2', '<', '3', True), ('2', '<=', '2', True),
    ('a', 'in', 'cat', True), ('z', 'not in', 'cat', True),
    ('z', 'not_in', 'cat', True), ('alphabet', 'contains', 'pha', True),
    ('alphabet', 'notcontains', 'xyz', True), ('alphabet', 'not_contains', 'xyz', True),
    ('abc-123', 'regex', r'\d+', True), ('abc-123', 'match', r'abc', True),
    ('value', 'truthy', '', True), ('value', 'exists', '', True),
    ('', 'falsy', '', True), ('', 'not_exists', '', True),
    ('prefix-value', 'startswith', 'prefix', True),
    ('prefix-value', 'endswith', 'value', True),
])
def test_condition_evaluator_supports_documented_operators(left, operator, right, expected):
    assert UiFlowRunner._eval_condition(left, operator, right) is expected


def test_condition_loops_use_the_shared_operator_evaluator_and_recheck_variables(tmp_path):
    runner = make_runner(tmp_path)
    result = runner.run(
        [{
            'type': 'loop',
            'mode': 'condition',
            'left': '{{local.enabled}}',
            'operator': 'truthy',
            'right': '',
            'max_loops': 5,
            'steps': [{'type': 'set_variable', 'name': 'enabled', 'value': False}],
        }],
        variables=[{'name': 'enabled', 'value': True}],
        runtime={'retry_times': 0},
    )

    assert result['passed'] == 1
    assert runner.context['local']['enabled'] is False


def test_foreach_loop_renders_body_after_assigning_each_item(tmp_path):
    runner = make_runner(tmp_path)
    runner.run(
        [{
            'type': 'loop',
            'mode': 'foreach',
            'items': ['first', 'second'],
            'item_var': 'current_item',
            'steps': [{
                'type': 'set_variable',
                'name': 'last_item',
                'value': '{{local.current_item}}',
            }],
        }],
        runtime={'retry_times': 0, 'stop_on_error': True},
    )

    assert runner.context['local']['last_item'] == 'second'


def test_unsupported_condition_operator_is_rejected(tmp_path):
    runner = make_runner(tmp_path)
    with pytest.raises(ValueError, match='Unsupported condition operator'):
        runner._eval_condition('a', 'approx', 'b')


def test_directional_and_legacy_coordinate_swipes_keep_their_contract(tmp_path):
    runner = make_runner(tmp_path)
    fake_global = SimpleNamespace(DEVICE=SimpleNamespace(get_current_resolution=lambda: (1000, 2000)))
    with (
        patch.object(flow_module, 'G', fake_global),
        patch.object(flow_module, 'swipe') as swipe,
    ):
        runner._action_swipe({'direction': 'down', 'duration': 1.25})
        runner._action_swipe({'start': '0,0', 'end': '40,60', 'duration': 0.8})
        runner._action_swipe({
            'selector_type': 'pos', 'selector': '[[10, 20], [30, 40]]', 'duration': 0.4,
        })

    assert swipe.call_args_list == [
        call((500, 600), (500, 1400), duration=1.25),
        call((0, 0), (40, 60), duration=0.8),
        call((10, 20), (30, 40), duration=0.4),
    ]


def test_swipe_to_checks_for_target_after_the_last_legacy_swipe(tmp_path):
    runner = make_runner(tmp_path)
    fake_global = SimpleNamespace(DEVICE=SimpleNamespace(get_current_resolution=lambda: (1000, 2000)))
    with (
        patch.object(flow_module, 'G', fake_global),
        patch.object(runner, '_resolve_selector', return_value='target'),
        patch.object(runner, '_resolve_action_target', side_effect=[None, 'target']),
        patch.object(flow_module, 'swipe') as swipe,
        patch.object(flow_module.time, 'sleep'),
    ):
        runner._action_swipe_to({
            'target_selector': 'target.png', 'max_swipes': 1, 'interval': 0,
        })
    swipe.assert_called_once()


def test_image_exists_click_chain_passes_both_thresholds_and_rejects_missing_target(tmp_path):
    runner = make_runner(tmp_path)
    main_target, fallback_target = object(), object()
    with (
        patch.object(runner, '_resolve_selector', side_effect=[main_target, fallback_target]) as resolve,
        patch.object(flow_module, 'touch') as touch,
        patch.object(flow_module.time, 'sleep'),
    ):
        runner._action_image_exists_click_chain({
            'selector': 'main.png', 'image_threshold': 0.82,
            'fallback_selector': 'fallback.png', 'fallback_image_threshold': 0.93,
        })

    assert resolve.call_args_list[0].args[0]['image_threshold'] == 0.82
    assert resolve.call_args_list[1].args[0]['image_threshold'] == 0.93
    assert touch.call_args_list == [call(main_target), call(fallback_target)]

    with (
        patch.object(runner, '_resolve_selector', side_effect=[main_target, None]),
        patch.object(flow_module, 'touch') as touch,
        pytest.raises(ValueError, match='requires both'),
    ):
        runner._action_image_exists_click_chain({'selector': 'main.png', 'fallback_selector': ''})
    touch.assert_not_called()


def test_runner_passes_configured_ocr_language_and_gpu_to_helper(tmp_path):
    runner = make_runner(tmp_path)
    runner.runtime = {'ocr_languages': ['ch_sim', 'en'], 'ocr_use_gpu': True}
    with patch.object(flow_module, 'get_ocr_helper', return_value='configured-helper') as get_helper:
        assert runner._get_ocr_helper() == 'configured-helper'
    get_helper.assert_called_once_with(languages=['ch_sim', 'en'], use_gpu=True)


def test_foreach_assert_image_mode_uses_templates_without_ocr_and_fails_on_no_match(tmp_path):
    runner = make_runner(tmp_path)
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', False),
        patch.object(runner, '_resolve_selector', return_value=(0, 0)),
        patch.object(flow_module.os.path, 'isfile', return_value=True),
        patch.object(flow_module.os.path, 'getsize', return_value=1),
        patch.object(flow_module, 'Template', return_value='expected-template') as template,
        patch.object(runner, '_image_lookup_once', return_value=(4, 5)),
        patch.object(flow_module, 'touch') as touch,
        patch.object(runner, '_get_ocr_helper') as get_ocr,
    ):
        runner._action_foreach_assert({
            'config': {
                'assert_type': 'image', 'expected_list': ['expected.png'],
                'expected_image_scope': 'common', 'image_threshold': 0.91,
                'click_selector_type': 'pos', 'click_selector': '0,0',
                'max_loops': 1, 'interval': 0, 'timeout': 3,
            },
        })

    template.assert_called_once_with(
        str(tmp_path / 'images' / 'common' / 'expected.png'), threshold=0.91,
    )
    touch.assert_called_once_with((0, 0))
    get_ocr.assert_not_called()

    with (
        patch.object(flow_module, 'OCR_AVAILABLE', False),
        patch.object(runner, '_resolve_selector', return_value=(1, 1)),
        patch.object(flow_module.os.path, 'isfile', return_value=True),
        patch.object(flow_module.os.path, 'getsize', return_value=1),
        patch.object(flow_module, 'Template', return_value='expected-template'),
        patch.object(runner, '_image_lookup_once', return_value=None),
        patch.object(flow_module, 'touch'),
        pytest.raises(AssertionError, match='got 0'),
    ):
        runner._action_foreach_assert({
            'assert_type': 'image', 'expected_list': ['expected.png'],
            'click_selector_type': 'pos', 'click_selector': '1,1',
            'max_loops': 1, 'interval': 0, 'timeout': 3,
        })


def test_foreach_assert_caps_waits_at_the_configured_timeout(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    now = [0.0]
    waits = []

    def fake_sleep(duration):
        waits.append(duration)
        now[0] += duration

    monkeypatch.setattr(flow_module.time, 'monotonic', lambda: now[0])
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', False),
        patch.object(runner, '_resolve_selector', return_value=(1, 1)),
        patch.object(flow_module.os.path, 'isfile', return_value=True),
        patch.object(flow_module.os.path, 'getsize', return_value=1),
        patch.object(flow_module, 'Template', return_value='expected-template'),
        patch.object(runner, '_image_lookup_once', return_value=(4, 5)),
        patch.object(flow_module, 'touch') as touch,
        patch.object(flow_module.time, 'sleep', side_effect=fake_sleep),
    ):
        runner._action_foreach_assert({
            'assert_type': 'image', 'expected_list': ['expected.png'],
            'click_selector_type': 'pos', 'click_selector': '1,1',
            'max_loops': 5, 'interval': 0.2, 'timeout': 0.25,
        })

    assert waits == [0.2]
    touch.assert_called_once()


def test_foreach_assert_preserves_zero_timeout_and_zero_min_match_legacy_values(tmp_path):
    runner = make_runner(tmp_path)
    with (
        patch.object(flow_module, 'OCR_AVAILABLE', False),
        patch.object(runner, '_resolve_selector', return_value=(1, 1)),
        patch.object(flow_module.os.path, 'isfile', return_value=True),
        patch.object(flow_module.os.path, 'getsize', return_value=1),
        patch.object(flow_module, 'Template', return_value='expected-template'),
        patch.object(runner, '_image_lookup_once', return_value=(4, 5)),
        patch.object(flow_module, 'touch') as touch,
    ):
        runner._action_foreach_assert({
            'assert_type': 'image', 'expected_list': ['expected.png'],
            'click_selector_type': 'pos', 'click_selector': '1,1',
            'max_loops': 2, 'min_match': 0, 'interval': 0, 'timeout': 0,
        })
    assert touch.call_count == 2


def test_screenshot_name_cannot_escape_its_output_directory(tmp_path):
    runner = make_runner(tmp_path)
    with patch.object(flow_module, 'snapshot') as snapshot:
        runner._action_snapshot({'name': '../../outside\u005csecret'})

    path = snapshot.call_args.kwargs['filename']
    assert flow_module.os.path.commonpath((runner.screenshots_dir, path)) == runner.screenshots_dir
    assert '..' not in flow_module.os.path.basename(path)
    assert flow_module.os.path.basename(path).endswith('.png')
    with pytest.raises(ValueError, match='inside the configured image directory'):
        runner._safe_image_path('common', '../../outside.png')


def test_screenshot_username_cannot_escape_media_screenshot_root(tmp_path):
    media_root = tmp_path / 'media'
    with override_settings(MEDIA_ROOT=str(media_root)):
        runner = UiFlowRunner(
            image_base_dir=str(tmp_path / 'images'), username='../../outside',
        )
        other = UiFlowRunner(
            image_base_dir=str(tmp_path / 'images'), username='另一个用户',
        )
    screenshots_root = os.path.realpath(media_root / 'app-automation' / 'screenshots')
    assert os.path.commonpath((screenshots_root, runner.screenshots_dir)) == screenshots_root
    assert runner.screenshots_dir != other.screenshots_dir


def test_global_retries_are_bounded_and_step_retry_count_overrides_default(tmp_path):
    runner = make_runner(tmp_path)
    runner._init_context(runtime={'retry_times': 2, 'retry_interval': 0})
    with (
        patch.object(runner, '_dispatch_step', side_effect=[RuntimeError('first'), RuntimeError('second'), 'done']) as dispatch,
        patch.object(flow_module, 'sleep'),
    ):
        runner._execute_step({'id': 'retried', 'type': 'fixture'})
    assert dispatch.call_count == 3
    assert runner.context['steps']['retried'] == 'done'

    runner._init_context(runtime={'retry_times': 2, 'retry_interval': 0})
    with patch.object(runner, '_dispatch_step', side_effect=RuntimeError('once')) as dispatch:
        with pytest.raises(RuntimeError, match='once'):
            runner._execute_step({'type': 'fixture', 'retry_times': 0})
    dispatch.assert_called_once()

    with pytest.raises(ValueError, match='retry_times'):
        runner.run([], runtime={'retry_times': 11})
    with pytest.raises(ValueError, match='ocr_languages'):
        runner.run([], runtime={'ocr_languages': ['xx']})
    with pytest.raises(ValueError, match='unsupported options'):
        runner.run([], runtime={'unknown_option': 'value'})


@pytest.mark.django_db
def test_runtime_config_serializer_validates_bounds_and_accepts_legacy_payload():
    valid = AppTestCaseSerializer(data={
        'name': 'runtime config case',
        'runtime_config': {
            'base_url': 'https://example.invalid/api',
            'retry_times': 10,
            'retry_interval': 0,
            'ocr_languages': ['ch_sim', 'en'],
            'ocr_use_gpu': False,
        },
    })
    assert valid.is_valid(), valid.errors
    case = valid.save()
    assert case.runtime_config['ocr_languages'] == ['ch_sim', 'en']
    assert case.runtime_config['base_url'] == 'https://example.invalid/api'

    invalid = AppTestCaseSerializer(data={
        'name': 'invalid runtime config case',
        'runtime_config': {'retry_times': 11},
    })
    assert not invalid.is_valid()
    assert 'runtime_config' in invalid.errors

    unsupported = AppTestCaseSerializer(data={
        'name': 'unsupported runtime config case',
        'runtime_config': {'unknown_option': 'value'},
    })
    assert not unsupported.is_valid()
    assert 'runtime_config' in unsupported.errors

    huge_interval = AppTestCaseSerializer(data={
        'name': 'oversized retry interval case',
        'runtime_config': {'retry_interval': 10 ** 1000},
    })
    assert not huge_interval.is_valid()

    legacy = AppTestCaseSerializer(data={'name': 'legacy case'})
    assert legacy.is_valid(), legacy.errors
    legacy_case = legacy.save()
    assert legacy_case.runtime_config == {}


def test_worker_uses_persisted_runtime_but_forces_stop_on_error():
    from apps.app_automation.tests import test_app_flow

    case = SimpleNamespace(
        name='runtime fixture',
        ui_flow=[{'type': 'wait'}],
        variables=[],
        runtime_config={'retry_times': 1, 'retry_interval': 0, 'stop_on_error': False},
    )
    with (
        patch.object(test_app_flow.AppTestCase.objects, 'get', return_value=case),
        patch.object(test_app_flow, 'UiFlowRunner') as runner,
        patch.object(test_app_flow.allure.dynamic, 'title'),
        patch.object(test_app_flow.allure.dynamic, 'suite'),
        patch.object(test_app_flow.allure.dynamic, 'label'),
        patch.object(test_app_flow.allure, 'attach'),
    ):
        runner.return_value.run.return_value = {
            'total': 1, 'passed': 1, 'failed': 0, 'assertions': 0,
        }
        with pytest.raises(pytest.skip.Exception):
            test_app_flow.TestAppFlow().test_execute_ui_flow(
                1, '', 1, object(), 'fixture-user',
            )

    assert runner.return_value.run.call_args.kwargs['runtime'] == {
        'retry_times': 1,
        'retry_interval': 0,
        'stop_on_error': True,
    }


def test_inactive_branch_and_loop_children_render_only_when_executed(tmp_path):
    runner = make_runner(tmp_path)
    result = runner.run([
        {'type': 'if', 'config': {
            'left': 'yes', 'operator': '==', 'right': 'yes',
            'then_steps': [{'type': 'set_variable', 'name': 'Display name',
                            'config': {'name': 'chosen', 'value': 'ok'}}],
            'else_steps': [{'type': 'set_variable', 'config': {
                'name': 'unused', 'value': '{{local.does_not_exist}}',
            }}],
        }},
    ], runtime={'retry_times': 0, 'stop_on_error': True})
    assert result['passed'] == 1
    assert runner.context['local']['chosen'] == 'ok'
    assert 'Display name' not in runner.context['local']


def test_runtime_base_url_supplies_variable_and_relative_http_request(tmp_path):
    runner = make_runner(tmp_path)
    runner._init_context(runtime={'base_url': 'https://service.example.test/api'})
    assert runner._render_value('{{global.API_BASE_URL}}') == 'https://service.example.test/api'
    response = SimpleNamespace(status_code=200, headers={'Content-Type': 'text/plain'}, text='ok')
    with (
        patch.object(flow_module, 'validate_outbound_url', side_effect=lambda url, **kwargs: url),
        patch.object(flow_module, 'guarded_request', return_value=response) as request,
    ):
        runner._action_api_request({'url': 'users'})
    assert request.call_args.kwargs['url'] == 'https://service.example.test/api/users'


@pytest.mark.parametrize('base_url', ['http://user:secret@example.test', 'file:///tmp/x',
                                      'https://example.test/?token=secret', 'http://bad:99999'])
def test_runtime_base_url_rejects_credentials_and_invalid_syntax(base_url):
    serializer = AppTestCaseSerializer(data={
        'name': 'bad url', 'runtime_config': {'base_url': base_url},
    })
    assert not serializer.is_valid()
    assert 'secret' not in str(serializer.errors)


def test_sensitive_input_failure_does_not_capture_automatic_screenshot(tmp_path):
    runner = make_runner(tmp_path)
    with (
        patch.object(flow_module, 'ALLURE_AVAILABLE', True),
        patch.object(flow_module.allure, 'attach') as attach,
        patch.object(runner, '_capture_screenshot') as screenshot,
    ):
        runner._attach_allure('error', {'type': 'input', 'config': {
            'sensitive': True, 'value': 'private-fixture-value',
        }})
    screenshot.assert_not_called()
    assert 'private-fixture-value' not in str(attach.call_args)


def test_regex_timeout_is_bounded_and_sanitized():
    with patch.object(flow_module.bounded_regex, 'search', side_effect=TimeoutError('private text')):
        with pytest.raises(ValueError, match='time limit') as error:
            UiFlowRunner._eval_condition('private text', 'regex', '(a+)+')
    assert 'private text' not in str(error.value)
