from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.app_automation.device_pool import acquire_device
from apps.app_automation.models import (
    AppDevice,
    AppProject,
    AppTestCase,
    AppTestExecution,
    AppTestSuite,
)


@pytest.fixture
def cancellation_fixture(db):
    owner = get_user_model().objects.create_user(username='cancel-suite-owner')
    project = AppProject.objects.create(name='cancel suite project', owner=owner)
    case = AppTestCase.objects.create(name='cancel suite case', project=project, created_by=owner)
    suite = AppTestSuite.objects.create(
        name='cancel suite', project=project, created_by=owner,
        execution_status='running', execution_result='passed',
        passed_count=3, failed_count=1,
    )
    device = AppDevice.objects.create(device_id='cancel-suite-device', status='online')
    client = APIClient()
    client.force_authenticate(user=owner)
    return SimpleNamespace(owner=owner, project=project, case=case, suite=suite,
                           device=device, client=client)


def _pending_suite_row(fixture, device, dispatch_id, *, release_lease, status='pending'):
    return AppTestExecution.objects.create(
        test_case=fixture.case,
        test_suite=fixture.suite,
        device=device,
        user=fixture.owner,
        status=status,
        task_id=dispatch_id,
        device_lease_generation=device.lease_generation,
        release_device_lease_on_stop=release_lease,
    )


@pytest.mark.django_db
def test_cross_project_shared_batch_is_rejected_before_any_cancellation_effect():
    requester = get_user_model().objects.create_user(username='cancel-project-a-owner')
    initiator = get_user_model().objects.create_user(username='cancel-shared-batch-owner')
    project_b_owner = get_user_model().objects.create_user(username='cancel-project-b-owner')
    project_a = AppProject.objects.create(name='cancel project A', owner=requester)
    project_b = AppProject.objects.create(name='cancel project B', owner=project_b_owner)
    project_a.members.add(initiator)
    project_b.members.add(initiator)
    case_a = AppTestCase.objects.create(name='A case', project=project_a, created_by=initiator)
    case_b = AppTestCase.objects.create(name='B case', project=project_b, created_by=initiator)
    device = AppDevice.objects.create(device_id='cancel-cross-project-device', status='online')
    device.lock(initiator)
    dispatch_id = 'shared-cross-project-dispatch'
    first = AppTestExecution.objects.create(
        test_case=case_a, device=device, user=initiator, task_id=dispatch_id,
        device_lease_generation=device.lease_generation, release_device_lease_on_stop=True,
    )
    second = AppTestExecution.objects.create(
        test_case=case_b, device=device, user=initiator, task_id=dispatch_id,
        device_lease_generation=device.lease_generation, release_device_lease_on_stop=True,
    )
    client = APIClient()
    client.force_authenticate(user=requester)

    with (
        patch('celery.current_app.control.revoke') as revoke,
        patch('apps.app_automation.execution_admission.release_device_if_idle') as release,
        patch('apps.app_automation.views.execution_views.send_execution_update') as update,
    ):
        response = client.post(f'/api/app-automation/executions/{first.pk}/stop/', {}, format='json')

    assert response.status_code == 403
    for execution in (first, second):
        execution.refresh_from_db()
        assert execution.status == 'pending'
        assert execution.stop_requested is False
        assert execution.finished_at is None
    device.refresh_from_db()
    assert device.locked_by_id == initiator.pk
    assert device.lease_generation == first.device_lease_generation
    revoke.assert_not_called()
    release.assert_not_called()
    update.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize('actor_mode', ['initiator', 'admin'])
def test_full_scope_actor_can_cancel_shared_cross_project_batch(actor_mode):
    initiator = get_user_model().objects.create_user(username=f'cancel-full-{actor_mode}')
    actor = initiator if actor_mode == 'initiator' else get_user_model().objects.create_user(
        username='cancel-full-admin-user', is_staff=True
    )
    project_a_owner = get_user_model().objects.create_user(username=f'cancel-full-a-{actor_mode}')
    project_b_owner = get_user_model().objects.create_user(username=f'cancel-full-b-{actor_mode}')
    project_a = AppProject.objects.create(name=f'full project A {actor_mode}', owner=project_a_owner)
    project_b = AppProject.objects.create(name=f'full project B {actor_mode}', owner=project_b_owner)
    project_a.members.add(initiator)
    project_b.members.add(initiator)
    case_a = AppTestCase.objects.create(name='full A case', project=project_a, created_by=initiator)
    case_b = AppTestCase.objects.create(name='full B case', project=project_b, created_by=initiator)
    device = AppDevice.objects.create(device_id=f'cancel-full-device-{actor_mode}', status='online')
    device.lock(initiator)
    dispatch_id = f'full-scope-dispatch-{actor_mode}'
    first = AppTestExecution.objects.create(
        test_case=case_a, device=device, user=initiator, task_id=dispatch_id,
        device_lease_generation=device.lease_generation, release_device_lease_on_stop=True,
    )
    second = AppTestExecution.objects.create(
        test_case=case_b, device=device, user=initiator, task_id=dispatch_id,
        device_lease_generation=device.lease_generation, release_device_lease_on_stop=True,
    )
    client = APIClient()
    client.force_authenticate(user=actor)

    with (
        patch('celery.current_app.control.revoke') as revoke,
        patch('apps.app_automation.views.execution_views.send_execution_update'),
    ):
        response = client.post(f'/api/app-automation/executions/{first.pk}/stop/', {}, format='json')

    assert response.status_code == 200
    for execution in (first, second):
        execution.refresh_from_db()
        assert execution.status == 'stopped'
    revoke.assert_called_once_with(dispatch_id, terminate=False)
    device.refresh_from_db()
    assert device.locked_by_id is None


@pytest.mark.django_db
@pytest.mark.parametrize(('auto_lease', 'should_release'), [(True, True), (False, False)])
def test_queued_suite_stop_settles_parent_and_late_worker_is_fenced(
    cancellation_fixture, auto_lease, should_release
):
    from apps.app_automation.tasks import execute_app_suite_task

    f = cancellation_fixture
    if auto_lease:
        device, _token = acquire_device(f.owner)
    else:
        device = f.device
        device.lock(f.owner)
    dispatch_id = f'queued-suite-{auto_lease}'
    first = _pending_suite_row(f, device, dispatch_id, release_lease=auto_lease)
    second = _pending_suite_row(f, device, dispatch_id, release_lease=auto_lease)
    f.suite.refresh_from_db()
    f.suite.execution_status = 'running'
    f.suite.execution_result = 'passed'
    f.suite.passed_count = 3
    f.suite.failed_count = 1
    f.suite.save(update_fields=['execution_status', 'execution_result', 'passed_count', 'failed_count'])

    with (
        patch('celery.current_app.control.revoke') as revoke,
        patch('apps.app_automation.views.execution_views.send_execution_update'),
    ):
        response = f.client.post(
            f'/api/app-automation/executions/{first.pk}/stop/', {}, format='json'
        )
        repeated = f.client.post(
            f'/api/app-automation/executions/{first.pk}/stop/', {}, format='json'
        )

    assert response.status_code == 200
    assert response.data['stopping'] is False
    assert repeated.status_code == 400
    revoke.assert_called_once_with(dispatch_id, terminate=False)
    for execution in (first, second):
        execution.refresh_from_db()
        assert execution.status == 'stopped'
        assert execution.finished_at is not None
    f.suite.refresh_from_db()
    assert f.suite.execution_status == 'completed'
    assert f.suite.execution_result == 'skipped'
    assert f.suite.passed_count == 0
    assert f.suite.failed_count == 0
    assert f.suite.last_run_at is not None
    device.refresh_from_db()
    assert (device.locked_by_id is None) is should_release

    with (
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as runner,
        patch('apps.app_automation.tasks.send_execution_update'),
    ):
        execute_app_suite_task.run(
            f.suite.pk,
            [first.pk, second.pk],
            lease_generation=first.device_lease_generation,
            dispatch_id=dispatch_id,
        )

    runner.assert_not_called()
    f.suite.refresh_from_db()
    assert f.suite.execution_status == 'completed'
    assert f.suite.execution_result == 'skipped'


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('first_outcome', 'expected_first_status', 'expected_result', 'expected_passed', 'expected_failed'),
    [
        ('passed', 'completed', 'unverified', 1, 0),
        ('failed', 'completed', 'failed', 0, 1),
        ('error', 'error', 'failed', 0, 1),
    ],
)
def test_cancel_after_completed_suite_case_aggregates_prior_outcome(
    cancellation_fixture, first_outcome, expected_first_status, expected_result,
    expected_passed, expected_failed,
):
    from apps.app_automation.tasks import execute_app_suite_task

    f = cancellation_fixture
    device, _token = acquire_device(f.owner)
    dispatch_id = 'partial-suite-dispatch'
    first = _pending_suite_row(f, device, dispatch_id, release_lease=True)
    second = _pending_suite_row(f, device, dispatch_id, release_lease=True)
    f.suite.execution_status = 'running'
    f.suite.execution_result = 'passed'
    f.suite.passed_count = 3
    f.suite.failed_count = 1
    f.suite.save(update_fields=['execution_status', 'execution_result', 'passed_count', 'failed_count'])
    stop_responses = []
    run_calls = []

    def run_case(*, execution_id, **_kwargs):
        run_calls.append(execution_id)
        if first_outcome == 'passed':
            return {
                'success': True,
                'exit_code': 0,
                'pytest_process_started': True,
                'pytest_process_completed': True,
                'test_results': {'total': 1, 'passed': 1, 'failed': 0},
            }
        if first_outcome == 'failed':
            return {
                'success': True,
                'exit_code': 0,
                'pytest_process_started': True,
                'pytest_process_completed': True,
                'test_results': {'total': 1, 'passed': 0, 'failed': 1},
            }
        return {
            'success': False,
            'exit_code': 1,
            'pytest_process_started': True,
            'pytest_process_completed': True,
            'test_results': {'total': 1, 'passed': 0, 'failed': 0},
        }

    def send_update(execution_id, **kwargs):
        if (
            execution_id == first.pk
            and kwargs.get('status') in {'completed', 'error'}
            and not stop_responses
        ):
            stop_responses.append(f.client.post(
                f'/api/app-automation/executions/{second.pk}/stop/', {}, format='json'
            ))

    with (
        patch('apps.app_automation.device_pool.Thread'),
        patch('apps.app_automation.tasks.send_execution_update', side_effect=send_update),
        patch('apps.app_automation.views.execution_views.send_execution_update'),
        patch('celery.current_app.control.revoke'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as runner,
    ):
        runner.return_value.run_tests.side_effect = run_case
        execute_app_suite_task.run(
            f.suite.pk,
            [first.pk, second.pk],
            lease_generation=device.lease_generation,
            dispatch_id=dispatch_id,
        )

    first.refresh_from_db()
    second.refresh_from_db()
    f.suite.refresh_from_db()
    assert run_calls == [first.pk]
    assert len(stop_responses) == 1
    assert stop_responses[0].status_code == 200
    assert stop_responses[0].data['stopping'] is False
    assert first.status == expected_first_status
    assert first.result == (None if first_outcome == 'error' else first_outcome)
    assert second.status == 'stopped'
    assert f.suite.execution_status == 'completed'
    assert f.suite.execution_result == expected_result
    assert f.suite.passed_count == expected_passed
    assert f.suite.failed_count == expected_failed


@pytest.mark.django_db
def test_running_suite_cancel_finalizer_keeps_completed_child_summary(cancellation_fixture):
    from apps.app_automation.tasks import execute_app_suite_task

    f = cancellation_fixture
    device, _token = acquire_device(f.owner)
    dispatch_id = 'running-partial-suite-dispatch'
    first = _pending_suite_row(f, device, dispatch_id, release_lease=True)
    second = _pending_suite_row(f, device, dispatch_id, release_lease=True)
    f.suite.execution_status = 'running'
    f.suite.execution_result = None
    f.suite.save(update_fields=['execution_status', 'execution_result'])

    def run_case(*, execution_id, cancel_check, **_kwargs):
        if execution_id == second.pk:
            response = f.client.post(
                f'/api/app-automation/executions/{second.pk}/stop/', {}, format='json'
            )
            assert response.status_code == 200
            assert response.data['stopping'] is True
            cancel_check()
        return {
            'success': True,
            'exit_code': 0,
            'pytest_process_started': True,
            'pytest_process_completed': True,
            'test_results': {'total': 1, 'passed': 1, 'failed': 0},
        }

    with (
        patch('apps.app_automation.device_pool.Thread'),
        patch('apps.app_automation.tasks.send_execution_update'),
        patch('apps.app_automation.views.execution_views.send_execution_update'),
        patch('celery.current_app.control.revoke'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as runner,
    ):
        runner.return_value.run_tests.side_effect = run_case
        execute_app_suite_task.run(
            f.suite.pk,
            [first.pk, second.pk],
            lease_generation=device.lease_generation,
            dispatch_id=dispatch_id,
        )

    first.refresh_from_db()
    second.refresh_from_db()
    f.suite.refresh_from_db()
    assert first.status == 'completed'
    assert first.result == 'passed'
    assert second.status == 'stopped'
    assert f.suite.execution_status == 'completed'
    assert f.suite.execution_result == 'unverified'
    assert f.suite.passed_count == 1
    assert f.suite.failed_count == 0


@pytest.mark.django_db
def test_claimed_old_worker_cannot_settle_suite_with_new_active_dispatch(cancellation_fixture):
    from apps.app_automation.tasks import execute_app_suite_task

    f = cancellation_fixture
    old_device, _token = acquire_device(f.owner)
    old_dispatch = 'claimed-old-suite-dispatch'
    old_row = _pending_suite_row(f, old_device, old_dispatch, release_lease=True)

    new_device = AppDevice.objects.create(device_id='claimed-new-suite-device', status='online')
    new_device.lock(f.owner)
    new_row = _pending_suite_row(
        f, new_device, 'claimed-new-suite-dispatch', release_lease=False,
        status='completed',
    )
    new_row.result = 'passed'
    new_row.save(update_fields=['result'])
    f.suite.execution_status = 'running'
    f.suite.execution_result = None
    f.suite.save(update_fields=['execution_status', 'execution_result'])

    with (
        patch('apps.app_automation.device_pool.Thread'),
        patch('apps.app_automation.tasks.send_execution_update'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as runner,
    ):
        runner.return_value.run_tests.return_value = {
            'success': True,
            'exit_code': 0,
            'test_results': {'total': 1, 'passed': 1, 'failed': 0},
        }
        execute_app_suite_task.run(
            f.suite.pk,
            [old_row.pk],
            lease_generation=old_row.device_lease_generation,
            dispatch_id=old_dispatch,
        )

    old_row.refresh_from_db()
    new_row.refresh_from_db()
    f.suite.refresh_from_db()
    assert old_row.status == 'completed'
    assert old_row.result == 'passed'
    assert new_row.status == 'completed'
    assert f.suite.execution_status == 'running'
    assert f.suite.execution_result is None

    from apps.app_automation.execution_admission import settle_app_suite_if_idle
    assert settle_app_suite_if_idle(
        f.suite.pk,
        execution_ids=[new_row.pk],
        dispatch_id='claimed-new-suite-dispatch',
        status='completed',
        result='passed',
        passed_count=1,
        failed_count=0,
    )
    f.suite.refresh_from_db()
    assert f.suite.execution_status == 'completed'
    assert f.suite.execution_result == 'passed'


@pytest.mark.django_db
def test_old_suite_cancellation_and_worker_cannot_overwrite_new_active_dispatch(cancellation_fixture):
    from apps.app_automation.tasks import execute_app_suite_task

    f = cancellation_fixture
    old_device, _token = acquire_device(f.owner)
    old_dispatch = 'old-suite-dispatch'
    old_row = _pending_suite_row(f, old_device, old_dispatch, release_lease=True)

    new_device = AppDevice.objects.create(device_id='new-suite-device', status='online')
    new_device.lock(f.owner)
    new_dispatch = 'new-suite-dispatch'
    new_row = _pending_suite_row(f, new_device, new_dispatch, release_lease=False)
    f.suite.execution_status = 'running'
    f.suite.execution_result = None
    f.suite.save(update_fields=['execution_status', 'execution_result'])

    with (
        patch('celery.current_app.control.revoke'),
        patch('apps.app_automation.views.execution_views.send_execution_update'),
    ):
        response = f.client.post(
            f'/api/app-automation/executions/{old_row.pk}/stop/', {}, format='json'
        )
    assert response.status_code == 200
    old_row.refresh_from_db()
    new_row.refresh_from_db()
    assert old_row.status == 'stopped'
    assert new_row.status == 'pending'
    f.suite.refresh_from_db()
    assert f.suite.execution_status == 'running'
    assert f.suite.execution_result is None
    old_device.refresh_from_db()
    new_device.refresh_from_db()
    assert old_device.locked_by_id is None
    assert new_device.locked_by_id == f.owner.pk

    with (
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as runner,
        patch('apps.app_automation.tasks.send_execution_update'),
    ):
        execute_app_suite_task.run(
            f.suite.pk,
            [old_row.pk],
            lease_generation=old_row.device_lease_generation,
            dispatch_id=old_dispatch,
        )

    runner.assert_not_called()
    f.suite.refresh_from_db()
    new_row.refresh_from_db()
    assert f.suite.execution_status == 'running'
    assert f.suite.execution_result is None
    assert new_row.status == 'pending'


@pytest.mark.django_db
def test_latest_suite_dispatch_finishes_before_older_active_dispatch(cancellation_fixture):
    from apps.app_automation.tasks import execute_app_suite_task

    f = cancellation_fixture
    old_device = f.device
    old_device.lock(f.owner)
    old_row = _pending_suite_row(f, old_device, 'overlap-old', release_lease=False)
    new_device = AppDevice.objects.create(device_id='overlap-new-device', status='online')
    new_device.lock(f.owner)
    new_case = AppTestCase.objects.create(
        name='overlap new case', project=f.project, created_by=f.owner,
    )
    new_row = AppTestExecution.objects.create(
        test_case=new_case, test_suite=f.suite, device=new_device, user=f.owner,
        task_id='overlap-new', device_lease_generation=new_device.lease_generation,
    )
    observed_new_completion = []

    def run_case(*, test_case_id, **_kwargs):
        if test_case_id == f.case.pk:
            execute_app_suite_task.run(
                f.suite.pk, [new_row.pk], dispatch_id='overlap-new',
                lease_generation=new_row.device_lease_generation,
            )
            old_row.refresh_from_db()
            f.suite.refresh_from_db()
            observed_new_completion.append((old_row.status, f.suite.execution_status))
            return {'success': False, 'exit_code': 1,
                    'test_results': {'total': 1, 'passed': 0, 'failed': 1}}
        return {'success': True, 'exit_code': 0,
                'test_results': {'total': 1, 'passed': 1, 'failed': 0}}

    with (
        patch('apps.app_automation.device_pool.Thread'),
        patch('apps.app_automation.tasks.send_execution_update'),
        patch('apps.app_automation.executors.test_executor.AppTestExecutor') as executor,
    ):
        executor.return_value.run_tests.side_effect = run_case
        execute_app_suite_task.run(
            f.suite.pk, [old_row.pk], dispatch_id='overlap-old',
            lease_generation=old_row.device_lease_generation,
        )

    old_row.refresh_from_db()
    new_row.refresh_from_db()
    f.suite.refresh_from_db()
    assert observed_new_completion == [('running', 'completed')]
    assert (old_row.status, old_row.result) == ('completed', 'failed')
    assert (new_row.status, new_row.result) == ('completed', 'passed')
    assert f.suite.execution_status == 'completed'
    assert f.suite.execution_result == 'passed'
    assert (f.suite.passed_count, f.suite.failed_count) == (1, 0)


@pytest.mark.django_db
def test_publish_failure_counts_only_the_current_suite_dispatch(cancellation_fixture):
    from apps.app_automation.tasks import settle_undispatched_app_executions

    f = cancellation_fixture
    f.device.lock(f.owner)
    old_passed = _pending_suite_row(
        f, f.device, 'prior-passed', release_lease=False, status='completed',
    )
    old_passed.result = 'passed'
    old_passed.save(update_fields=['result'])
    old_error = _pending_suite_row(
        f, f.device, 'prior-error', release_lease=False, status='error',
    )
    current = _pending_suite_row(f, f.device, 'publish-failure', release_lease=False)

    assert settle_undispatched_app_executions(
        [current.pk], 'synthetic publish failure', suite_id=f.suite.pk,
    ) == 1

    f.suite.refresh_from_db()
    current.refresh_from_db()
    old_passed.refresh_from_db()
    old_error.refresh_from_db()
    assert f.suite.execution_status == 'error'
    assert f.suite.execution_result is None
    assert (f.suite.passed_count, f.suite.failed_count) == (0, 1)
    assert current.status == 'error'
    assert (old_passed.status, old_passed.result) == ('completed', 'passed')
    assert old_error.status == 'error'
