import uuid
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ui_automation import tasks
from apps.ui_automation.case_runner import execute_case_execution, persist_case_result
from apps.ui_automation.models import (
    Element,
    LocatorStrategy,
    TestCase as UiTestCase,
    TestCaseExecution as UiTestCaseExecution,
    TestCaseStep as UiTestCaseStep,
    TestExecution as UiTestExecution,
    TestSuite as UiTestSuite,
    TestSuiteTestCase as UiTestSuiteTestCase,
    UiExecutionSubmission,
    UiProject,
)
from apps.ui_automation.workbench import create_case_execution

User = get_user_model()


def _context(name='workbench'):
    user = User.objects.create_user(username=f'{name}-owner')
    project = UiProject.objects.create(
        name=name,
        base_url=f'https://{name}.example.test/base',
        owner=user,
    )
    case = UiTestCase.objects.create(project=project, name=f'{name}-case', created_by=user)
    return user, project, case


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _case_payload(project, request_id, **overrides):
    return {
        'async': True,
        'client_request_id': str(request_id),
        'project_id': project.pk,
        'engine': 'playwright',
        'browser': 'chrome',
        'headless': True,
        **overrides,
    }


@pytest.mark.django_db
def test_case_submission_persists_snapshot_before_dispatch_and_replays_by_actor_uuid(monkeypatch):
    user, project, case = _context('case-snapshot')
    strategy = LocatorStrategy.objects.create(name='css')
    element = Element.objects.create(
        project=project,
        name='submit-button',
        locator_strategy=strategy,
        locator_value='#submit',
        created_by=user,
    )
    step = UiTestCaseStep.objects.create(
        test_case=case,
        step_number=1,
        action_type='click',
        element=element,
        input_value='snapshot-only-value',
    )
    request_id = uuid.uuid4()
    dispatch = Mock()

    def dispatch_after_persist(*, args, **options):
        assert options.get('retry') is False
        execution_id, claim = args
        stored = UiTestCaseExecution.objects.get(pk=execution_id)
        assert stored.status == 'pending'
        assert stored.dispatch_claim == uuid.UUID(claim)
        assert stored.request_snapshot['steps'][0]['id'] == step.pk
        dispatch(args=args)

    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', dispatch_after_persist)
    client = _client(user)
    url = f'/api/ui-automation/test-cases/{case.pk}/run/'
    payload = _case_payload(project, request_id)

    accepted = client.post(url, payload, format='json')
    assert accepted.status_code == 202
    assert accepted.data['accepted'] is True
    execution = UiTestCaseExecution.objects.get(pk=accepted.data['execution_id'])
    assert execution.request_snapshot == {
        'version': 1,
        'config': {'engine': 'playwright', 'browser': 'chrome', 'headless': True},
        'case': {'id': case.pk, 'name': case.name, 'project_id': project.pk},
        'base_url': project.base_url,
        'steps': [{
            'id': step.pk,
            'step_number': 1,
            'action_type': 'click',
            'description': '',
            'input_value': 'snapshot-only-value',
            'wait_time': 1000,
            'assert_type': '',
            'assert_value': '',
            'element_data': {
                'id': element.pk,
                'name': element.name,
                'locator_strategy': 'css',
                'locator_value': '#submit',
                'wait_timeout': 5,
                'force_action': False,
            },
            'element': {
                'id': element.pk,
                'name': element.name,
                'locator_value': '#submit',
                'locator_strategy': 'css',
            },
        }],
    }
    assert dispatch.call_count == 1

    replay = client.post(url, payload, format='json')
    assert replay.status_code == 202
    assert replay.data['replayed'] is True
    assert replay.data['execution_id'] == execution.pk
    assert UiTestCaseExecution.objects.filter(test_case=case).count() == 1

    found = client.get(
        f'/api/ui-automation/test-cases/{case.pk}/submission/',
        {'client_request_id': str(request_id)},
    )
    assert found.status_code == 200
    assert found.data['execution_id'] == execution.pk
    assert 'request_snapshot' not in found.data

    member = User.objects.create_user(username='case-snapshot-member')
    project.members.add(member)
    member_result = _client(member).get(
        f'/api/ui-automation/test-cases/{case.pk}/submission/',
        {'client_request_id': str(request_id)},
    )
    assert member_result.status_code == 404

    other_case = UiTestCase.objects.create(project=project, name='another-case', created_by=user)
    conflict = client.post(
        f'/api/ui-automation/test-cases/{other_case.pk}/run/', payload, format='json'
    )
    assert conflict.status_code == 409
    assert conflict.data['original_execution_id'] == execution.pk
    assert UiExecutionSubmission.objects.filter(actor=user, client_request_id=request_id).count() == 1
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            UiExecutionSubmission.objects.create(
                actor=user,
                client_request_id=request_id,
                kind='case',
                resource_id=case.pk,
                request_fingerprint='duplicate',
            )
    for fingerprint in ('nullable-a', 'nullable-b'):
        UiExecutionSubmission.objects.create(
            actor=user,
            client_request_id=None,
            kind='case',
            resource_id=case.pk,
            request_fingerprint=fingerprint,
        )


@pytest.mark.django_db
def test_case_async_requires_uuid_and_rejects_cross_project_step_elements():
    user, project, case = _context('cross-project-step')
    client = _client(user)
    missing_id = client.post(
        f'/api/ui-automation/test-cases/{case.pk}/run/',
        _case_payload(project, ''),
        format='json',
    )
    assert missing_id.status_code == 400
    assert UiTestCaseExecution.objects.count() == 0

    other_owner = User.objects.create_user(username='cross-project-element-owner')
    other_project = UiProject.objects.create(
        name='foreign-element-project',
        base_url='https://foreign.example.test',
        owner=other_owner,
    )
    strategy = LocatorStrategy.objects.create(name='css')
    foreign_element = Element.objects.create(
        project=other_project,
        name='foreign-element',
        locator_strategy=strategy,
        locator_value='#foreign',
        created_by=other_owner,
    )
    UiTestCaseStep.objects.create(
        test_case=case, step_number=1, action_type='click', element=foreign_element,
    )
    rejected = client.post(
        f'/api/ui-automation/test-cases/{case.pk}/run/',
        _case_payload(project, uuid.uuid4()),
        format='json',
    )
    assert rejected.status_code == 400
    assert rejected.data['failure_stage'] == 'preflight'
    assert UiTestCaseExecution.objects.count() == 0


@pytest.mark.django_db
def test_suite_submission_is_persisted_idempotently_and_requires_uuid(monkeypatch):
    user, project, case = _context('suite-submission')
    suite = UiTestSuite.objects.create(project=project, name='suite')
    UiTestSuiteTestCase.objects.create(test_suite=suite, test_case=case)
    request_id = uuid.uuid4()
    dispatch = Mock()

    def dispatch_after_persist(*, args, **options):
        assert options.get('retry') is False
        execution_id, claim = args
        stored = UiTestExecution.objects.get(pk=execution_id)
        assert stored.status == 'PENDING'
        assert stored.dispatch_claim == uuid.UUID(claim)
        assert stored.request_snapshot['cases'][0]['id'] == case.pk
        dispatch(args=args)

    monkeypatch.setattr(tasks.execute_workbench_suite, 'apply_async', dispatch_after_persist)
    client = _client(user)
    url = f'/api/ui-automation/test-suites/{suite.pk}/run_suite/'
    no_id = client.post(url, {}, format='json')
    assert no_id.status_code == 400

    payload = {
        'client_request_id': str(request_id),
        'engine': 'playwright',
        'browser': 'chrome',
        'headless': True,
    }
    accepted = client.post(url, payload, format='json')
    assert accepted.status_code == 202
    execution_id = accepted.data['execution_id']
    replay = client.post(url, payload, format='json')
    assert replay.status_code == 202
    assert replay.data['replayed'] is True
    assert replay.data['execution_id'] == execution_id
    assert UiTestExecution.objects.filter(test_suite=suite).count() == 1
    assert dispatch.call_count == 1

    found = client.get(
        f'/api/ui-automation/test-suites/{suite.pk}/submission/',
        {'client_request_id': str(request_id)},
    )
    assert found.status_code == 200
    assert found.data['execution_id'] == execution_id
    assert 'request_snapshot' not in found.data


@pytest.mark.django_db
def test_broker_failure_keeps_pending_submission_for_lease_recovery(monkeypatch):
    user, project, case = _context('case-dispatch-failure')
    request_id = uuid.uuid4()
    dispatch = Mock(side_effect=[RuntimeError('fake broker failure'), None])
    monkeypatch.setattr(tasks.execute_workbench_case, 'apply_async', dispatch)
    client = _client(user)
    url = f'/api/ui-automation/test-cases/{case.pk}/run/'
    payload = _case_payload(project, request_id)

    failed = client.post(url, payload, format='json')
    assert failed.status_code == 503
    execution = UiTestCaseExecution.objects.get(pk=failed.data['execution_id'])
    assert execution.status == 'pending'
    assert execution.failure_stage == ''
    assert execution.finished_at is None
    assert execution.dispatch_delivery_token is not None
    assert execution.dispatch_delivery_expires_at > timezone.now()

    replay = client.post(url, payload, format='json')
    assert replay.status_code == 202
    assert replay.data['execution_id'] == execution.pk
    assert replay.data['replayed'] is True
    dispatch.assert_called_once()

    UiTestCaseExecution.objects.filter(pk=execution.pk).update(
        dispatch_delivery_expires_at=timezone.now() - timedelta(seconds=1),
    )
    from apps.ui_automation.workbench import reconcile_pending_submissions
    assert reconcile_pending_submissions(limit=1) == 1
    execution.refresh_from_db()
    assert execution.status == 'pending'
    assert execution.dispatch_enqueued_at is not None
    assert execution.dispatch_delivery_token is None
    assert dispatch.call_count == 2


@pytest.mark.django_db
def test_case_abort_route_fences_late_worker_saves_and_hides_control_snapshot():
    user, project, case = _context('case-abort')
    claim = uuid.uuid4()
    execution = UiTestCaseExecution.objects.create(
        test_case=case,
        project=project,
        created_by=user,
        status='running',
        dispatch_claim=claim,
        request_snapshot={'config': {}, 'steps': [], 'private': 'snapshot-only-value'},
    )
    client = _client(user)
    path = f'/api/ui-automation/test-case-executions/{execution.pk}/'

    fetched = client.get(path)
    assert fetched.status_code == 200
    assert not {
        'dispatch_claim', 'dispatch_delivery_token', 'dispatch_delivery_expires_at',
        'dispatch_enqueued_at', 'cancel_requested_at', 'request_snapshot',
    } & set(fetched.data)
    patched = client.patch(path, {
        'dispatch_claim': str(uuid.uuid4()),
        'cancel_requested_at': timezone.now().isoformat(),
        'request_snapshot': {'private': 'injected-value'},
        'failure_stage': 'worker',
    }, format='json')
    assert patched.status_code == 400
    assert 'status' not in patched.data
    execution.refresh_from_db()
    assert execution.dispatch_claim == claim
    assert execution.cancel_requested_at is None
    assert execution.request_snapshot['private'] == 'snapshot-only-value'

    aborted = client.post(f'{path}abort/')
    assert aborted.status_code == 200
    assert aborted.data['status'] == 'aborted'
    assert persist_case_result(
        execution.pk,
        str(claim),
        status='passed',
        fields={'execution_logs': 'late save must be ignored'},
    ) is False
    execution.refresh_from_db()
    assert execution.status == 'aborted'
    assert execution.error_message == '执行已由用户停止。'

    outsider = User.objects.create_user(username='case-abort-outsider')
    denied = _client(outsider).post(f'{path}abort/')
    assert denied.status_code == 404


@pytest.mark.django_db
def test_duplicate_case_worker_claim_does_not_replay_terminal_execution(monkeypatch):
    user, _project, case = _context('case-claim')
    options = {'engine': 'playwright', 'browser': 'chrome', 'headless': True}
    execution, replayed, conflict, _execution_id = create_case_execution(case, user, options)
    assert not replayed and not conflict
    called = []

    def fake_run(_view, request, pk=None):
        called.append(pk)
        assert persist_case_result(
            request._ui_execution.pk,
            request._ui_dispatch_claim,
            status='passed',
            fields={'finished_at': timezone.now()},
        )

    monkeypatch.setattr('apps.ui_automation.views.TestCaseViewSet.run', fake_run)
    claim = str(execution.dispatch_claim)
    assert execute_case_execution(execution.pk, claim) is True
    assert execute_case_execution(execution.pk, claim) is False
    execution.refresh_from_db()
    assert execution.status == 'passed'
    assert len(called) == 1


@pytest.mark.django_db
def test_execution_readiness_reports_policy_and_restricted_webkit(monkeypatch):
    from apps.ui_automation.playwright_engine import PlaywrightTestEngine
    from apps.ui_automation.selenium_engine import SeleniumTestEngine

    user, project, _case = _context('readiness')
    monkeypatch.setattr('apps.ui_automation.network_policy.validate_ui_outbound_url', lambda url: url)
    monkeypatch.setattr(
        PlaywrightTestEngine,
        'check_execution_environment_sync',
        staticmethod(lambda browser: (browser not in {'safari', 'webkit'}, 'WebKit restricted' if browser in {'safari', 'webkit'} else 'ready')),
    )
    monkeypatch.setattr(
        SeleniumTestEngine,
        'check_execution_environment',
        staticmethod(lambda browser: (browser != 'safari', 'Safari restricted' if browser == 'safari' else 'ready')),
    )
    response = _client(user).get(
        f'/api/ui-automation/projects/{project.pk}/execution-readiness/'
    )
    assert response.status_code == 200
    assert response.data['project_id'] == project.pk
    assert response.data['recommended_headless'] is True
    assert response.data['base_url_ready'] is True
    playwright = next(item for item in response.data['engines'] if item['value'] == 'playwright')
    assert next(item for item in playwright['browsers'] if item['value'] == 'safari')['available'] is False
    assert all('available' in item for engine in response.data['engines'] for item in engine['browsers'])
