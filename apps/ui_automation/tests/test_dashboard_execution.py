from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock
import json
import threading

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from selenium.common.exceptions import WebDriverException

from apps.ui_automation import test_executor, views
from apps.ui_automation.execution_summary import INTERNAL_CASE_EXECUTION_ERROR, INTERNAL_SUITE_EXECUTION_ERROR
from apps.ui_automation.models import (
    TestCase as UiTestCase,
    TestCaseExecution as UiTestCaseExecution,
    TestCaseStep as UiTestCaseStep,
    TestExecution as UiTestExecution,
    TestSuite as UiTestSuite,
    TestSuiteTestCase as UiTestSuiteTestCase,
    UiProject,
)

User = get_user_model()


def _user_project(name, *, owner=None):
    owner = owner or User.objects.create_user(username=f'{name}-owner')
    project = UiProject.objects.create(
        name=name,
        base_url=f'https://{name}.example.test',
        owner=owner,
    )
    return owner, project


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_created_project_returns_the_id_needed_to_continue_setup(monkeypatch):
    user = User.objects.create_user(username='guided-project-owner')
    monkeypatch.setattr('apps.ui_automation.serializers.UiProjectCreateSerializer.validate_base_url', lambda _self, value: value)
    response = _client_for(user).post('/api/ui-automation/projects/', {
        'name': 'First browser test',
        'base_url': 'https://example.test/',
        'owner': user.pk,
    }, format='json')

    assert response.status_code == 201
    assert UiProject.objects.get(pk=response.data['id']).owner_id == user.pk


@pytest.mark.django_db
def test_dashboard_stats_scopes_selected_project_and_preserves_suite_count():
    user, project = _user_project('dashboard-own')
    member_project = UiProject.objects.create(
        name='dashboard-member',
        base_url='https://dashboard-member.example.test',
        owner=User.objects.create_user(username='dashboard-member-owner'),
    )
    member_project.members.add(user)
    outsider, private_project = _user_project('dashboard-private')

    own_cases = [
        UiTestCase.objects.create(project=project, name=f'own-case-{index}', created_by=user)
        for index in range(2)
    ]
    own_suite = UiTestSuite.objects.create(project=project, name='own-suite')
    for case in own_cases:
        UiTestSuiteTestCase.objects.create(test_suite=own_suite, test_case=case)

    member_case = UiTestCase.objects.create(project=member_project, name='member-case', created_by=user)
    member_suite = UiTestSuite.objects.create(project=member_project, name='member-suite')
    UiTestSuiteTestCase.objects.create(test_suite=member_suite, test_case=member_case)
    private_case = UiTestCase.objects.create(project=private_project, name='private-case', created_by=outsider)
    private_suite = UiTestSuite.objects.create(project=private_project, name='private-suite')
    UiTestSuiteTestCase.objects.create(test_suite=private_suite, test_case=private_case)

    client = _client_for(user)
    selected = client.get('/api/ui-automation/dashboard/stats/', {'project': project.pk})
    assert selected.status_code == 200
    assert selected.data['project_count'] == 1
    assert selected.data['test_case_count'] == 2
    assert selected.data['suite_count'] == 2
    assert selected.data['suite_entity_count'] == 1

    all_accessible = client.get('/api/ui-automation/dashboard/stats/')
    assert all_accessible.status_code == 200
    assert all_accessible.data['project_count'] == 2
    assert all_accessible.data['suite_count'] == 3
    assert all_accessible.data['suite_entity_count'] == 2

    denied = client.get('/api/ui-automation/dashboard/stats/', {'project': private_project.pk})
    assert denied.status_code == 404


@pytest.mark.django_db
def test_recent_failed_executions_merges_latest_cases_and_suites_without_child_duplicates():
    user, project = _user_project('recent-own')
    outsider, private_project = _user_project('recent-private')
    suite = UiTestSuite.objects.create(project=project, name='checkout-suite')
    standalone_case = UiTestCase.objects.create(project=project, name='standalone-case', created_by=user)
    suite_case = UiTestCase.objects.create(project=project, name='suite-child-case', created_by=user)
    UiTestSuiteTestCase.objects.create(test_suite=suite, test_case=suite_case)
    now = timezone.now()

    suite_failure = UiTestExecution.objects.create(
        project=project,
        test_suite=suite,
        status='FAILED',
        result_data={
            'test_cases': [{
                'success': True,
                'steps': [{'step_number': 1, 'success': True}],
            }, {
                'status': 'failed',
                'error': 'Checkout failed at step 4',
                'steps': [
                    {'step_number': 1, 'success': True},
                    {'step_number': 4, 'success': False, 'error': 'assertion mismatch'},
                ],
                'screenshots': [{'url': 'data:image/png;base64,' + 'X' * 20000}],
            }],
        },
    )
    UiTestExecution.objects.filter(pk=suite_failure.pk).update(created_at=now - timedelta(seconds=1))
    suite_error = UiTestExecution.objects.create(
        project=project,
        test_suite=suite,
        status='ERROR',
        error_message='suite worker error',
    )
    UiTestExecution.objects.filter(pk=suite_error.pk).update(created_at=now - timedelta(seconds=4))

    def add_case_execution(*, status, seconds, logs='', error_message='', test_suite=None, case=standalone_case):
        execution = UiTestCaseExecution.objects.create(
            test_case=case,
            project=project,
            test_suite=test_suite,
            execution_source='suite' if test_suite else 'manual',
            status=status,
            execution_logs=logs,
            error_message=error_message,
            created_by=user,
        )
        UiTestCaseExecution.objects.filter(pk=execution.pk).update(
            created_at=now - timedelta(seconds=seconds),
        )
        return execution

    case_failure = add_case_execution(
        status='failed',
        seconds=2,
        logs='[{"step_number": 1, "success": true, "error": null}, '
             '{"step_number": 2, "success": false, "error": "Assertion failed"}]',
    )
    case_error = add_case_execution(
        status='error',
        seconds=3,
        logs='Browser worker crashed at step 3',
    )
    add_case_execution(status='FAILED', seconds=5, error_message='older failure')
    add_case_execution(status='error', seconds=6, error_message='old error')
    add_case_execution(
        status='failed',
        seconds=0,
        test_suite=suite,
        case=suite_case,
        error_message='suite child duplicate',
    )

    foreign_case = UiTestCase.objects.create(project=private_project, name='foreign-case', created_by=outsider)
    foreign = UiTestCaseExecution.objects.create(
        test_case=foreign_case,
        project=private_project,
        status='failed',
        error_message='must not leak',
        created_by=outsider,
    )
    UiTestCaseExecution.objects.filter(pk=foreign.pk).update(created_at=now)

    client = _client_for(user)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(
            '/api/ui-automation/dashboard/recent-executions/',
            {'project': project.pk, 'status': 'failed'},
        )
    assert response.status_code == 200
    sql = ' '.join(query['sql'].lower() for query in queries)
    assert 'failure_summary' in sql
    assert all(column not in sql for column in ('result_data', 'execution_logs', 'screenshots', 'error_message'))
    results = response.data['results']
    assert len(results) == 5
    assert {item['kind'] for item in results} == {'case', 'suite'}
    assert all(item['status'] in {'failed', 'error'} for item in results)
    assert all(set(item) == {
        'id', 'kind', 'name', 'status', 'created_at', 'failure_message', 'failed_step_number'
    } for item in results)
    assert not any('child' in item['name'] for item in results)
    assert not any(item['name'] == 'foreign-case' for item in results)
    suite_error_result = next(
        item for item in results if item['id'] == suite_error.pk and item['kind'] == 'suite'
    )
    assert suite_error_result['kind'] == 'suite'
    assert suite_error_result['status'] == 'error'
    assert suite_error_result['failure_message'] == 'suite worker error'
    assert next(
        item for item in results if item['id'] == suite_failure.pk and item['kind'] == 'suite'
    )['failed_step_number'] == 4
    assert next(
        item for item in results if item['id'] == case_failure.pk and item['kind'] == 'case'
    )['failed_step_number'] == 2
    plain_log_result = next(
        item for item in results if item['id'] == case_error.pk and item['kind'] == 'case'
    )
    assert plain_log_result['status'] == 'error'
    assert plain_log_result['failure_message'] == 'Browser worker crashed at step 3'
    assert plain_log_result['failed_step_number'] == 3

    denied = _client_for(user).get(
        '/api/ui-automation/dashboard/recent-executions/',
        {'project': private_project.pk, 'status': 'failed'},
    )
    assert denied.status_code == 404


def _suite_run_fixture(name):
    user, project = _user_project(name)
    suite = UiTestSuite.objects.create(project=project, name=f'{name}-suite')
    case = UiTestCase.objects.create(project=project, name=f'{name}-case', created_by=user)
    UiTestSuiteTestCase.objects.create(test_suite=suite, test_case=case)
    return user, project, suite


def _allow_playwright(monkeypatch):
    from apps.ui_automation.playwright_engine import PlaywrightTestEngine

    monkeypatch.setattr(
        PlaywrightTestEngine,
        'check_execution_environment_sync',
        staticmethod(lambda _browser: (True, '')),
    )


def _immediate_thread(monkeypatch, *, start_error=None):
    class ImmediateThread:
        def __init__(self, target, daemon=None):
            self.target = target
            self.daemon = daemon

        def start(self):
            if start_error:
                raise start_error
            self.target()

        def join(self):
            return  # start() runs the target synchronously in these tests.

    monkeypatch.setattr(threading, 'Thread', ImmediateThread)


@pytest.mark.django_db
def test_suite_runs_pass_precreated_execution_and_get_independent_ids(monkeypatch):
    user, _project, suite = _suite_run_fixture('suite-identity')
    _allow_playwright(monkeypatch)
    _immediate_thread(monkeypatch)
    seen_execution_ids = []

    def run_with_existing_execution(executor):
        seen_execution_ids.append(executor.execution.pk)
        assert executor.create_execution_record() is executor.execution
        executor.execution.status = 'SUCCESS'
        executor.execution.finished_at = timezone.now()
        executor.execution.save(update_fields=['status', 'finished_at'])

    monkeypatch.setattr(test_executor.TestExecutor, 'run', run_with_existing_execution)
    client = _client_for(user)
    first = client.post(f'/api/ui-automation/test-suites/{suite.pk}/run_suite/', {}, format='json')
    second = client.post(f'/api/ui-automation/test-suites/{suite.pk}/run_suite/', {}, format='json')

    assert first.status_code == 200
    assert second.status_code == 200
    first_id = first.data['execution_id']
    second_id = second.data['execution_id']
    assert first_id != second_id
    assert seen_execution_ids == [first_id, second_id]
    assert UiTestExecution.objects.filter(test_suite=suite).count() == 2
    assert suite.suite_test_cases.count() == 1


@pytest.mark.django_db
def test_suite_worker_exception_marks_aggregate_failed(monkeypatch):
    user, _project, suite = _suite_run_fixture('suite-worker-error')
    _allow_playwright(monkeypatch)
    _immediate_thread(monkeypatch)

    def fail_worker(_executor):
        raise RuntimeError('worker failed')

    monkeypatch.setattr(test_executor.TestExecutor, 'run', fail_worker)
    response = _client_for(user).post(
        f'/api/ui-automation/test-suites/{suite.pk}/run_suite/', {}, format='json'
    )

    assert response.status_code == 200
    execution = UiTestExecution.objects.get(pk=response.data['execution_id'])
    assert execution.status == 'FAILED'
    assert execution.finished_at is not None
    assert execution.error_message == INTERNAL_SUITE_EXECUTION_ERROR
    assert execution.failure_summary['message'] == INTERNAL_SUITE_EXECUTION_ERROR
    suite.refresh_from_db()
    assert suite.execution_status == 'failed'


@pytest.mark.django_db
def test_suite_thread_launch_exception_marks_aggregate_failed(monkeypatch):
    user, _project, suite = _suite_run_fixture('suite-launch-error')
    _allow_playwright(monkeypatch)
    _immediate_thread(monkeypatch, start_error=RuntimeError('thread start failed'))

    response = _client_for(user).post(
        f'/api/ui-automation/test-suites/{suite.pk}/run_suite/', {}, format='json'
    )

    assert response.status_code == 500
    execution = UiTestExecution.objects.get(pk=response.data['execution_id'])
    assert execution.status == 'FAILED'
    assert execution.finished_at is not None
    assert execution.error_message == response.data['error'] == INTERNAL_SUITE_EXECUTION_ERROR


@pytest.mark.django_db
def test_recent_status_filters_follow_workbench_normalization():
    user, project, suite = _suite_run_fixture('status-aliases')
    case = suite.suite_test_cases.get().test_case
    passed = UiTestCaseExecution.objects.create(
        test_case=case, project=project, created_by=user, status='  CoMpLeTeD  ',
    )
    aborted = UiTestExecution.objects.create(project=project, test_suite=suite, status=' stopped ')
    client = _client_for(user)
    for execution, normalized_status in ((passed, 'passed'), (aborted, 'aborted')):
        response = client.get('/api/ui-automation/dashboard/recent-executions/', {
            'project': project.pk, 'status': normalized_status,
        })
        assert response.status_code == 200
        assert [(row['id'], row['status']) for row in response.data['results']] == [(execution.pk, normalized_status)]


@pytest.mark.django_db
def test_recent_failure_summary_repairs_late_legacy_writes_once():
    user, project, suite = _suite_run_fixture('late-legacy-finish')
    case = suite.suite_test_cases.get().test_case
    standalone = UiTestCaseExecution.objects.create(test_case=case, project=project, created_by=user, status='running')
    shared = UiTestExecution.objects.create(project=project, test_suite=suite, status='RUNNING')
    # These writes represent the old worker's update after migration 0005 already ran.
    UiTestCaseExecution.objects.filter(pk=standalone.pk).update(
        status='failed', failure_summary=None,
        execution_logs='[{"step_number":1,"success":true},{"step_number":2,"success":false,"error":"Wrong title"}]',
    )
    UiTestExecution.objects.filter(pk=shared.pk).update(
        status='FAILED', result_data={'test_cases': [{'status': 'failed', 'steps': [
            {'step_number': 3, 'success': False, 'error': 'Wrong total'},
        ]}]},
    )
    client = _client_for(user)
    request_args = {'project': project.pk, 'status': 'failed'}
    path = '/api/ui-automation/dashboard/recent-executions/'
    first = client.get(path, request_args)
    assert first.status_code == 200
    assert {(row['kind'], row['failed_step_number'], row['failure_message']) for row in first.data['results']} == {
        ('case', 2, 'Wrong title'), ('suite', 3, 'Wrong total'),
    }
    with CaptureQueriesContext(connection) as queries:
        second = client.get(path, request_args)
    assert second.data == first.data
    sql = ' '.join(query['sql'].lower() for query in queries)
    assert not any(field in sql for field in ('result_data', 'execution_logs', 'screenshots'))


@pytest.mark.django_db
@pytest.mark.parametrize('fill_at', ['before_payload_read', 'before_update'])
def test_recent_failure_summary_returns_the_concurrent_winners_summary(monkeypatch, fill_at):
    user, project, suite = _suite_run_fixture(f'concurrent-failure-{fill_at}')
    case = suite.suite_test_cases.get().test_case
    execution = UiTestCaseExecution.objects.create(test_case=case, project=project, created_by=user, status='running')
    record = UiTestCaseExecution.objects.filter(pk=execution.pk)
    record.update(status='failed', failure_summary=None, execution_logs=(
        '[{"step_number":1,"success":false,"error":"Original assertion"}]'
    ))
    winner = {'step_number': 2, 'message': 'Updated assertion'}

    if fill_at == 'before_payload_read':
        recover = views._recover_recent_failure_summaries

        def fill_then_recover(executions, rows, payload_field):
            if payload_field == 'execution_logs':
                assert rows[0][-1] is None
                record.update(failure_summary=winner)
            return recover(executions, rows, payload_field)

        monkeypatch.setattr(views, '_recover_recent_failure_summaries', fill_then_recover)
    else:
        build_summary = views.build_failure_summary

        def fill_then_build(*args, **kwargs):
            record.update(failure_summary=winner)
            return build_summary(*args, **kwargs)

        monkeypatch.setattr(views, 'build_failure_summary', fill_then_build)

    response = _client_for(user).get('/api/ui-automation/dashboard/recent-executions/', {'project': project.pk})
    assert response.status_code == 200
    result, = response.data['results']
    assert result['id'] == execution.pk
    assert result['failed_step_number'] == winner['step_number']
    assert result['failure_message'] == winner['message']
    execution.refresh_from_db()
    assert execution.failure_summary == winner


@pytest.mark.django_db
def test_legacy_failure_summaries_are_populated_without_rewriting_evidence():
    user, project, suite = _suite_run_fixture('legacy-summaries')
    case = suite.suite_test_cases.get().test_case
    case_logs = '[{"step_number":1,"success":true},{"step_number":2,"success":false,"error":"case assertion"}]'
    case_execution = UiTestCaseExecution.objects.create(
        test_case=case, project=project, created_by=user, status='failed',
        execution_logs=case_logs,
    )
    result_data = {'test_cases': [{'status': 'failed', 'steps': [
        {'step_number': 1, 'success': True},
        {'step_number': 4, 'success': False, 'error': 'suite assertion'},
    ], 'screenshots': [{'url': 'stored evidence'}]}]}
    suite_execution = UiTestExecution.objects.create(
        project=project, test_suite=suite, status='FAILED', result_data=result_data,
    )
    UiTestCaseExecution.objects.filter(pk=case_execution.pk).update(failure_summary=None)
    UiTestExecution.objects.filter(pk=suite_execution.pk).update(failure_summary={})
    migration = import_module('apps.ui_automation.migrations.0005_backfill_execution_failure_summaries')
    migration.backfill_execution_failure_summaries(apps, SimpleNamespace(connection=connection))

    case_execution.refresh_from_db()
    suite_execution.refresh_from_db()
    assert case_execution.failure_summary == {'message': 'case assertion', 'step_number': 2}
    assert suite_execution.failure_summary == {'message': 'suite assertion', 'step_number': 4}
    assert case_execution.execution_logs == case_logs
    assert suite_execution.result_data == result_data


@pytest.mark.django_db
def test_internal_case_error_returns_public_text_and_an_execution_id(monkeypatch):
    from apps.ui_automation.playwright_engine import PlaywrightTestEngine

    user, project = _user_project('internal-error')
    case = UiTestCase.objects.create(project=project, name='case fails internally', created_by=user)
    log_exception = Mock()
    monkeypatch.setattr(views.logger, 'exception', log_exception)

    def fail_preflight(_browser):
        raise RuntimeError('unshareable operational details')

    monkeypatch.setattr(PlaywrightTestEngine, 'check_execution_environment_sync', staticmethod(fail_preflight))
    result = _client_for(user).post(f'/api/ui-automation/test-cases/{case.pk}/run/', {}, format='json')
    assert result.status_code == 500
    assert result.data['errors'] == [{'message': INTERNAL_CASE_EXECUTION_ERROR}]
    assert result.data['logs'] == INTERNAL_CASE_EXECUTION_ERROR
    execution = UiTestCaseExecution.objects.get(pk=result.data['execution_id'])
    assert execution.status == 'error'
    assert execution.finished_at is not None
    assert execution.error_message == execution.execution_logs == INTERNAL_CASE_EXECUTION_ERROR
    assert execution.failure_summary == {'message': INTERNAL_CASE_EXECUTION_ERROR, 'step_number': None}
    log_exception.assert_called_once()


@pytest.mark.django_db
@pytest.mark.parametrize('failure_at', ['step', 'worker', 'start', 'step_result'])
def test_unexpected_case_worker_errors_remain_safe_and_keep_the_result(monkeypatch, failure_at):
    user, project = _user_project(f'case-runtime-{failure_at}')
    case = UiTestCase.objects.create(project=project, name='faulted run', created_by=user)
    UiTestCaseStep.objects.create(test_case=case, action_type='wait', step_number=1)
    driver_error = WebDriverException('private fixture details', stacktrace=['fixture stack frame'])

    class FakeEngine:
        def __init__(self, **_kwargs):
            if failure_at == 'worker':
                raise RuntimeError('private fixture details')

        @staticmethod
        def check_execution_environment(_browser):
            return True, ''

        def start(self):
            if failure_at == 'start':
                raise driver_error

        def navigate(self, _url):
            return True, ''

        def execute_step(self, *_args):
            if failure_at == 'step_result':
                return False, str(driver_error), None
            raise RuntimeError('private fixture details')

        def capture_screenshot(self):
            return None

        def stop(self):
            pass

    monkeypatch.setattr('apps.ui_automation.selenium_engine.SeleniumTestEngine', FakeEngine)
    _immediate_thread(monkeypatch)
    response = _client_for(user).post(f'/api/ui-automation/test-cases/{case.pk}/run/', {'engine': 'selenium'}, format='json')
    assert response.status_code == 200
    assert response.data['success'] is False
    assert 'private fixture details' not in str(response.data)
    assert 'Traceback' not in str(response.data)
    assert 'Stacktrace' not in str(response.data)
    execution = UiTestCaseExecution.objects.get(pk=response.data['execution_id'])
    assert execution.status == ('error' if failure_at == 'worker' else 'failed')
    assert 'private fixture details' not in execution.error_message
    assert 'fixture stack frame' not in execution.execution_logs
    if failure_at == 'start':
        assert response.data['errors'][0]['step_number'] == 0
        assert '检查执行服务' in response.data['errors'][0]['details']
        assert '检查执行服务' in execution.failure_summary['message']
    else:
        assert INTERNAL_CASE_EXECUTION_ERROR in execution.error_message
    expected_step = 1 if failure_at in {'step', 'step_result'} else None
    assert execution.failure_summary['step_number'] == expected_step


@pytest.mark.django_db
def test_legacy_suite_traceback_is_hidden_in_reports_and_recent_executions():
    user, project, suite = _suite_run_fixture('internal-suite-error')
    execution = UiTestExecution.objects.create(project=project, test_suite=suite, status='FAILED', error_message=(
        'Legacy worker details\nTraceback (most recent call last):\ninternal code location'
    ))
    client = _client_for(user)
    detail = client.get(f'/api/ui-automation/test-executions/{execution.pk}/')
    assert detail.status_code == 200
    assert detail.data['error_message'] == INTERNAL_SUITE_EXECUTION_ERROR
    listing = client.get('/api/ui-automation/test-executions/', {'search': 'Legacy worker details'})
    assert listing.status_code == 200
    assert listing.data['count'] == 0
    recent = client.get('/api/ui-automation/dashboard/recent-executions/', {'project': project.pk})
    assert recent.data['results'][0]['failure_message'] == INTERNAL_SUITE_EXECUTION_ERROR


@pytest.mark.django_db
def test_legacy_suite_nested_diagnostics_are_redacted_in_detail_and_list():
    user, project, suite = _suite_run_fixture('nested-suite-error')
    case = suite.suite_test_cases.get().test_case
    stack = str(WebDriverException('private fixture diagnostics', stacktrace=['fixture frame']))
    screenshot = {'url': 'data:image/png;base64,AAAA', 'description': stack}
    payload = {'test_cases': [{
        'test_case_id': case.pk,
        'test_case_name': case.name,
        'status': 'failed',
        'error': stack,
        'steps': [
            {'step_number': 1, 'success': True, 'description': 'Page opened'},
            {'step_number': 2, 'success': False, 'error': stack},
        ],
        'screenshots': [screenshot],
    }]}
    execution = UiTestExecution.objects.create(
        project=project, test_suite=suite, status='FAILED', result_data=payload,
    )
    client = _client_for(user)
    detail = client.get(f'/api/ui-automation/test-executions/{execution.pk}/')
    listing = client.get('/api/ui-automation/test-executions/', {'project': project.pk})
    assert detail.status_code == listing.status_code == 200
    for data in (detail.data, listing.data['results'][0]):
        case_result, = data['result_data']['test_cases']
        assert case_result['test_case_id'] == case.pk
        assert case_result['error'] == INTERNAL_SUITE_EXECUTION_ERROR
        assert case_result['steps'] == [
            payload['test_cases'][0]['steps'][0],
            {'step_number': 2, 'success': False, 'error': INTERNAL_SUITE_EXECUTION_ERROR},
        ]
        assert case_result['screenshots'] == [dict(screenshot, description=INTERNAL_SUITE_EXECUTION_ERROR)]
        assert 'private fixture diagnostics' not in str(data['result_data'])
    execution.refresh_from_db()
    assert execution.result_data == payload


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['python', 'selenium'])
def test_legacy_traceback_is_hidden_in_detail_search_and_recent_executions(kind):
    owner, project = _user_project('legacy-internal-error')
    member = User.objects.create_user(username='legacy-summary-member')
    project.members.add(member)
    case = UiTestCase.objects.create(project=project, name='old execution', created_by=owner)
    private_error = 'unshareable operational detail'
    if kind == 'python':
        error_message = private_error
        execution_logs = f'执行失败: {private_error}\n\nTraceback (most recent call last):\ninternal code location'
    else:
        error_message = f'Chrome 浏览器启动失败: {WebDriverException(private_error, stacktrace=["fixture stack frame"])}'
        execution_logs = '[]'
    execution = UiTestCaseExecution.objects.create(
        test_case=case, project=project, created_by=owner, status='error', error_message=error_message,
        execution_logs=execution_logs,
    )
    client = _client_for(member)
    detail = client.get(f'/api/ui-automation/test-case-executions/{execution.pk}/')
    assert detail.status_code == 200
    assert detail.data['execution_logs'] == (INTERNAL_CASE_EXECUTION_ERROR if kind == 'python' else '[]')
    assert detail.data['error_message'] == INTERNAL_CASE_EXECUTION_ERROR
    listing = client.get('/api/ui-automation/test-case-executions/', {'search': private_error})
    assert listing.status_code == 200
    assert listing.data['count'] == 0
    visible_search = client.get('/api/ui-automation/test-case-executions/', {'search': INTERNAL_CASE_EXECUTION_ERROR})
    assert visible_search.data['count'] == 1
    recent = client.get('/api/ui-automation/dashboard/recent-executions/', {'project': project.pk})
    assert recent.data['results'][0]['failure_message'] == INTERNAL_CASE_EXECUTION_ERROR


@pytest.mark.django_db
def test_legacy_structured_logs_keep_failed_step_in_detail_and_repaired_summary():
    user, project = _user_project('structured-legacy')
    case = UiTestCase.objects.create(project=project, name='Case with historical steps', created_by=user)
    stack = str(WebDriverException('private fixture diagnostics', stacktrace=['fixture frame']))
    steps = [
        {'step_number': 1, 'success': True, 'description': 'Page opened'},
        {'step_number': 2, 'success': False, 'error': stack},
    ]
    logs = json.dumps(steps)
    screenshot = {'url': 'data:image/png;base64,AAAA', 'step_number': 2, 'description': stack}
    execution = UiTestCaseExecution.objects.create(
        test_case=case, project=project, status='failed', created_by=user,
        error_message=stack, execution_logs=logs, screenshots=[screenshot],
    )
    expected_summary = {'message': INTERNAL_CASE_EXECUTION_ERROR, 'step_number': 2}
    assert execution.failure_summary == expected_summary
    UiTestCaseExecution.objects.filter(pk=execution.pk).update(
        failure_summary={'message': INTERNAL_CASE_EXECUTION_ERROR, 'step_number': None},
    )
    plain = UiTestCaseExecution.objects.create(
        test_case=case, project=project, status='error', created_by=user, execution_logs=stack,
    )

    migration = import_module('apps.ui_automation.migrations.0006_restore_case_failure_steps')
    migration.restore_case_failure_steps(apps, SimpleNamespace(connection=connection))
    migration.restore_case_failure_steps(apps, SimpleNamespace(connection=connection))
    execution.refresh_from_db()
    plain.refresh_from_db()
    assert execution.failure_summary == expected_summary
    assert execution.execution_logs == logs
    assert execution.screenshots == [screenshot]
    assert plain.failure_summary == {'message': INTERNAL_CASE_EXECUTION_ERROR, 'step_number': None}

    client = _client_for(user)
    detail = client.get(f'/api/ui-automation/test-case-executions/{execution.pk}/')
    assert detail.status_code == 200
    assert detail.data['error_message'] == INTERNAL_CASE_EXECUTION_ERROR
    assert json.loads(detail.data['execution_logs']) == [
        steps[0], dict(steps[1], error=INTERNAL_CASE_EXECUTION_ERROR),
    ]
    assert detail.data['screenshots'] == [dict(screenshot, description=INTERNAL_CASE_EXECUTION_ERROR)]
    with CaptureQueriesContext(connection) as queries:
        recent = client.get('/api/ui-automation/dashboard/recent-executions/', {'project': project.pk})
    result = next(item for item in recent.data['results'] if item['id'] == execution.pk)
    assert result['failure_message'] == INTERNAL_CASE_EXECUTION_ERROR
    assert result['failed_step_number'] == 2
    assert all('execution_logs' not in query['sql'].lower() for query in queries)


def test_manual_case_preflight_failure_returns_created_execution_id(monkeypatch):
    execution = SimpleNamespace(id=917, status='running', save=Mock())
    test_case = SimpleNamespace(
        id=3,
        name='preflight case',
        project=SimpleNamespace(base_url='https://example.test'),
    )

    class UnavailableSelenium:
        @staticmethod
        def check_execution_environment(_browser):
            return False, 'driver missing'

    monkeypatch.setattr(views.TestCaseExecution.objects, 'create', lambda **_kwargs: execution)
    monkeypatch.setattr(views.TestCaseViewSet, 'get_object', lambda _self: test_case)
    monkeypatch.setattr('apps.ui_automation.selenium_engine.SeleniumTestEngine', UnavailableSelenium)

    response = views.TestCaseViewSet().run(SimpleNamespace(
        data={'engine': 'selenium', 'browser': 'chrome'},
        user=SimpleNamespace(username='case-runner'),
    ))

    assert response.status_code == 400
    assert response.data['execution_id'] == execution.id
    assert execution.status == 'failed'
    execution.save.assert_called_once()
