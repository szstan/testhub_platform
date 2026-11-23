import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.models import (
    AppProject,
    AppProjectComponent,
    AppTestCase,
    AppTestExecution,
)
from apps.app_automation.scene_component_runtime import SceneComponentError, expand_project_flow
from apps.app_automation.views.scene_data_views import ProjectComponentViewSet
from apps.app_automation.views.test_case_views import AppTestCaseViewSet
from apps.users.models import User


@pytest.fixture
def schema_flag_context(db):
    owner = User.objects.create_user(username='schema-flag-owner')
    member = User.objects.create_user(username='schema-flag-member')
    project = AppProject.objects.create(name='schema flag project', owner=owner)
    project.members.add(member)
    return owner, member, project


def _post_component(user, payload):
    request = APIRequestFactory().post('/scene-components/', payload, format='json')
    force_authenticate(request, user=user)
    return ProjectComponentViewSet.as_view({'post': 'create'})(request)


def _post_case(user, payload):
    request = APIRequestFactory().post('/app-test-cases/', payload, format='json')
    force_authenticate(request, user=user)
    return AppTestCaseViewSet.as_view({'post': 'create'})(request)


@pytest.mark.django_db
@pytest.mark.parametrize('schema_flag', ['sensitive', 'secret', 'writeOnly'])
@pytest.mark.parametrize('default_location', ['schema_default', 'default_config'])
def test_component_defaults_reject_literals_and_accept_variable_refs(
    schema_flag_context, schema_flag, default_location,
):
    owner, _, project = schema_flag_context
    spec = {'type': 'string', schema_flag: True}
    if default_location == 'schema_default':
        spec['default'] = 'synthetic-private-default'
        defaults = {}
    else:
        defaults = {'opaque_field': 'synthetic-private-default'}
    payload = {
        'project': project.pk,
        'name': 'schema flag defaults',
        'type': 'schema_flag_defaults',
        'version': '1',
        'schema': {'properties': {'opaque_field': spec}},
        'default_config': defaults,
        'steps': [{'type': 'wait'}],
    }

    rejected = _post_component(owner, payload)

    assert rejected.status_code == 400
    assert 'synthetic-private-default' not in str(rejected.data)
    assert not AppProjectComponent.objects.filter(project=project).exists()

    if default_location == 'schema_default':
        spec['default'] = '{{synthetic_runtime_value}}'
    else:
        defaults['opaque_field'] = '${synthetic_runtime_value}'
    accepted = _post_component(owner, payload)

    assert accepted.status_code == 201, accepted.data


@pytest.mark.django_db
@pytest.mark.parametrize('schema_flag', ['sensitive', 'secret', 'writeOnly'])
def test_case_and_runtime_reject_flagged_literals_and_accept_variable_refs(
    schema_flag_context, schema_flag,
):
    owner, member, project = schema_flag_context
    component = AppProjectComponent.objects.create(
        project=project,
        name='schema flag reference',
        type='schema_flag_reference',
        version='1',
        schema={'properties': {'opaque_field': {'type': 'string', schema_flag: True}}},
        steps=[{'type': 'wait'}],
        created_by=owner,
    )
    private_value = 'synthetic-private-reference'
    literal_flow = [{
        'kind': 'custom', 'type': component.type, 'version': component.version,
        'params': {'opaque_field': private_value},
    }]

    rejected = _post_case(member, {
        'name': 'literal schema flag reference', 'project': project.pk, 'ui_flow': literal_flow,
    })

    assert rejected.status_code == 400
    assert private_value not in str(rejected.data)
    assert not AppTestCase.objects.filter(project=project).exists()

    legacy_case = AppTestCase.objects.create(
        name='legacy literal schema flag reference', project=project, created_by=member,
        ui_flow=literal_flow,
    )
    legacy_execution = AppTestExecution.objects.create(test_case=legacy_case, user=member)
    with pytest.raises(SceneComponentError) as exc_info:
        expand_project_flow(legacy_execution, legacy_case, literal_flow)
    assert private_value not in str(exc_info.value)

    variable_flow = [{
        'kind': 'custom', 'type': component.type, 'version': component.version,
        'params': {'opaque_field': '{{synthetic_runtime_value}}'},
    }]
    accepted = _post_case(member, {
        'name': 'variable schema flag reference', 'project': project.pk, 'ui_flow': variable_flow,
    })

    assert accepted.status_code == 201, accepted.data
    saved_case = AppTestCase.objects.get(pk=accepted.data['id'])
    execution = AppTestExecution.objects.create(test_case=saved_case, user=member)
    assert expand_project_flow(execution, saved_case, variable_flow) == [{'type': 'wait'}]
