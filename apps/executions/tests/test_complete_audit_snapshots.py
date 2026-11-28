from datetime import timedelta
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.executions.models import (
    TestPlan as Plan, TestRun as Run, TestRunCase as RunCase,
    TestRunCaseHistory as History,
)
from apps.projects.models import Project
from apps.testcases.models import TestCase as Case
from apps.users.models import User


pytestmark = pytest.mark.django_db


@pytest.fixture
def audit_context():
    owner = User.objects.create_user(username='complete-audit-owner')
    project = Project.objects.create(name='Complete history project', owner=owner)
    case = Case.objects.create(project=project, author=owner, title='Snapshot case')
    plan = Plan.objects.create(name='Snapshot plan', creator=owner)
    plan.projects.add(project)
    run = Run.objects.create(name='Snapshot run', test_plan=plan, project=project, creator=owner, assignee=owner)
    row = RunCase.objects.create(test_run=run, testcase=case)
    client = APIClient()
    client.force_authenticate(owner)
    return owner, project, plan, run, row, client


def test_history_captures_defects_and_duration_and_preserves_previous_snapshot(audit_context):
    _owner, _project, _plan, _run, row, client = audit_context
    url = f'/api/executions/run_cases/{row.pk}/'
    first = client.patch(url, {'defects': ['BUG-1'], 'elapsed_time': '00:00:05'}, format='json')
    assert first.status_code == 200, first.data
    earlier = row.history.get()
    assert earlier.defects == ['BUG-1']
    assert earlier.elapsed_time == timedelta(seconds=5)

    changed_defects = client.patch(url, {'defects': ['BUG-2']}, format='json')
    assert changed_defects.status_code == 200, changed_defects.data
    changed_duration = client.patch(url, {'elapsed_time': '00:00:02'}, format='json')
    assert changed_duration.status_code == 200, changed_duration.data
    latest = row.history.order_by('-pk').first()
    assert latest.defects == ['BUG-2']
    assert latest.elapsed_time == timedelta(seconds=2)
    earlier.refresh_from_db()
    assert earlier.defects == ['BUG-1']
    assert earlier.elapsed_time == timedelta(seconds=5)

    history = client.get(url + 'history/')
    assert history.status_code == 200
    newest = next(item for item in history.data if item['id'] == latest.pk)
    assert newest['defects'] == ['BUG-2']
    assert newest['elapsed_time'] == changed_duration.data['elapsed_time']


def test_complete_snapshot_failure_rolls_back_defects_and_duration(audit_context):
    _owner, _project, _plan, _run, row, client = audit_context
    with patch('apps.executions.views.TestRunCaseHistory.objects.create', side_effect=RuntimeError('snapshot unavailable')):
        with pytest.raises(RuntimeError, match='snapshot unavailable'):
            client.patch(f'/api/executions/run_cases/{row.pk}/', {
                'defects': ['BUG-ROLLBACK'], 'elapsed_time': '00:00:05',
            }, format='json')
    row.refresh_from_db()
    assert row.defects == []
    assert row.elapsed_time is None
    assert row.executed_by_id is None
    assert not row.history.exists()


def test_zero_duration_on_create_is_an_explicit_audited_result(audit_context):
    owner, project, _plan, run, _row, client = audit_context
    case = Case.objects.create(project=project, author=owner, title='Zero-duration case')
    response = client.post('/api/executions/run_cases/', {
        'test_run': run.pk, 'testcase': case.pk, 'elapsed_time': '00:00:00',
    }, format='json')
    assert response.status_code == 201, response.data
    snapshot = History.objects.get(run_case_id=response.data['id'])
    assert snapshot.elapsed_time == timedelta(0)
    assert snapshot.defects == []
    assert snapshot.executed_by_id == owner.pk


def test_preexisting_history_values_remain_unknown_instead_of_copying_live_state(audit_context):
    owner, _project, _plan, _run, row, client = audit_context
    legacy = History.objects.create(run_case=row, status='passed', executed_by=owner)
    row.defects = ['BUG-LATER']
    row.elapsed_time = timedelta(seconds=20)
    row.save(update_fields=['defects', 'elapsed_time'])
    response = client.get(f'/api/executions/run_cases/{row.pk}/history/')
    assert response.status_code == 200
    saved = next(item for item in response.data if item['id'] == legacy.pk)
    assert saved['defects'] is None
    assert saved['elapsed_time'] is None


@pytest.mark.parametrize('operation', ['update', 'delete'])
def test_plan_missing_during_lock_lookup_returns_not_found(audit_context, operation):
    _owner, _project, plan, run, _row, client = audit_context
    # The scoped GET has its object; the lock lookup models a competing delete.
    with patch('apps.executions.views.TestPlan.objects.select_for_update', return_value=Plan.objects.none()):
        if operation == 'update':
            response = client.patch(f'/api/executions/plans/{plan.pk}/', {'name': 'Lost update'}, format='json')
        else:
            response = client.delete(f'/api/executions/plans/{plan.pk}/')
    assert response.status_code == 404, response.data
    plan.refresh_from_db()
    assert plan.name == 'Snapshot plan'
    assert Run.objects.filter(pk=run.pk).exists()
