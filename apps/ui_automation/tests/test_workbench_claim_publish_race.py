"""A failed claim must stay recoverable when the old publisher ACK arrives late."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction

from apps.ui_automation import case_runner, suite_runner, tasks
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


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('kind', ['case', 'suite'])
def test_claim_rollback_before_publish_ack_cannot_mark_pending_delivery_as_enqueued(monkeypatch, kind):
    user = get_user_model().objects.create_user(username=f'claim-before-publish-ack-{kind}')
    project = UiProject.objects.create(name=f'publish ack {kind}', owner=user)
    case = UiTestCase.objects.create(project=project, name='case', created_by=user)
    options = {'engine': 'playwright', 'browser': 'chrome', 'headless': True}
    if kind == 'case':
        execution = create_case_execution(case, user, options)[0]
        model = UiCaseExecution
        task = tasks.execute_workbench_case
        runner = case_runner
        pending = 'pending'
    else:
        suite = UiTestSuite.objects.create(project=project, name='suite')
        UiSuiteTestCase.objects.create(test_suite=suite, test_case=case)
        execution = create_suite_execution(suite, user, options)[0]
        model = UiSuiteExecution
        task = tasks.execute_workbench_suite
        runner = suite_runner
        pending = 'PENDING'

    original_atomic = transaction.atomic

    @contextmanager
    def rollback_after_update(*args, **kwargs):
        with original_atomic(*args, **kwargs):
            yield
            raise RuntimeError('claim rolled back before publisher received ACK')

    def deliver_before_publisher_ack(*, args, retry):
        monkeypatch.setattr(runner, 'transaction', SimpleNamespace(atomic=rollback_after_update))
        assert task.run(*args) is False
        row = model.objects.get(pk=execution.pk)
        assert row.status == pending
        assert row.worker_nonce is None
        assert row.dispatch_enqueued_at is None
        assert row.dispatch_delivery_token is None

    publish = Mock(side_effect=deliver_before_publisher_ack)
    monkeypatch.setattr(task, 'apply_async', publish)
    assert dispatch_execution(kind, execution) is True
    execution.refresh_from_db()
    assert execution.status == pending
    assert execution.worker_nonce is None
    assert execution.dispatch_enqueued_at is None
    assert execution.dispatch_delivery_token is None

    monkeypatch.setattr(runner, 'transaction', SimpleNamespace(atomic=original_atomic))
    publish.side_effect = None
    assert reconcile_pending_submissions(limit=1) == 1
    assert publish.call_count == 2
    execution.refresh_from_db()
    assert execution.dispatch_enqueued_at is not None
