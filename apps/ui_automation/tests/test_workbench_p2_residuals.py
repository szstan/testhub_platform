from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, transaction
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIClient

from apps.ui_automation import tasks
from apps.ui_automation.case_runner import CaseExecutionCancelled, persist_case_result
from apps.ui_automation.models import (
    TestCase as UiTestCase,
    TestCaseExecution as UiCaseExecution,
    TestExecution as UiSuiteExecution,
    TestSuite as UiTestSuite,
    TestSuiteTestCase as UiSuiteTestCase,
    UiProject,
)
from apps.ui_automation.suite_runner import _finish_suite
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


def _new_suite_parent(user, project, suite, *, status='PENDING'):
    nonce = uuid.uuid4() if status == 'RUNNING' else None
    now = timezone.now()
    return UiSuiteExecution.objects.create(
        project=project,
        test_suite=suite,
        executed_by=user,
        status=status,
        dispatch_claim=uuid.uuid4(),
        worker_nonce=nonce,
        worker_heartbeat_at=now if nonce else None,
        worker_lease_expires_at=now + timedelta(minutes=5) if nonce else None,
        request_snapshot={
            'config': dict(OPTIONS),
            'cases': [],
        },
    )


@pytest.mark.django_db
def test_playwright_prepare_failure_does_not_sweep_children_of_other_suite_parents(monkeypatch):
    from apps.ui_automation.test_executor import TestExecutor

    user, project, case, suite = _context('playwright-parent-fence')
    first_parent = _new_suite_parent(user, project, suite, status='RUNNING')
    second_parent = _new_suite_parent(user, project, suite, status='RUNNING')
    first_child = UiCaseExecution.objects.create(
        test_case=case,
        project=project,
        test_suite=suite,
        parent_execution=first_parent,
        created_by=user,
        status='pending',
    )
    second_child = UiCaseExecution.objects.create(
        test_case=case,
        project=project,
        test_suite=suite,
        parent_execution=second_parent,
        created_by=user,
        status='pending',
    )
    first_parent._ui_dispatch_claim = str(first_parent.dispatch_claim)
    first_parent._ui_worker_nonce = str(first_parent.worker_nonce)
    executor = TestExecutor(
        suite,
        engine='playwright',
        execution=first_parent,
        executed_by=user,
    )
    executor.test_cases = [case]
    monkeypatch.setattr(
        'apps.ui_automation.playwright_engine.PlaywrightTestEngine.check_execution_environment_sync',
        staticmethod(lambda _browser: (False, 'browser preparation failed')),
    )

    executor.run_with_playwright()

    first_child.refresh_from_db()
    second_child.refresh_from_db()
    assert first_child.status == 'pending'
    assert second_child.status == 'pending'


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_claim_rollback_releases_published_pending_row_for_poller_recovery(monkeypatch, kind):
    from apps.ui_automation import case_runner, suite_runner
    from apps.ui_automation.views import TestCaseViewSet

    user, _project, case, suite = _context(f'claim-rollback-{kind}')
    if kind == 'case':
        execution = create_case_execution(case, user, OPTIONS)[0]
        task = tasks.execute_workbench_case
        runner_module = case_runner
    else:
        execution = create_suite_execution(suite, user, OPTIONS)[0]
        task = tasks.execute_workbench_suite
        runner_module = suite_runner

    published = Mock()
    monkeypatch.setattr(task, 'apply_async', published)
    assert dispatch_execution(kind, execution) is True

    real_atomic = transaction.atomic

    @contextmanager
    def rollback_after_claim(*args, **kwargs):
        with real_atomic(*args, **kwargs):
            yield
            raise RuntimeError('claim transaction rolled back after update')

    monkeypatch.setattr(
        runner_module,
        'transaction',
        SimpleNamespace(atomic=rollback_after_claim),
    )

    assert task.run(execution.pk, str(execution.dispatch_claim)) is False
    execution.refresh_from_db()
    assert execution.status == ('pending' if kind == 'case' else 'PENDING')
    assert execution.worker_nonce is None
    assert execution.dispatch_enqueued_at is None

    monkeypatch.setattr(
        runner_module,
        'transaction',
        SimpleNamespace(atomic=real_atomic),
    )
    assert reconcile_pending_submissions(limit=1) == 1
    assert published.call_count == 2
    execution.refresh_from_db()
    assert execution.dispatch_enqueued_at is not None

    if kind == 'case':
        def finish_case(_view, request, pk=None):
            assert persist_case_result(
                request._ui_execution.pk,
                request._ui_dispatch_claim,
                status='passed',
                worker_nonce=request._ui_worker_nonce,
            )
            return Response({'success': True})

        monkeypatch.setattr(TestCaseViewSet, 'run', finish_case)
    else:
        monkeypatch.setattr(suite_runner, '_check_environment', lambda *_args: (True, ''))

        class FinishingExecutor:
            def __init__(self, **kwargs):
                self.execution = kwargs['execution']

            def run(self):
                assert _finish_suite(
                    self.execution.pk,
                    str(self.execution.dispatch_claim),
                    status='SUCCESS',
                    worker_nonce=self.execution._ui_worker_nonce,
                )

        monkeypatch.setattr('apps.ui_automation.test_executor.TestExecutor', FinishingExecutor)

    assert task.run(execution.pk, str(execution.dispatch_claim)) is True
    execution.refresh_from_db()
    assert execution.status == ('passed' if kind == 'case' else 'SUCCESS')


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_claim_rollback_before_publisher_ack_fences_late_ack(monkeypatch, kind):
    from apps.ui_automation import case_runner, suite_runner

    user, _project, case, suite = _context(f'claim-rollback-before-ack-{kind}')
    if kind == 'case':
        execution = create_case_execution(case, user, OPTIONS)[0]
        model = UiCaseExecution
        task = tasks.execute_workbench_case
        runner_module = case_runner
        pending_status = 'pending'
    else:
        execution = create_suite_execution(suite, user, OPTIONS)[0]
        model = UiSuiteExecution
        task = tasks.execute_workbench_suite
        runner_module = suite_runner
        pending_status = 'PENDING'

    publish_started = Event()
    allow_publisher_ack = Event()
    published_args = []

    def publish(*, args, **_options):
        published_args.append(args)
        publish_started.set()
        if not allow_publisher_ack.wait(5):
            raise TimeoutError('publisher ACK was not released')

    monkeypatch.setattr(task, 'apply_async', publish)

    real_atomic = transaction.atomic

    @contextmanager
    def rollback_after_claim(*args, **kwargs):
        with real_atomic(*args, **kwargs):
            yield
            raise RuntimeError('claim transaction rolled back before publisher ACK')

    monkeypatch.setattr(
        runner_module,
        'transaction',
        SimpleNamespace(atomic=rollback_after_claim),
    )

    def dispatch_in_thread():
        close_old_connections()
        try:
            row = model.objects.get(pk=execution.pk)
            return dispatch_execution(kind, row)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        dispatch_future = pool.submit(dispatch_in_thread)
        assert publish_started.wait(5)
        execution.refresh_from_db()
        stale_delivery_token = execution.dispatch_delivery_token
        assert stale_delivery_token is not None
        assert execution.dispatch_enqueued_at is None

        assert task.run(execution.pk, str(execution.dispatch_claim)) is False
        execution.refresh_from_db()
        assert execution.status == pending_status
        assert execution.worker_nonce is None
        assert execution.dispatch_enqueued_at is None
        assert execution.dispatch_delivery_token != stale_delivery_token
        assert execution.dispatch_delivery_token is None
        assert execution.dispatch_delivery_expires_at is None

        allow_publisher_ack.set()
        assert dispatch_future.result(timeout=5) is True

    execution.refresh_from_db()
    assert execution.dispatch_delivery_token is None
    assert execution.dispatch_enqueued_at is None
    assert published_args == [(execution.pk, str(execution.dispatch_claim))]

    monkeypatch.setattr(
        runner_module,
        'transaction',
        SimpleNamespace(atomic=real_atomic),
    )
    assert reconcile_pending_submissions(limit=1) == 1
    assert len(published_args) == 2
    execution.refresh_from_db()
    assert execution.dispatch_enqueued_at is not None


@pytest.mark.django_db(transaction=True)
def test_suite_running_progress_is_not_written_after_abort_races_claim_commit(monkeypatch):
    from apps.ui_automation import suite_runner

    user, project, _case, suite = _context('suite-claim-abort-order')
    execution = _new_suite_parent(user, project, suite)
    committed = Event()
    resume = Event()
    real_atomic = transaction.atomic

    @contextmanager
    def pause_after_commit(*args, **kwargs):
        with real_atomic(*args, **kwargs):
            yield
        committed.set()
        if not resume.wait(5):
            raise TimeoutError('claim test was not resumed')

    monkeypatch.setattr(
        suite_runner,
        'transaction',
        SimpleNamespace(atomic=pause_after_commit),
    )

    def claim_in_worker():
        close_old_connections()
        try:
            return suite_runner._claim_execution(execution.pk, str(execution.dispatch_claim))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(claim_in_worker)
        assert committed.wait(5)
        aborted = _client(user).post(f'/api/ui-automation/test-executions/{execution.pk}/abort/')
        assert aborted.status_code == 200
        suite.refresh_from_db()
        assert suite.execution_status == 'failed'
        resume.set()
        assert future.result(timeout=5) is not None

    execution.refresh_from_db()
    suite.refresh_from_db()
    assert execution.status == 'ABORTED'
    assert suite.execution_status == 'failed'


@pytest.mark.django_db
@pytest.mark.parametrize('engine_type', ['playwright', 'selenium'])
@pytest.mark.parametrize('phase', ['before_start', 'before_navigation'])
def test_case_ownership_is_checked_before_engine_start_and_navigation(monkeypatch, engine_type, phase):
    from apps.ui_automation.views import TestCaseViewSet

    user, project, case, _suite = _context(f'case-ownership-{engine_type}-{phase}')
    claim = uuid.uuid4()
    execution = UiCaseExecution.objects.create(
        test_case=case,
        project=project,
        created_by=user,
        status='running',
        dispatch_claim=claim,
    )
    calls = []
    starts = []
    navigations = []
    readiness = Mock(return_value=(True, 'ready'))

    if engine_type == 'playwright':
        class FakeEngine:
            check_execution_environment_sync = staticmethod(readiness)

            def __init__(self, **_kwargs):
                pass

            async def start(self):
                starts.append('start')

            async def navigate(self, url):
                navigations.append(url)
                return True, 'navigated'

            async def stop(self):
                return None

        monkeypatch.setattr('apps.ui_automation.playwright_engine.PlaywrightTestEngine', FakeEngine)
    else:
        class FakeEngine:
            check_execution_environment = staticmethod(readiness)

            def __init__(self, **_kwargs):
                pass

            def start(self):
                starts.append('start')

            def navigate(self, url):
                navigations.append(url)
                return True, 'navigated'

            def stop(self):
                return None

        monkeypatch.setattr('apps.ui_automation.selenium_engine.SeleniumTestEngine', FakeEngine)
        monkeypatch.setattr('apps.ui_automation.access.selenium_compatibility_enabled', lambda _user: True)

    def check_ownership():
        calls.append('check')
        if phase == 'before_start' and len(calls) == 1:
            raise CaseExecutionCancelled('cancelled before browser startup')
        if phase == 'before_navigation' and len(calls) == 3:
            raise CaseExecutionCancelled('cancelled before navigation')

    request = SimpleNamespace(
        data={'engine': engine_type, 'browser': 'chrome', 'headless': True},
        user=user,
        _ui_async_worker=True,
        _ui_execution=execution,
        _ui_dispatch_claim=str(claim),
        _ui_step_snapshot=[],
        _ui_test_case=case,
        _ui_base_url=project.base_url,
        _ui_cancel_check=check_ownership,
    )
    view = TestCaseViewSet()
    view.request = request
    view.action = 'run'
    view.kwargs = {'pk': str(case.pk)}

    response = view.run(request, pk=str(case.pk))

    assert response.status_code == 200
    assert calls
    if phase == 'before_start':
        readiness.assert_not_called()
        assert starts == []
        assert navigations == []
    else:
        readiness.assert_called_once()
        assert starts == ['start']
        assert navigations == []
    execution.refresh_from_db()
    assert execution.status == 'aborted'


@pytest.mark.django_db
@pytest.mark.parametrize('engine_type', ['playwright', 'selenium'])
def test_lost_case_lease_before_engine_start_does_not_start_browser_and_remains_unknown(monkeypatch, engine_type):
    from apps.ui_automation.worker_lease import WorkbenchWorkerLeaseLost
    from apps.ui_automation.workbench import reconcile_stale_workbench_executions
    from apps.ui_automation.views import TestCaseViewSet

    user, project, case, _suite = _context(f'case-lost-lease-{engine_type}')
    claim = uuid.uuid4()
    nonce = uuid.uuid4()
    expired = timezone.now() - timedelta(seconds=1)
    execution = UiCaseExecution.objects.create(
        test_case=case,
        project=project,
        created_by=user,
        status='running',
        dispatch_claim=claim,
        worker_nonce=nonce,
        worker_heartbeat_at=expired - timedelta(seconds=1),
        worker_lease_expires_at=expired,
    )
    starts = []
    navigations = []
    readiness = Mock(return_value=(True, 'ready'))
    if engine_type == 'playwright':
        class FakeEngine:
            check_execution_environment_sync = staticmethod(readiness)

            def __init__(self, **_kwargs):
                pass

            async def start(self):
                starts.append('start')

            async def navigate(self, url):
                navigations.append(url)
                return True, 'navigated'

            async def stop(self):
                return None

        monkeypatch.setattr('apps.ui_automation.playwright_engine.PlaywrightTestEngine', FakeEngine)
    else:
        class FakeEngine:
            check_execution_environment = staticmethod(readiness)

            def __init__(self, **_kwargs):
                pass

            def start(self):
                starts.append('start')

            def navigate(self, url):
                navigations.append(url)
                return True, 'navigated'

            def stop(self):
                return None

        monkeypatch.setattr('apps.ui_automation.selenium_engine.SeleniumTestEngine', FakeEngine)
        monkeypatch.setattr('apps.ui_automation.access.selenium_compatibility_enabled', lambda _user: True)
    request = SimpleNamespace(
        data={'engine': engine_type, 'browser': 'chrome', 'headless': True},
        user=user,
        _ui_async_worker=True,
        _ui_execution=execution,
        _ui_dispatch_claim=str(claim),
        _ui_worker_nonce=str(nonce),
        _ui_step_snapshot=[],
        _ui_test_case=case,
        _ui_base_url=project.base_url,
        _ui_cancel_check=lambda: (_ for _ in ()).throw(
            WorkbenchWorkerLeaseLost('case worker lease was lost')
        ),
    )
    view = TestCaseViewSet()
    view.request = request
    view.action = 'run'
    view.kwargs = {'pk': str(case.pk)}

    response = view.run(request, pk=str(case.pk))

    assert response.status_code == 500
    assert starts == []
    assert navigations == []
    readiness.assert_not_called()
    assert reconcile_stale_workbench_executions(limit=1, now=expired + timedelta(seconds=1)) == 1
    execution.refresh_from_db()
    assert execution.status == 'aborted'
    assert execution.failure_stage == 'worker_lost'
    assert 'external effects are unknown' in execution.error_message


@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_workbench_claim_metadata_outage_uses_unbounded_task_retry(monkeypatch, kind):
    from apps.ui_automation.worker_lease import WorkbenchClaimRecoveryUnavailable

    failure = WorkbenchClaimRecoveryUnavailable('could not release pending dispatch metadata')
    if kind == 'case':
        task = tasks.execute_workbench_case
        runner_path = 'apps.ui_automation.case_runner.execute_case_execution'
    else:
        task = tasks.execute_workbench_suite
        runner_path = 'apps.ui_automation.suite_runner.execute_suite_execution'
    monkeypatch.setattr(runner_path, Mock(side_effect=failure))
    retry = Mock(side_effect=RuntimeError('retry scheduled'))
    monkeypatch.setattr(task, 'retry', retry)

    with pytest.raises(RuntimeError, match='retry scheduled'):
        task.run(71, str(uuid.uuid4()))

    retry.assert_called_once()
    assert retry.call_args.kwargs['exc'] is failure
    assert retry.call_args.kwargs['countdown'] == tasks.WORKBENCH_CLAIM_RECOVERY_RETRY_SECONDS
    assert task.max_retries is None
