import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

pytest.importorskip('airtest.core.api', reason='APP worker Airtest dependency is not installed')

from apps.app_automation.executors.test_executor import AppTestExecutor
from apps.app_automation.models import AppDevice, AppProject, AppTestCase, AppTestExecution, AppTestSuite
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner
from apps.app_automation.tasks import classify_app_test_result, execute_app_suite_task, execute_app_test_task


def make_runner():
    runner = UiFlowRunner.__new__(UiFlowRunner)
    runner.runtime = {'retry_interval': 0}
    runner.image_base_dir = 'fixture-images'
    return runner


def test_missing_image_and_existence_assertions_fail_when_airtest_returns_false():
    runner = make_runner()
    with (patch.object(runner, '_resolve_selector', return_value=object()),
          patch('apps.app_automation.runners.ui_flow_runner.exists', return_value=False),
          pytest.raises(AssertionError, match='实际不存在')):
        runner._assert_exists({'expected_exists': True})
    with (patch.object(runner, '_resolve_selector', return_value=object()),
          patch('apps.app_automation.runners.ui_flow_runner.exists', return_value=False)):
        runner._assert_exists({'expected_exists': False})
    with (patch('apps.app_automation.runners.ui_flow_runner.os.path.isfile', return_value=True),
          patch('apps.app_automation.runners.ui_flow_runner.Template', return_value=object()),
          patch('apps.app_automation.runners.ui_flow_runner.exists', return_value=False),
          pytest.raises(AssertionError, match='未在屏幕上找到')):
        runner._assert_image({'expected': 'fixture.png'})


def test_unknown_step_and_unavailable_ocr_cannot_silently_pass():
    runner = make_runner()
    with (patch.object(runner, '_try_execute_as_custom', return_value=False),
          patch('apps.app_automation.runners.ui_flow_runner.ALLURE_AVAILABLE', False),
          pytest.raises(ValueError, match='未知的动作类型')):
        runner.run([{'type': 'unsupported-fixture'}], runtime={'stop_on_error': True})
    with (patch('apps.app_automation.runners.ui_flow_runner.OCR_AVAILABLE', False),
          pytest.raises(RuntimeError, match='需要 OCR')):
        runner._action_foreach_assert({})


def test_configured_missing_input_and_wait_targets_fail_without_typing_or_sleeping():
    runner = make_runner()
    with (patch.object(runner, '_resolve_selector', return_value=None),
          patch('apps.app_automation.runners.ui_flow_runner.airtest_text') as text,
          pytest.raises(ValueError, match='输入步骤无法解析')):
        runner._action_input({'selector': 'missing.png', 'value': 'fixture'})
    text.assert_not_called()
    with (patch.object(runner, '_resolve_selector', return_value=None),
          patch('apps.app_automation.runners.ui_flow_runner.sleep') as sleep,
          pytest.raises(ValueError, match='等待步骤无法解析')):
        runner._action_wait({'selector': 'missing.png', 'timeout': 1})
    sleep.assert_not_called()


def test_unlocated_input_without_selector_remains_supported_and_hides_value(caplog):
    runner = make_runner()
    with (patch.object(runner, '_resolve_selector', return_value=None),
          patch('apps.app_automation.runners.ui_flow_runner.airtest_text') as text,
          patch('apps.core.variable_resolver.resolve_variables', return_value='private-fixture')):
        runner._action_input({'value': 'private-fixture'})
    text.assert_called_once_with('private-fixture')
    assert 'private-fixture' not in caplog.text


def test_runner_counts_only_assertions_that_actually_execute():
    runner = make_runner()
    with patch('apps.app_automation.runners.ui_flow_runner.ALLURE_AVAILABLE', False):
        with patch.object(runner, '_action_wait'):
            without_assertion = runner.run([{'type': 'wait'}])
        with (patch.object(runner, '_action_wait'), patch.object(runner, '_action_assert')):
            with_assertion = runner.run([{'type': 'wait'}, {'type': 'assert'}])
    assert without_assertion['assertions'] == 0
    assert with_assertion['assertions'] == 1


@pytest.mark.parametrize(('report', 'expected'), [
    ({'success': False, 'exit_code': 1, 'test_results': {'total': 1, 'passed': 1, 'failed': 0}}, ('error', None)),
    ({'success': False, 'exit_code': 1, 'test_results': {'total': 1, 'passed': 0, 'failed': 1}}, ('completed', 'failed')),
    ({'success': True, 'exit_code': 0, 'test_results': {'total': 1, 'passed': 0, 'failed': 0, 'skipped': 1, 'unverified': 1}}, ('completed', 'unverified')),
    ({'success': True, 'exit_code': 0, 'test_results': {'total': 1, 'passed': 0, 'failed': 0, 'skipped': 1}}, ('completed', 'skipped')),
    ({'success': True, 'exit_code': 0, 'test_results': {'total': 1, 'passed': 1, 'failed': 0}}, ('completed', 'passed')),
    ({'success': True, 'exit_code': 0, 'test_results': {'total': 0}}, ('error', None)),
])
def test_result_classification_respects_exit_and_verification(report, expected):
    assert classify_app_test_result(report)[:2] == expected


def test_allure_parser_recognizes_unverified_and_malformed_results(tmp_path):
    executor = AppTestExecutor(base_path=str(tmp_path))
    (tmp_path / 'one-result.json').write_text(json.dumps({
        'status': 'skipped', 'labels': [{'name': 'testhub_verification', 'value': 'unverified'}],
    }), encoding='utf-8')
    result = executor._parse_allure_results(str(tmp_path))
    assert (result['total'], result['skipped'], result['unverified']) == (1, 1, 1)
    (tmp_path / 'broken-result.json').write_text('{bad json', encoding='utf-8')
    result = executor._parse_allure_results(str(tmp_path))
    assert result['total'] == 2 and result['failed'] == 1


def test_worker_entry_marks_no_assertion_unverified_without_real_device():
    from apps.app_automation.tests import test_app_flow

    case = SimpleNamespace(name='fixture', ui_flow=[{'type': 'wait'}], variables=[])
    with (patch.object(test_app_flow.AppTestCase.objects, 'get', return_value=case),
          patch.object(test_app_flow, 'UiFlowRunner') as runner,
          patch.object(test_app_flow.allure.dynamic, 'label') as label,
          patch.object(test_app_flow.allure, 'attach')):
        runner.return_value.run.return_value = {'total': 1, 'passed': 1, 'failed': 0, 'assertions': 0}
        with pytest.raises(pytest.skip.Exception):
            test_app_flow.TestAppFlow().test_execute_ui_flow(1, '', 1, object(), 'fixture')
    label.assert_any_call('testhub_verification', 'unverified')


def test_worker_entry_keeps_verified_assertion_as_passed():
    from apps.app_automation.tests import test_app_flow

    case = SimpleNamespace(name='fixture', ui_flow=[{'type': 'assert'}], variables=[])
    with (patch.object(test_app_flow.AppTestCase.objects, 'get', return_value=case),
          patch.object(test_app_flow, 'UiFlowRunner') as runner,
          patch.object(test_app_flow.allure.dynamic, 'label') as label,
          patch.object(test_app_flow.allure, 'attach')):
        runner.return_value.run.return_value = {'total': 1, 'passed': 1, 'failed': 0, 'assertions': 1}
        test_app_flow.TestAppFlow().test_execute_ui_flow(1, '', 1, object(), 'fixture')
    label.assert_any_call('testhub_verification', 'verified')


@pytest.mark.django_db
def test_worker_persists_unverified_and_nonzero_exit_as_distinct_results():
    owner = get_user_model().objects.create_user(username='verdict-fixture')
    project = AppProject.objects.create(name='verdict fixture', owner=owner)
    case = AppTestCase.objects.create(name='fixture', project=project, created_by=owner)
    device = AppDevice.objects.create(device_id='verdict-fixture', status='online')
    device.lock(owner)
    reports = [
        {'success': True, 'exit_code': 0, 'test_results': {'total': 1, 'passed': 0, 'failed': 0, 'skipped': 1, 'unverified': 1}},
        {'success': False, 'exit_code': 1, 'test_results': {'total': 1, 'passed': 1, 'failed': 0}},
    ]
    results = []
    for report in reports:
        execution = AppTestExecution.objects.create(test_case=case, device=device, user=owner,
                                                     device_lease_generation=device.lease_generation)
        with (patch('apps.app_automation.device_pool.Thread'),
              patch('apps.app_automation.tasks.send_execution_update'),
              patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor):
            executor.return_value.run_tests.return_value = report
            execute_app_test_task.run(execution.pk, lease_generation=device.lease_generation,
                                      release_device_lease=False)
        execution.refresh_from_db()
        results.append((execution.status, execution.result))
    assert results == [('completed', 'unverified'), ('error', None)]


@pytest.mark.django_db
def test_suite_with_unverified_case_is_not_reported_as_passed():
    owner = get_user_model().objects.create_user(username='suite-verdict-fixture')
    project = AppProject.objects.create(name='suite verdict', owner=owner)
    first = AppTestCase.objects.create(name='one', project=project, created_by=owner)
    second = AppTestCase.objects.create(name='two', project=project, created_by=owner)
    suite = AppTestSuite.objects.create(name='suite', project=project, created_by=owner)
    device = AppDevice.objects.create(device_id='suite-verdict-fixture', status='online')
    device.lock(owner)
    rows = [AppTestExecution.objects.create(test_case=case, test_suite=suite, device=device, user=owner,
                                           device_lease_generation=device.lease_generation) for case in (first, second)]
    reports = [
        {'success': True, 'test_results': {'total': 1, 'passed': 1, 'failed': 0}},
        {'success': True, 'test_results': {'total': 1, 'passed': 0, 'failed': 0, 'skipped': 1, 'unverified': 1}},
    ]
    with (patch('apps.app_automation.device_pool.Thread'),
          patch('apps.app_automation.tasks.send_execution_update'),
          patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor):
        executor.return_value.run_tests.side_effect = reports
        execute_app_suite_task.run(suite.pk, [row.pk for row in rows],
                                   lease_generation=device.lease_generation, release_device_lease=False)
    suite.refresh_from_db()
    assert suite.execution_result == 'unverified'
    assert suite.passed_count == 1 and suite.failed_count == 0


@pytest.mark.django_db
def test_suite_assertion_failure_clears_completed_worker_marker():
    owner = get_user_model().objects.create_user(username='suite-assertion-owner')
    project = AppProject.objects.create(name='suite assertion project', owner=owner)
    case = AppTestCase.objects.create(name='assertion case', project=project, created_by=owner)
    suite = AppTestSuite.objects.create(name='assertion suite', project=project, created_by=owner)
    device = AppDevice.objects.create(device_id='suite-assertion-device', status='online')
    device.lock(owner)
    execution = AppTestExecution.objects.create(
        test_case=case,
        test_suite=suite,
        device=device,
        user=owner,
        device_lease_generation=device.lease_generation,
    )

    def failed_assertion(*args, before_device_io, **kwargs):
        from apps.app_automation.execution_admission import clear_device_action_pending

        before_device_io()
        clear_device_action_pending(execution.pk)
        return {
            'success': False,
            'exit_code': 1,
            'report_path': 'synthetic-report',
            'test_results': {
                'total': 1, 'passed': 0, 'failed': 1, 'broken': 0,
                'skipped': 0, 'unverified': 0,
            },
        }

    with (
        patch('apps.app_automation.device_pool.Thread'),
        patch('apps.app_automation.tasks.send_execution_update'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor,
    ):
        executor.return_value.run_tests.side_effect = failed_assertion
        execute_app_suite_task.run(
            suite.pk,
            [execution.pk],
            lease_generation=device.lease_generation,
            release_device_lease=False,
        )

    execution.refresh_from_db()
    suite.refresh_from_db()
    assert execution.status == 'completed'
    assert execution.result == 'failed'
    assert execution.device_action_pending is False
    assert suite.execution_result == 'failed'
    assert suite.failed_count == 1
