from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest.mock import Mock
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIClient

from apps.ui_automation import tasks
from apps.ui_automation.case_runner import execute_case_execution, persist_case_result
from apps.ui_automation.models import (
    TestCase as UiTestCase,
    TestCaseExecution as UiCaseExecution,
    TestExecution as UiSuiteExecution,
    TestSuite as UiTestSuite,
    TestSuiteTestCase as UiSuiteTestCase,
    UiProject,
)
from apps.ui_automation.workbench import (
    create_case_execution,
    create_suite_execution,
    dispatch_execution,
    reconcile_pending_submissions,
)

User = get_user_model()
OPTIONS = {'engine': 'playwright', 'browser': 'chrome', 'headless': True}


def _context(name):
    user = User.objects.create_user(username=f'{name}-owner')
    project = UiProject.objects.create(
        name=name,
        base_url=f'https://{name}.example.test',
        owner=user,
    )
    case = UiTestCase.objects.create(project=project, name=f'{name}-case', created_by=user)
    suite = UiTestSuite.objects.create(project=project, name=f'{name}-suite')
    UiSuiteTestCase.objects.create(test_suite=suite, test_case=case)
    return user, project, case, suite


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _create_execution(kind, user, case, suite, request_id):
    if kind == 'case':
        return create_case_execution(case, user, OPTIONS, client_request_id=request_id)[0]
    return create_suite_execution(suite, user, OPTIONS, client_request_id=request_id)[0]


def _post_replay(kind, client, project, case, suite, request_id):
    if kind == 'case':
        return client.post(
            f'/api/ui-automation/test-cases/{case.pk}/run/',
            {
                'async': True,
                'client_request_id': str(request_id),
                'project_id': project.pk,
                **OPTIONS,
            },
            format='json',
        )
    return client.post(
        f'/api/ui-automation/test-suites/{suite.pk}/run_suite/',
        {'client_request_id': str(request_id), **OPTIONS},
        format='json',
    )


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_replay_recovers_submission_persisted_before_first_publish(monkeypatch, kind):
    user, project, case, suite = _context(f'persisted-replay-{kind}')
    request_id = uuid.uuid4()
    execution = _create_execution(kind, user, case, suite, request_id)
    task = tasks.execute_workbench_case if kind == 'case' else tasks.execute_workbench_suite
    dispatch = Mock()
    monkeypatch.setattr(task, 'apply_async', dispatch)

    response = _post_replay(kind, _client(user), project, case, suite, request_id)

    assert response.status_code == 202
    assert response.data['replayed'] is True
    assert response.data['execution_id'] == execution.pk
    dispatch.assert_called_once_with(args=(execution.pk, str(execution.dispatch_claim)), retry=False)
    execution.refresh_from_db()
    assert execution.dispatch_enqueued_at is not None
    assert execution.dispatch_delivery_token is None

    replay = _post_replay(kind, _client(user), project, case, suite, request_id)
    assert replay.status_code == 202
    assert replay.data['execution_id'] == execution.pk
    assert dispatch.call_count == 1


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_expired_pre_publish_lease_is_reclaimed_by_bounded_reconciler(monkeypatch, kind):
    user, project, case, suite = _context(f'expired-lease-{kind}')
    request_id = uuid.uuid4()
    execution = _create_execution(kind, user, case, suite, request_id)
    execution_model = UiCaseExecution if kind == 'case' else UiSuiteExecution
    task = tasks.execute_workbench_case if kind == 'case' else tasks.execute_workbench_suite
    dispatch = Mock()
    monkeypatch.setattr(task, 'apply_async', dispatch)
    execution_model.objects.filter(pk=execution.pk).update(
        dispatch_delivery_token=uuid.uuid4(),
        dispatch_delivery_expires_at=timezone.now() + timedelta(minutes=1),
    )

    replay = _post_replay(kind, _client(user), project, case, suite, request_id)
    assert replay.status_code == 202
    dispatch.assert_not_called()

    execution_model.objects.filter(pk=execution.pk).update(
        dispatch_delivery_expires_at=timezone.now() - timedelta(seconds=1),
    )
    assert reconcile_pending_submissions(limit=1) == 1
    dispatch.assert_called_once_with(args=(execution.pk, str(execution.dispatch_claim)), retry=False)
    execution.refresh_from_db()
    assert execution.dispatch_enqueued_at is not None

    replay = _post_replay(kind, _client(user), project, case, suite, request_id)
    assert replay.status_code == 202
    assert dispatch.call_count == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_dispatch_reservations_publish_only_once(monkeypatch):
    user, _project, case, _suite = _context('concurrent-delivery-lease')
    execution = create_case_execution(case, user, OPTIONS)[0]
    entered_publish = Event()
    finish_publish = Event()
    calls = []
    calls_lock = Lock()

    def publish(*, args, **options):
        assert options.get('retry') is False
        with calls_lock:
            calls.append(args)
        entered_publish.set()
        if not finish_publish.wait(5):
            raise TimeoutError('test publisher was not released')

    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', publish)

    def attempt():
        close_old_connections()
        try:
            row = UiCaseExecution.objects.get(pk=execution.pk)
            return dispatch_execution('case', row)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt)
        assert entered_publish.wait(5)
        second = pool.submit(attempt)
        assert second.result(timeout=5) is False
        finish_publish.set()
        assert first.result(timeout=5) is True

    assert calls == [(execution.pk, str(execution.dispatch_claim))]
    execution.refresh_from_db()
    assert execution.dispatch_enqueued_at is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_concurrent_duplicate_workers_claim_execution_once(monkeypatch, kind):
    from apps.ui_automation.suite_runner import _finish_suite, execute_suite_execution

    user, _project, case, suite = _context(f'concurrent-worker-{kind}')
    execution = _create_execution(kind, user, case, suite, None)
    worker = execute_case_execution if kind == 'case' else execute_suite_execution
    entered_runner = Event()
    finish_runner = Event()
    calls = []
    calls_lock = Lock()

    if kind == 'case':
        def run_case(_view, request, pk=None):
            with calls_lock:
                calls.append(pk)
            entered_runner.set()
            if not finish_runner.wait(5):
                raise TimeoutError('test case runner was not released')
            assert persist_case_result(
                request._ui_execution.pk,
                request._ui_dispatch_claim,
                status='passed',
                fields={'finished_at': timezone.now()},
            )
            return Response({'success': True})

        monkeypatch.setattr('apps.ui_automation.views.TestCaseViewSet.run', run_case)
    else:
        monkeypatch.setattr('apps.ui_automation.suite_runner._check_environment', lambda *_args: (True, ''))

        class BlockingExecutor:
            def __init__(self, **kwargs):
                self.execution = kwargs['execution']

            def run(self):
                with calls_lock:
                    calls.append(self.execution.pk)
                entered_runner.set()
                if not finish_runner.wait(5):
                    raise TimeoutError('test suite runner was not released')
                assert _finish_suite(
                    self.execution.pk,
                    str(self.execution.dispatch_claim),
                    status='SUCCESS',
                )

        monkeypatch.setattr('apps.ui_automation.test_executor.TestExecutor', BlockingExecutor)

    def run_worker():
        close_old_connections()
        try:
            return worker(execution.pk, str(execution.dispatch_claim))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_worker)
        assert entered_runner.wait(5)
        second = pool.submit(run_worker)
        assert second.result(timeout=5) is False
        finish_runner.set()
        assert first.result(timeout=5) is True

    assert len(calls) == 1
    execution.refresh_from_db()
    assert execution.status in ({'passed'} if kind == 'case' else {'SUCCESS'})


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_cancelled_pending_execution_is_not_recovered(monkeypatch, kind):
    user, _project, case, suite = _context(f'cancelled-reconcile-{kind}')
    execution = _create_execution(kind, user, case, suite, uuid.uuid4())
    task = tasks.execute_workbench_case if kind == 'case' else tasks.execute_workbench_suite
    dispatch = Mock()
    monkeypatch.setattr(task, 'apply_async', dispatch)
    url = (
        f'/api/ui-automation/test-case-executions/{execution.pk}/abort/'
        if kind == 'case'
        else f'/api/ui-automation/test-executions/{execution.pk}/abort/'
    )

    response = _client(user).post(url)
    assert response.status_code == 200
    assert reconcile_pending_submissions(limit=20) == 0
    execution.refresh_from_db()
    assert execution.status == ('aborted' if kind == 'case' else 'ABORTED')
    dispatch.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_late_publish_error_does_not_overwrite_worker_terminal_state(monkeypatch, kind):
    from apps.ui_automation.suite_runner import _finish_suite, execute_suite_execution

    user, project, case, suite = _context(f'late-publish-error-{kind}')
    request_id = uuid.uuid4()
    execution = _create_execution(kind, user, case, suite, request_id)
    task = tasks.execute_workbench_case if kind == 'case' else tasks.execute_workbench_suite

    if kind == 'case':
        from apps.ui_automation.views import TestCaseViewSet

        original_run = TestCaseViewSet.run

        def run_case(view, request, pk=None):
            if not hasattr(request, '_ui_execution'):
                return original_run(view, request, pk=pk)
            assert persist_case_result(
                request._ui_execution.pk,
                request._ui_dispatch_claim,
                status='passed',
                fields={'finished_at': timezone.now()},
            )
            return Response({'success': True})

        monkeypatch.setattr(TestCaseViewSet, 'run', run_case)
        worker = execute_case_execution
    else:
        monkeypatch.setattr('apps.ui_automation.suite_runner._check_environment', lambda *_args: (True, ''))

        class FinishingExecutor:
            def __init__(self, **kwargs):
                self.execution = kwargs['execution']

            def run(self):
                assert _finish_suite(
                    self.execution.pk,
                    str(self.execution.dispatch_claim),
                    status='SUCCESS',
                )

        monkeypatch.setattr('apps.ui_automation.test_executor.TestExecutor', FinishingExecutor)
        worker = execute_suite_execution

    def publish_after_accept(*, args, **options):
        assert options.get('retry') is False
        assert worker(*args) is True
        raise RuntimeError('broker response lost after task acceptance')

    monkeypatch.setattr(task, 'apply_async', publish_after_accept)
    response = _post_replay(kind, _client(user), project, case, suite, request_id)

    assert response.status_code == 202
    execution.refresh_from_db()
    assert execution.status == ('passed' if kind == 'case' else 'SUCCESS')
    assert execution.failure_stage == ''
    assert execution.finished_at is not None
    assert reconcile_pending_submissions(limit=20) == 0


@pytest.mark.django_db
def test_acknowledged_pending_execution_is_not_republished_after_lease_ttl(monkeypatch):
    user, _project, case, _suite = _context('acknowledged-queue-item')
    execution = create_case_execution(case, user, OPTIONS)[0]
    dispatch = Mock()
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', dispatch)
    sent_at = timezone.now()

    assert dispatch_execution('case', execution, now=sent_at) is True
    execution.refresh_from_db()
    assert execution.dispatch_enqueued_at is not None
    assert reconcile_pending_submissions(limit=20, now=sent_at + timedelta(hours=1)) == 0
    dispatch.assert_called_once()


@pytest.mark.django_db
def test_reconciler_stops_after_first_broker_failure(monkeypatch):
    user, _project, case, suite = _context('reconcile-broker-break')
    first = create_case_execution(case, user, OPTIONS)[0]
    create_suite_execution(suite, user, OPTIONS)
    case_dispatch = Mock(side_effect=RuntimeError('broker unavailable'))
    suite_dispatch = Mock()
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', case_dispatch)
    monkeypatch.setattr(tasks.execute_workbench_suite, 'apply_async', suite_dispatch)

    assert reconcile_pending_submissions(limit=20, now=timezone.now()) == 0
    case_dispatch.assert_called_once()
    suite_dispatch.assert_not_called()
    first.refresh_from_db()
    assert first.dispatch_delivery_token is not None


@pytest.mark.django_db
def test_reconciler_limit_caps_total_case_and_suite_attempts(monkeypatch):
    user, _project, case, suite = _context('reconcile-total-limit')
    create_case_execution(case, user, OPTIONS)
    create_case_execution(case, user, OPTIONS)
    create_suite_execution(suite, user, OPTIONS)
    create_suite_execution(suite, user, OPTIONS)
    case_dispatch = Mock()
    suite_dispatch = Mock()
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', case_dispatch)
    monkeypatch.setattr(tasks.execute_workbench_suite, 'apply_async', suite_dispatch)

    assert reconcile_pending_submissions(limit=2, now=timezone.now()) == 2
    assert case_dispatch.call_count + suite_dispatch.call_count == 2


def test_reconcile_command_passes_a_bounded_batch(monkeypatch):
    from io import StringIO
    from django.core.management import call_command

    reconcile = Mock(return_value=3)
    stale_workers = Mock(return_value=2)
    monkeypatch.setattr(
        'apps.ui_automation.management.commands.reconcile_workbench_dispatches.reconcile_pending_submissions',
        reconcile,
    )
    monkeypatch.setattr(
        'apps.ui_automation.management.commands.reconcile_workbench_dispatches.reconcile_stale_workbench_executions',
        stale_workers,
    )
    output = StringIO()

    call_command('reconcile_workbench_dispatches', batch_size=3, stdout=output)

    reconcile.assert_called_once_with(limit=3)
    stale_workers.assert_called_once_with(limit=3)
    assert 'Reconciled UI workbench dispatches: 3' in output.getvalue()
    assert 'Reconciled UI workbench workers: 2' in output.getvalue()
