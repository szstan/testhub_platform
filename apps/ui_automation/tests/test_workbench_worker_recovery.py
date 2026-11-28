from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import Mock
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.ui_automation import tasks
from apps.ui_automation.case_runner import claim_case_execution, persist_case_result
from apps.ui_automation.models import (
    TestCase as UiTestCase,
    TestCaseExecution as UiCaseExecution,
    TestExecution as UiSuiteExecution,
    TestSuite as UiTestSuite,
    TestSuiteTestCase as UiSuiteTestCase,
    UiProject,
)
from apps.ui_automation.suite_runner import _claim_execution, _finish_suite, execute_suite_execution
from apps.ui_automation.workbench import (
    create_case_execution,
    create_suite_execution,
    dispatch_execution,
    reconcile_pending_submissions,
    reconcile_stale_workbench_executions,
)
from apps.ui_automation.worker_lease import ExecutionWorkerLeaseHeartbeat

User = get_user_model()
OPTIONS = {'engine': 'playwright', 'browser': 'chrome', 'headless': True}
PRIVATE_FIELDS = {
    'dispatch_claim', 'dispatch_delivery_token', 'dispatch_delivery_expires_at',
    'dispatch_enqueued_at', 'worker_nonce', 'worker_heartbeat_at',
    'worker_lease_expires_at', 'cancel_requested_at', 'request_snapshot',
}


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


def _create_execution(kind, user, case, suite):
    if kind == 'case':
        return create_case_execution(case, user, OPTIONS)[0], UiCaseExecution
    return create_suite_execution(suite, user, OPTIONS)[0], UiSuiteExecution


def _claim(kind, execution, nonce):
    if kind == 'case':
        return claim_case_execution(execution.pk, str(execution.dispatch_claim), nonce)
    return _claim_execution(execution.pk, str(execution.dispatch_claim), nonce)


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_acknowledged_running_worker_loss_is_terminal_unknown_and_never_replayed(monkeypatch, kind):
    user, _project, case, suite = _context(f'ack-loss-{kind}')
    execution, model = _create_execution(kind, user, case, suite)
    task = tasks.execute_workbench_case if kind == 'case' else tasks.execute_workbench_suite
    publish = Mock()
    monkeypatch.setattr(task, 'apply_async', publish)
    acknowledged_at = timezone.now()
    assert dispatch_execution(kind, execution, now=acknowledged_at) is True

    nonce = uuid.uuid4()
    assert _claim(kind, execution, nonce) is not None
    execution.refresh_from_db()
    assert execution.dispatch_enqueued_at is not None
    assert execution.worker_nonce == nonce

    expired_at = timezone.now() + timedelta(minutes=2)
    model.objects.filter(pk=execution.pk).update(
        worker_lease_expires_at=expired_at - timedelta(seconds=1),
    )
    child = None
    if kind == 'suite':
        child = UiCaseExecution.objects.create(
            test_case=case,
            project=execution.project,
            test_suite=suite,
            parent_execution=execution,
            status='running',
            started_at=acknowledged_at,
            created_by=user,
        )

    assert reconcile_stale_workbench_executions(limit=20, now=expired_at) == 1
    execution.refresh_from_db()
    assert execution.status == ('aborted' if kind == 'case' else 'ABORTED')
    assert execution.failure_stage == 'worker_lost'
    assert 'external effects are unknown' in execution.error_message
    assert execution.finished_at == expired_at
    assert execution.worker_lease_expires_at is None
    if kind == 'suite':
        assert execution.result_data['summary']['external_effects_unknown'] is True
        assert execution.result_data['summary']['interrupted'] is True
        child.refresh_from_db()
        assert child.status == 'aborted'
        assert child.failure_stage == 'worker_lost'
        suite.refresh_from_db()
        assert suite.execution_status == 'failed'

    assert reconcile_pending_submissions(limit=20, now=expired_at) == 0
    assert reconcile_stale_workbench_executions(limit=20, now=expired_at + timedelta(hours=1)) == 0
    publish.assert_called_once()


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_acknowledged_pending_queue_wait_is_not_mistaken_for_worker_loss(monkeypatch, kind):
    user, _project, case, suite = _context(f'healthy-queue-{kind}')
    execution, _model = _create_execution(kind, user, case, suite)
    task = tasks.execute_workbench_case if kind == 'case' else tasks.execute_workbench_suite
    publish = Mock()
    monkeypatch.setattr(task, 'apply_async', publish)

    sent_at = timezone.now()
    assert dispatch_execution(kind, execution, now=sent_at) is True
    assert reconcile_stale_workbench_executions(
        limit=20, now=sent_at + timedelta(days=1),
    ) == 0
    execution.refresh_from_db()
    assert execution.status == ('pending' if kind == 'case' else 'PENDING')
    assert execution.worker_nonce is None
    assert execution.dispatch_enqueued_at is not None
    publish.assert_called_once()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_claim_commit_ack_loss_recovers_only_for_matching_invocation_nonce(monkeypatch, kind):
    user, _project, case, suite = _context(f'claim-ack-loss-{kind}')
    execution, _model = _create_execution(kind, user, case, suite)
    runner_module = (
        __import__('apps.ui_automation.case_runner', fromlist=['claim_case_execution'])
        if kind == 'case'
        else __import__('apps.ui_automation.suite_runner', fromlist=['_claim_execution'])
    )
    original_atomic = runner_module.transaction.atomic
    raised_after_commit = False

    @contextmanager
    def commit_then_lose_ack(*args, **kwargs):
        nonlocal raised_after_commit
        with original_atomic(*args, **kwargs):
            yield
        if not raised_after_commit:
            raised_after_commit = True
            raise RuntimeError('database commit acknowledgement lost')

    monkeypatch.setattr(runner_module.transaction, 'atomic', commit_then_lose_ack)
    nonce = uuid.uuid4()
    claimed = _claim(kind, execution, nonce)
    assert claimed is not None
    assert claimed.worker_nonce == nonce
    assert raised_after_commit is True

    replacement_nonce = uuid.uuid4()
    assert _claim(kind, execution, replacement_nonce) is None
    execution.refresh_from_db()
    assert execution.worker_nonce == nonce

    if kind == 'case':
        assert persist_case_result(
            execution.pk,
            str(execution.dispatch_claim),
            status='passed',
            worker_nonce=nonce,
        ) is True
    else:
        assert _finish_suite(
            execution.pk,
            str(execution.dispatch_claim),
            status='SUCCESS',
            worker_nonce=nonce,
        ) is True
    execution.refresh_from_db()
    assert execution.status == ('passed' if kind == 'case' else 'SUCCESS')


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_old_worker_cannot_write_after_expired_lease_is_fenced(kind):
    user, _project, case, suite = _context(f'late-worker-{kind}')
    execution, model = _create_execution(kind, user, case, suite)
    old_nonce = uuid.uuid4()
    assert _claim(kind, execution, old_nonce) is not None
    expired_at = timezone.now() + timedelta(minutes=1)
    model.objects.filter(pk=execution.pk).update(
        worker_lease_expires_at=expired_at - timedelta(seconds=1),
    )
    assert reconcile_stale_workbench_executions(limit=1, now=expired_at) == 1

    if kind == 'case':
        assert persist_case_result(
            execution.pk,
            str(execution.dispatch_claim),
            status='passed',
            fields={'execution_logs': 'late success'},
            worker_nonce=old_nonce,
        ) is False
    else:
        assert _finish_suite(
            execution.pk,
            str(execution.dispatch_claim),
            status='SUCCESS',
            worker_nonce=old_nonce,
        ) is False
    execution.refresh_from_db()
    assert execution.status == ('aborted' if kind == 'case' else 'ABORTED')
    assert 'unknown' in execution.error_message


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_each_actual_celery_invocation_gets_a_fresh_nonce(monkeypatch, kind):
    observed = []
    if kind == 'case':
        monkeypatch.setattr(
            'apps.ui_automation.case_runner.execute_case_execution',
            lambda execution_id, claim_token, worker_nonce: observed.append(worker_nonce) or False,
        )
        task = tasks.execute_workbench_case
    else:
        monkeypatch.setattr(
            'apps.ui_automation.suite_runner.execute_suite_execution',
            lambda execution_id, claim_token, worker_nonce: observed.append(worker_nonce) or False,
        )
        task = tasks.execute_workbench_suite

    same_delivery_args = (2718, str(uuid.uuid4()))
    task.push_request(id='same-celery-delivery')
    try:
        assert task.request.id == 'same-celery-delivery'
        assert task.run(*same_delivery_args) is False
        assert task.request.id == 'same-celery-delivery'
        assert task.run(*same_delivery_args) is False
        assert task.request.id == 'same-celery-delivery'
    finally:
        task.pop_request()
    assert len(observed) == 2
    assert all(isinstance(value, uuid.UUID) for value in observed)
    assert observed[0] != observed[1]
    assert task.acks_late is True
    assert task.reject_on_worker_lost is True


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_worker_nonce_lease_fields_are_not_serialized(kind):
    from apps.ui_automation.serializers import TestCaseExecutionSerializer, TestExecutionSerializer

    user, _project, case, suite = _context(f'private-worker-fields-{kind}')
    execution, _model = _create_execution(kind, user, case, suite)
    serializer = TestCaseExecutionSerializer if kind == 'case' else TestExecutionSerializer
    assert not PRIVATE_FIELDS & set(serializer(execution).data)


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_heartbeat_renews_only_a_live_matching_worker_lease(kind):
    user, _project, case, suite = _context(f'heartbeat-{kind}')
    execution, model = _create_execution(kind, user, case, suite)
    nonce = uuid.uuid4()
    assert _claim(kind, execution, nonce) is not None
    execution.refresh_from_db()
    heartbeat = ExecutionWorkerLeaseHeartbeat(
        model, execution.pk, execution.dispatch_claim, nonce, lease_seconds=90,
    )
    initial_expiry = execution.worker_lease_expires_at
    initial_heartbeat = execution.worker_heartbeat_at
    assert heartbeat._heartbeat() is True
    execution.refresh_from_db()
    assert execution.worker_heartbeat_at >= initial_heartbeat
    assert execution.worker_lease_expires_at > initial_expiry

    model.objects.filter(pk=execution.pk).update(
        worker_lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    assert heartbeat._heartbeat() is False
