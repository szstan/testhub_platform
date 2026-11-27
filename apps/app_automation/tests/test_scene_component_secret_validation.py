import copy

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.models import AppProject, AppProjectComponent
from apps.app_automation.views.scene_data_views import ProjectComponentViewSet
from apps.users.models import User


@pytest.fixture
def projects(db):
    owner = User.objects.create_user(username='component-secret-owner')
    member = User.objects.create_user(username='component-secret-member')
    stranger = User.objects.create_user(username='component-secret-stranger')
    project = AppProject.objects.create(name='component-secret-project', owner=owner)
    project.members.add(member)
    other_project = AppProject.objects.create(name='component-secret-other', owner=stranger)
    return owner, member, stranger, project, other_project


def create_component(user, payload):
    request = APIRequestFactory().post('/scene-components/', payload, format='json')
    force_authenticate(request, user=user)
    return ProjectComponentViewSet.as_view({'post': 'create'})(request)


def nested_step(step):
    return [{
        'type': 'if',
        'config': {'then_steps': [{
            'type': 'try',
            'config': {'finally_steps': [{
                'type': 'inline_fixture',
                'kind': 'custom',
                'version': 'inline',
                'steps': [step],
            }]},
        }]},
    }]


def component_payload(project):
    return {
        'project': project.pk,
        'name': 'Sensitive component fixture',
        'type': 'sensitive_component_fixture',
        'version': '1',
        'schema': {
            'properties': {
                'password': {
                    'type': 'string',
                    'sensitive': True,
                    'default': '{{local.password}}',
                },
                'label': {'type': 'string'},
            },
            'required': [],
        },
        'default_config': {
            'password': '${global.password}',
            'label': 'ordinary component default',
        },
        'steps': nested_step({
            'type': 'text',
            'config': {'sensitive': True, 'text': '{{local.password}}'},
        }) + [{
            'type': 'input',
            'sensitive': True,
            'value': '${global.password}',
        }],
    }


@pytest.mark.django_db
def test_component_accepts_sensitive_variable_references_without_mutating_source(projects):
    _, member, _, project, _ = projects
    payload = component_payload(project)
    original = copy.deepcopy(payload)

    response = create_component(member, payload)

    assert response.status_code == 201
    assert payload == original
    component = AppProjectComponent.objects.get(pk=response.data['id'])
    assert component.schema['properties']['password']['default'] == '{{local.password}}'
    assert component.default_config['password'] == '${global.password}'
    assert component.steps == original['steps']


@pytest.mark.django_db
@pytest.mark.parametrize('location', [
    'schema_property_default',
    'default_config',
    'nested_input',
    'nested_text',
    'secret_alias',
])
def test_component_rejects_sensitive_literals_without_echoing_values(projects, location):
    owner, _, _, project, _ = projects
    payload = component_payload(project)
    private_value = f'synthetic-private-{location}'
    if location == 'schema_property_default':
        payload['schema']['properties']['password']['default'] = private_value
    elif location == 'default_config':
        payload['default_config']['password'] = private_value
    elif location == 'nested_input':
        payload['steps'] = nested_step({
            'type': 'input', 'sensitive': True, 'value': private_value,
        })
    elif location == 'nested_text':
        payload['steps'] = nested_step({
            'type': 'text', 'config': {'sensitive': True, 'text': private_value},
        })
    else:
        private_value = 'secret://synthetic-private-alias'
        payload['default_config']['password'] = private_value

    response = create_component(owner, payload)

    assert response.status_code == 400
    assert private_value not in str(response.data)
    assert not AppProjectComponent.objects.filter(project=project).exists()


@pytest.mark.django_db
def test_component_preserves_non_sensitive_literals(projects):
    owner, _, _, project, _ = projects
    payload = component_payload(project)
    ordinary_value = 'synthetic-ordinary-literal'
    payload['default_config']['label'] = ordinary_value
    payload['steps'] = [{'type': 'input', 'value': ordinary_value}]

    response = create_component(owner, payload)

    assert response.status_code == 201
    component = AppProjectComponent.objects.get(pk=response.data['id'])
    assert component.default_config['label'] == ordinary_value
    assert component.steps[0]['value'] == ordinary_value


@pytest.mark.django_db
def test_component_creation_keeps_project_permissions_scoped(projects):
    _, member, stranger, project, other_project = projects

    allowed = create_component(member, component_payload(project))
    denied = create_component(stranger, {**component_payload(project), 'version': '2'})
    separate_project = create_component(stranger, component_payload(other_project))

    assert allowed.status_code == 201
    assert denied.status_code == 400
    assert separate_project.status_code == 201
    assert AppProjectComponent.objects.filter(project=project).count() == 1
    assert AppProjectComponent.objects.filter(project=other_project).count() == 1
