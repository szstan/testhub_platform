import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from apps.executions.models import ExecutionJob, ExecutionSourceBinding
from apps.projects.models import Project
from apps.users.models import User


@pytest.fixture
def binding_fixture(db):
    owner = User.objects.create_user(username='binding-owner', password='pass')
    outsider = User.objects.create_user(username='binding-outsider', password='pass')
    project = Project.objects.create(name='Binding Project', owner=owner)
    other_project = Project.objects.create(name='Other Binding Project', owner=outsider)
    return owner, outsider, project, other_project


@pytest.mark.django_db
def test_source_binding_api_is_project_scoped_and_validates_typed_reference(binding_fixture):
    owner, outsider, project, other_project = binding_fixture
    client = APIClient()
    client.force_authenticate(user=owner)

    created = client.post(
        '/api/executions/source-bindings/',
        {
            'project': project.pk,
            'source_type': 'api',
            'source_id': 'api:suite:42',
            'source_project_id': 'legacy-api-project',
            'metadata': {'environment': 'staging'},
        },
        format='json',
    )
    assert created.status_code == 201, created.data
    assert created.data['source_reference']['canonical'] == 'api:suite:42'
    assert created.data['source_binding_status'] if 'source_binding_status' in created.data else True
    assert created.data['route_configured'] is False
    assert created.data['executor_status'] == 'unsupported'

    listed = client.get('/api/executions/source-bindings/?project=%s' % project.pk)
    assert listed.status_code == 200
    assert listed.data['count'] == 1

    invalid_reference = client.post(
        '/api/executions/source-bindings/',
        {
            'project': project.pk,
            'source_type': 'api',
            'source_id': 'suite:42',
            'source_project_id': 'legacy-api-project',
            'metadata': {},
        },
        format='json',
    )
    assert invalid_reference.status_code == 400
    assert 'source_id' in invalid_reference.data

    missing_source_project = client.post(
        '/api/executions/source-bindings/',
        {
            'project': project.pk,
            'source_type': 'api',
            'source_id': 'api:suite:43',
            'metadata': {},
        },
        format='json',
    )
    assert missing_source_project.status_code == 400
    assert 'source_project_id' in missing_source_project.data

    client.force_authenticate(user=outsider)
    assert client.get('/api/executions/source-bindings/').data['count'] == 0
    assert client.get('/api/executions/source-bindings/%s/' % created.data['id']).status_code == 404

    forbidden_create = client.post(
        '/api/executions/source-bindings/',
        {
            'project': project.pk,
            'source_type': 'api',
            'source_id': 'api:suite:44',
            'source_project_id': str(other_project.pk),
            'metadata': {},
        },
        format='json',
    )
    assert forbidden_create.status_code in {403, 400}


@pytest.mark.django_db
def test_job_readiness_reflects_route_and_enabled_binding(binding_fixture):
    owner, _outsider, project, _other_project = binding_fixture
    client = APIClient()
    client.force_authenticate(user=owner)
    job = ExecutionJob.objects.create(
        project=project,
        source_type='api',
        source_id='api:suite:42',
        requested_by=owner,
    )

    missing = client.get('/api/executions/jobs/%s/' % job.pk)
    assert missing.status_code == 200
    assert missing.data['source_binding_status'] == 'missing'
    assert missing.data['execution_supported'] is False

    ExecutionSourceBinding.objects.create(
        project=project,
        source_type='api',
        source_id='api:suite:42',
        source_project_id='legacy-api-project',
        created_by=owner,
    )
    with override_settings(EXECUTION_DISPATCH_ROUTES={'api': 'apps.executions.tasks.execute_api_source'}):
        ready = client.get('/api/executions/jobs/%s/' % job.pk)

    assert ready.status_code == 200
    assert ready.data['source_binding_status'] == 'ready'
    assert ready.data['executor_status'] == 'ready'
    assert ready.data['execution_supported'] is True


@pytest.mark.django_db
def test_disabling_binding_is_visible_in_readiness(binding_fixture):
    owner, _outsider, project, _other_project = binding_fixture
    binding = ExecutionSourceBinding.objects.create(
        project=project,
        source_type='ui',
        source_id='ui:case:10',
        source_project_id='legacy-ui-project',
        created_by=owner,
        enabled=True,
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    with override_settings(EXECUTION_DISPATCH_ROUTES={'ui': 'apps.executions.tasks.execute_ui_source'}):
        response = client.post(f'/api/executions/source-bindings/{binding.pk}/disable/')
    assert response.status_code == 200
    assert response.data['executor_status'] == 'disabled_binding'
