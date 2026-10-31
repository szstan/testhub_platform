from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.api_testing.models import ApiCollection, ApiProject, ApiRequest, TestSuite as ApiTestSuite
from apps.app_automation.models import AppProject, AppTestCase, AppTestSuite
from apps.executions.adapters import (
    ApiExecutorAdapter,
    AppExecutorAdapter,
    PerformanceExecutorAdapter,
    UiExecutorAdapter,
)
from apps.executions.models import ExecutionJob, ExecutionSourceBinding
from apps.executions.source_workers import (
    execute_api_source,
    execute_app_source,
    execute_performance_source,
    execute_ui_source,
)
from apps.perf_testing.models import PerfProject, PerfScenario
from apps.projects.models import Project
from apps.ui_automation.models import (
    TestCase as UiTestCase,
    TestSuite as UiTestSuite,
    UiProject,
)
from apps.users.models import User


WORKERS = {
    'api': (ApiExecutorAdapter, execute_api_source),
    'ui': (UiExecutorAdapter, execute_ui_source),
    'app': (AppExecutorAdapter, execute_app_source),
    'performance': (PerformanceExecutorAdapter, execute_performance_source),
}


@pytest.fixture
def boundary(db):
    actor = User.objects.create_user(username='source-boundary-actor', password='pass')
    other_owner = User.objects.create_user(username='source-boundary-other', password='pass')
    admin = User.objects.create_user(username='source-boundary-admin', password='pass', is_staff=True)
    canonical = Project.objects.create(id=97001, name='Canonical source project', owner=actor)
    canonical_other = Project.objects.create(id=97002, name='Reparent target project', owner=actor)
    return {
        'actor': actor,
        'other_owner': other_owner,
        'admin': admin,
        'canonical': canonical,
        'canonical_other': canonical_other,
    }


def make_native_source(source_type, owner, *, project_id=None, member=None, suffix='source', kind=None):
    project_kwargs = {'name': f'{source_type}-{suffix}', 'owner': owner}
    if project_id is not None:
        project_kwargs['id'] = project_id

    if source_type == 'api':
        project = ApiProject.objects.create(
            **project_kwargs, project_type='HTTP', status='IN_PROGRESS'
        )
        if kind == 'suite':
            source = ApiTestSuite.objects.create(
                project=project, name=f'{suffix}-suite', created_by=owner
            )
        else:
            collection = ApiCollection.objects.create(project=project, name=f'{suffix}-collection')
            source = ApiRequest.objects.create(
                collection=collection,
                name=f'{suffix}-request',
                url='https://example.test',
                created_by=owner,
            )
            kind = 'request'
    elif source_type == 'ui':
        project = UiProject.objects.create(
            **project_kwargs, base_url='https://example.test'
        )
        if kind == 'suite':
            source = UiTestSuite.objects.create(project=project, name=f'{suffix}-suite')
        else:
            source = UiTestCase.objects.create(
                project=project, name=f'{suffix}-case', created_by=owner
            )
            kind = 'case'
    elif source_type == 'app':
        project = AppProject.objects.create(**project_kwargs)
        if kind == 'suite':
            source = AppTestSuite.objects.create(
                project=project, name=f'{suffix}-suite', created_by=owner
            )
        else:
            source = AppTestCase.objects.create(
                project=project, name=f'{suffix}-case', created_by=owner
            )
            kind = 'case'
    elif source_type == 'performance':
        project = PerfProject.objects.create(**project_kwargs)
        source = PerfScenario.objects.create(
            project=project, name=f'{suffix}-scenario', created_by=owner
        )
        kind = 'scenario'
    else:
        raise AssertionError(source_type)

    if member is not None:
        project.members.add(member)
    return project, source, f'{source_type}:{kind}:{source.pk}'


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def post_binding(user, canonical_project, source_type, source_id, source_project_id, metadata=None):
    return client_for(user).post(
        '/api/executions/source-bindings/',
        {
            'project': canonical_project.pk,
            'source_type': source_type,
            'source_id': source_id,
            'source_project_id': str(source_project_id),
            'metadata': metadata or {},
        },
        format='json',
    )


_UNSET = object()


def create_worker_job(boundary, source_type, native_project, source_id, *, metadata=None,
                      source_project_id=None, requested_by=_UNSET, created_by=_UNSET):
    actor = boundary['actor']
    binding = ExecutionSourceBinding.objects.create(
        project=boundary['canonical'],
        source_type=source_type,
        source_id=source_id,
        source_project_id=str(source_project_id if source_project_id is not None else native_project.pk),
        metadata=metadata or {},
        created_by=actor if created_by is _UNSET else created_by,
    )
    job = ExecutionJob.objects.create(
        project=boundary['canonical'],
        source_type=source_type,
        source_id=source_id,
        requested_by=actor if requested_by is _UNSET else requested_by,
    )
    adapter_class, worker = WORKERS[source_type]
    receipt = adapter_class(dispatcher=lambda *_: 'source-boundary-test-dispatch').submit(job)
    attempt = job.attempts.get(pk=receipt.attempt_id)
    return binding, job, attempt, worker


@pytest.mark.parametrize(
    ('source_type', 'kind'),
    [
        ('api', 'suite'), ('api', 'request'),
        ('ui', 'suite'), ('ui', 'case'),
        ('app', 'suite'), ('app', 'case'),
        ('performance', 'scenario'),
    ],
)
def test_binding_resolves_each_supported_native_model_kind(boundary, source_type, kind):
    native_project, _source, source_id = make_native_source(
        source_type, boundary['other_owner'], project_id=boundary['canonical'].pk + 90,
        member=boundary['actor'], suffix=f'kind-{source_type}-{kind}', kind=kind,
    )
    response = post_binding(
        boundary['actor'], boundary['canonical'], source_type, source_id, native_project.pk,
        metadata={'approved': True} if source_type == 'performance' else {},
    )
    assert response.status_code == 201, response.data


def test_create_rejects_foreign_native_projects_even_when_numeric_ids_match(boundary):
    actor, canonical, outsider = boundary['actor'], boundary['canonical'], boundary['other_owner']

    for source_type in WORKERS:
        native_project, source, source_id = make_native_source(
            source_type,
            outsider,
            project_id=canonical.pk,
            suffix='foreign',
        )
        assert native_project.pk == canonical.pk
        response = post_binding(
            actor, canonical, source_type, source_id, native_project.pk,
            metadata={'approved': True} if source_type == 'performance' else {},
        )
        assert response.status_code == 403, (source_type, response.data)


def test_binding_patch_validates_effective_source_and_allows_authorized_reparent(boundary):
    actor, canonical, canonical_other = (
        boundary['actor'], boundary['canonical'], boundary['canonical_other']
    )
    native_project, source, source_id = make_native_source(
        'api', boundary['other_owner'], project_id=canonical.pk + 1, member=actor,
        suffix='patch-owned',
    )
    created = post_binding(actor, canonical, 'api', source_id, native_project.pk)
    assert created.status_code == 201, created.data

    url = f"/api/executions/source-bindings/{created.data['id']}/"
    client = client_for(actor)
    reparented = client.patch(url, {'project': canonical_other.pk}, format='json')
    assert reparented.status_code == 200, reparented.data
    assert reparented.data['project'] == canonical_other.pk

    mismatched_project = client.patch(
        url, {'source_project_id': str(native_project.pk + 1000)}, format='json'
    )
    assert mismatched_project.status_code == 400, mismatched_project.data

    foreign_project, foreign_source, foreign_id = make_native_source(
        'api', boundary['other_owner'], project_id=canonical.pk + 2, suffix='patch-foreign'
    )
    foreign_update = client.patch(
        url,
        {'source_id': foreign_id, 'source_project_id': str(foreign_project.pk)},
        format='json',
    )
    assert foreign_update.status_code == 403, foreign_update.data


def test_binding_rejects_wrong_native_kind(boundary):
    native_project, source, _source_id = make_native_source(
        'api', boundary['actor'], member=boundary['actor'], suffix='wrong-kind'
    )
    response = post_binding(
        boundary['actor'], boundary['canonical'], 'api',
        f'api:case:{source.pk}', native_project.pk,
    )
    assert response.status_code == 400, response.data
    assert 'source_id' in response.data


def test_app_approval_capabilities_and_lease_require_privileged_changes(boundary):
    actor, canonical = boundary['actor'], boundary['canonical']
    native_project, source, source_id = make_native_source(
        'app', boundary['other_owner'], project_id=canonical.pk + 10, member=actor,
        suffix='approval',
    )
    approved_metadata = {
        'approved': True,
        'capabilities': {'platform': 'android'},
        'lease_seconds': 120,
    }
    denied_create = post_binding(
        actor, canonical, 'app', source_id, native_project.pk,
        metadata=approved_metadata,
    )
    assert denied_create.status_code == 403, denied_create.data

    created = post_binding(actor, canonical, 'app', source_id, native_project.pk)
    assert created.status_code == 201, created.data
    url = f"/api/executions/source-bindings/{created.data['id']}/"
    member_client = client_for(actor)
    for metadata in (
        {'approved': True},
        {'capabilities': {'platform': 'android'}},
        {'lease_seconds': 7200},
    ):
        denied = member_client.patch(url, {'metadata': metadata}, format='json')
        assert denied.status_code == 403, (metadata, denied.data)

    approved = client_for(boundary['admin']).patch(
        url, {'metadata': approved_metadata}, format='json'
    )
    assert approved.status_code == 200, approved.data

    app_binding = ExecutionSourceBinding.objects.get(pk=created.data['id'])
    job = ExecutionJob.objects.create(
        project=canonical, source_type='app', source_id=source_id, requested_by=actor
    )
    receipt = AppExecutorAdapter(
        dispatcher=lambda *_: 'approved-app-dispatch'
    ).submit(job)
    attempt = job.attempts.get(pk=receipt.attempt_id)
    worker = execute_app_source
    app_binding.refresh_from_db()
    assert app_binding.metadata['approved'] is True
    device = SimpleNamespace(pk=71, lease_generation=3)
    execution = SimpleNamespace(
        pk=72, result='passed', status='completed', refresh_from_db=lambda: None
    )
    with (
        patch('apps.app_automation.device_pool.acquire_device', return_value=(device, 'test-lease')) as acquire,
        patch('apps.app_automation.device_pool.release_device') as release,
        patch('apps.app_automation.models.AppTestExecution.objects.create', return_value=execution),
        patch('apps.app_automation.tasks.execute_app_test_task.run') as app_runner,
    ):
        result = worker.run(job.pk, attempt.pk)

    assert result['status'] == 'passed', result
    acquire.assert_called_once_with(
        actor, capabilities={'platform': 'android'}, lease_seconds=120
    )
    app_runner.assert_called_once()
    release.assert_called_once_with(device.pk, actor, 'test-lease')


def test_app_approval_cannot_be_smuggled_through_non_app_metadata(boundary):
    actor, canonical = boundary['actor'], boundary['canonical']
    api_project, _api_source, api_source_id = make_native_source(
        'api', boundary['other_owner'], project_id=canonical.pk + 11, member=actor,
        suffix='metadata-smuggle-api',
    )
    created = post_binding(
        actor, canonical, 'api', api_source_id, api_project.pk,
        metadata={'approved': True},
    )
    assert created.status_code == 201, created.data

    app_project, _app_source, app_source_id = make_native_source(
        'app', boundary['other_owner'], project_id=canonical.pk + 12, member=actor,
        suffix='metadata-smuggle-app',
    )
    changed = client_for(actor).patch(
        f"/api/executions/source-bindings/{created.data['id']}/",
        {
            'source_type': 'app',
            'source_id': app_source_id,
            'source_project_id': str(app_project.pk),
        },
        format='json',
    )
    assert changed.status_code == 403, changed.data


def test_worker_rechecks_revoked_native_access_for_every_source_type(boundary):
    for source_type in WORKERS:
        native_project, _source, source_id = make_native_source(
            source_type, boundary['other_owner'], project_id=boundary['canonical'].pk + 20,
            member=boundary['actor'], suffix=f'revoked-{source_type}',
        )
        binding_metadata = {'approved': True} if source_type in {'app', 'performance'} else {}
        _binding, job, attempt, worker = create_worker_job(
            boundary, source_type, native_project, source_id, metadata=binding_metadata
        )
        native_project.members.remove(boundary['actor'])
        action_path = {
            'api': 'apps.executions.source_workers._api_execute',
            'ui': 'apps.executions.source_workers._ui_execute',
            'app': 'apps.executions.source_workers._app_execute',
            'performance': 'apps.executions.source_workers._performance_execute',
        }[source_type]
        with patch(action_path) as source_action:
            result = worker.run(job.pk, attempt.pk)
        assert result['status'] == 'failed', (source_type, result)
        source_action.assert_not_called()


def test_api_environment_none_does_not_bypass_revoked_native_project_access(boundary):
    native_project, _source, source_id = make_native_source(
        'api', boundary['other_owner'], project_id=boundary['canonical'].pk + 30,
        member=boundary['actor'], suffix='api-env-none',
    )
    _binding, job, attempt, worker = create_worker_job(
        boundary, 'api', native_project, source_id
    )
    native_project.members.remove(boundary['actor'])
    with patch(
        'apps.api_testing.utils.execute_api_request', return_value={'success': True}
    ) as execute_request:
        result = worker.run(job.pk, attempt.pk)
    assert result['status'] == 'failed', result
    execute_request.assert_not_called()


def test_worker_rejects_direct_orm_binding_with_forged_source_project_id(boundary):
    native_project, _source, source_id = make_native_source(
        'api', boundary['actor'], project_id=boundary['canonical'].pk + 40,
        suffix='forged-binding',
    )
    _binding, job, attempt, worker = create_worker_job(
        boundary, 'api', native_project, source_id, source_project_id='999999'
    )
    with patch('apps.executions.source_workers._api_execute') as execute:
        result = worker.run(job.pk, attempt.pk)
    assert result['status'] == 'failed', result
    execute.assert_not_called()


def test_worker_refreshes_actor_active_flag_before_source_execution(boundary):
    from apps.executions.source_contract import resolve_source_binding

    native_project, _source, source_id = make_native_source(
        'api', boundary['actor'], project_id=boundary['canonical'].pk + 45,
        suffix='actor-deactivated-mid-delivery',
    )
    _binding, job, attempt, worker = create_worker_job(
        boundary, 'api', native_project, source_id
    )

    def deactivate_after_job_load(current_job):
        User.objects.filter(pk=boundary['actor'].pk).update(is_active=False)
        return resolve_source_binding(current_job)

    with (
        patch(
            'apps.executions.source_workers.resolve_source_binding',
            side_effect=deactivate_after_job_load,
        ),
        patch('apps.executions.source_workers._api_execute') as execute,
    ):
        result = worker.run(job.pk, attempt.pk)

    assert result['status'] == 'failed', result
    execute.assert_not_called()


@pytest.mark.parametrize('actor_state', ['missing', 'inactive'])
def test_worker_requires_present_active_execution_actor(boundary, actor_state):
    native_project, _source, source_id = make_native_source(
        'api', boundary['actor'], project_id=boundary['canonical'].pk + 50,
        suffix=f'actor-{actor_state}',
    )
    if actor_state == 'inactive':
        boundary['actor'].is_active = False
        boundary['actor'].save(update_fields=['is_active'])
        requested_by, created_by = boundary['actor'], boundary['actor']
    else:
        requested_by, created_by = None, None
    _binding, job, attempt, worker = create_worker_job(
        boundary, 'api', native_project, source_id,
        requested_by=requested_by, created_by=created_by,
    )
    with patch('apps.executions.source_workers._api_execute') as execute:
        result = worker.run(job.pk, attempt.pk)
    assert result['status'] == 'failed', result
    execute.assert_not_called()


@pytest.mark.parametrize('source_type', ['api', 'ui', 'performance'])
def test_authorized_native_members_keep_mocked_source_routes(boundary, source_type):
    native_project, source, source_id = make_native_source(
        source_type, boundary['other_owner'], project_id=boundary['canonical'].pk + 60,
        member=boundary['actor'], suffix=f'authorized-{source_type}',
    )
    metadata = {'approved': True} if source_type == 'performance' else {}
    _binding, job, attempt, worker = create_worker_job(
        boundary, source_type, native_project, source_id, metadata=metadata
    )

    if source_type == 'api':
        with patch(
            'apps.api_testing.utils.execute_api_request',
            return_value={'success': True, 'status_code': 200},
        ) as execute_request:
            result = worker.run(job.pk, attempt.pk)
        assert result['status'] == 'passed', result
        assert execute_request.call_args.args[0].pk == source.pk
        assert execute_request.call_args.args[1] is None
        assert execute_request.call_args.args[2] == boundary['actor']
    elif source_type == 'ui':
        with patch('apps.ui_automation.test_executor.TestExecutor') as runner_class:
            runner_class.return_value.results = []

            def pass_suite():
                suite = runner_class.call_args.kwargs['test_suite']
                suite.execution_status = 'passed'
                suite.save(update_fields=['execution_status', 'updated_at'])

            runner_class.return_value.run.side_effect = pass_suite
            result = worker.run(job.pk, attempt.pk)
        assert result['status'] == 'passed', result
        runner_class.return_value.run.assert_called_once()
    else:
        execution = SimpleNamespace(pk=73)
        with patch(
            'apps.perf_testing.services.executor.start_execution',
            return_value=(execution, {}),
        ) as start_execution:
            result = worker.run(job.pk, attempt.pk)
        assert result['status'] == 'started', result
        start_execution.assert_called_once()
        assert start_execution.call_args.args[0].pk == source.pk
        assert start_execution.call_args.kwargs['user'] == boundary['actor']


def test_canonical_reparent_patch_rechecks_native_access_after_revocation(boundary):
    native_project, _source, source_id = make_native_source(
        'ui', boundary['other_owner'], project_id=boundary['canonical'].pk + 70,
        member=boundary['actor'], suffix='reparent-access',
    )
    created = post_binding(
        boundary['actor'], boundary['canonical'], 'ui', source_id, native_project.pk
    )
    assert created.status_code == 201, created.data
    native_project.members.remove(boundary['actor'])
    url = f"/api/executions/source-bindings/{created.data['id']}/"
    response = client_for(boundary['actor']).patch(
        url, {'project': boundary['canonical_other'].pk}, format='json'
    )
    assert response.status_code == 403, response.data
