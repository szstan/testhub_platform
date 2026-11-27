import pytest

from apps.app_automation.tasks import classify_app_test_result, worker_io_completed_normally


def report_result(**changes):
    result = {
        'success': False,
        'exit_code': 0,
        'error': 'Allure report generation failed after pytest completed',
        'test_results': {'total': 1, 'passed': 1, 'failed': 0},
        'device_io_admitted': True,
        'pytest_process_started': True,
        'pytest_process_completed': True,
    }
    result.update(changes)
    return result


def test_report_generation_failure_is_distinguished_from_process_failure():
    value = report_result()
    status, result, message = classify_app_test_result(value)
    assert (status, result) == ('error', None)
    assert 'Allure 报告生成失败' in message
    assert '异常退出' not in message
    assert worker_io_completed_normally(value) is True


@pytest.mark.parametrize('error', ['private-token=synthetic', '/private/worker/path',
                                   'Allure failure: private-input'])
def test_arbitrary_worker_messages_are_not_exposed_as_report_stage(error):
    status, result, message = classify_app_test_result(report_result(error=error))
    assert (status, result) == ('error', None)
    assert '异常退出' in message
    assert error not in message


def test_a_nonzero_process_exit_keeps_process_failure_classification():
    status, result, message = classify_app_test_result(report_result(exit_code=2))
    assert (status, result) == ('error', None)
    assert '异常退出' in message
    assert worker_io_completed_normally(report_result(exit_code=2)) is False


def test_business_failure_result_is_preserved_when_report_creation_also_fails():
    value = report_result(exit_code=1, test_results={'total': 1, 'passed': 0, 'failed': 1})
    assert classify_app_test_result(value) == ('completed', 'failed', '')
