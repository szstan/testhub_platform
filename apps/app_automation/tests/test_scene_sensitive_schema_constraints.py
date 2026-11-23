import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.models import AppProject, AppProjectComponent, AppTestCase, AppTestExecution
from apps.app_automation.scene_component_runtime import (
    SceneComponentError,
    expand_project_flow,
    validate_sensitive_component_data,
)
from apps.app_automation.views.scene_data_views import ProjectComponentViewSet
from apps.app_automation.views.test_case_views import AppTestCaseViewSet
from apps.users.models import User


_CONSTRAINTS = ('enum', 'const', 'examples')
_SENSITIVE_LOCATIONS = (
    'sensitive_flag',
    'secret_flag',
    'write_only_flag',
    'root_sensitive_flag',
    'root_sensitive_additional_properties',
    'sensitive_ancestor',
    'sensitive_name',
    'sensitive_name_items',
    'sensitive_name_additional_properties',
)


def _schema_with_constraint(location, keyword, value):
    if location == 'root_sensitive_flag':
        schema = {'type': 'object', 'sensitive': True}
        target = schema
    elif location == 'root_sensitive_additional_properties':
        target = {'type': 'string'}
        schema = {'type': 'object', 'secret': True, 'additionalProperties': target}
    elif location.endswith('_flag'):
        flag = {
            'sensitive_flag': 'sensitive',
            'secret_flag': 'secret',
            'write_only_flag': 'writeOnly',
        }[location]
        target = {'type': 'string', flag: True}
        schema = {'properties': {'credential': target}}
    elif location == 'sensitive_ancestor':
        target = {'type': 'string'}
        schema = {'properties': {'account': {
            'type': 'object', 'sensitive': True, 'properties': {'choice': target},
        }}}
    elif location == 'sensitive_name':
        target = {'type': 'string'}
        schema = {'properties': {'account': {
            'type': 'object', 'properties': {'api_token': target},
        }}}
    elif location == 'sensitive_name_items':
        target = {'type': 'string'}
        schema = {'properties': {'password': {'type': 'array', 'items': target}}}
    else:
        target = {'type': 'string'}
        schema = {'properties': {'api_token': {
            'type': 'object', 'additionalProperties': target,
        }}}
    target[keyword] = value
    return schema


def _constraint_value(keyword, value):
    return [value] if keyword in ('enum', 'examples') else value


@pytest.mark.parametrize('location', _SENSITIVE_LOCATIONS)
@pytest.mark.parametrize('keyword', _CONSTRAINTS)
def test_sensitive_schema_constraints_reject_literals_without_echoing(location, keyword):
    private_value = f'synthetic-private-{location}-{keyword}'
    schema = _schema_with_constraint(
        location, keyword, _constraint_value(keyword, private_value),
    )

    with pytest.raises(SceneComponentError) as exc_info:
        validate_sensitive_component_data(schema, {})

    assert private_value not in str(exc_info.value)


@pytest.mark.parametrize(('keyword', 'value'), [
    ('enum', ['', None, '{{local.password}}', '${global.password}']),
    ('const', '{{local.password}}'),
    ('examples', [None, '', '{{local.password}}', '${global.password}']),
])
def test_sensitive_schema_constraints_accept_empty_values_and_exact_scene_references(keyword, value):
    schema = {'properties': {'password': {
        'type': 'string', 'writeOnly': True, keyword: value,
    }}}

    validate_sensitive_component_data(schema, {})


@pytest.mark.parametrize('keyword', _CONSTRAINTS)
@pytest.mark.parametrize('unsafe_reference', [
    '{{params.password}}',
    'secret://synthetic-private-alias',
])
def test_sensitive_schema_constraints_reject_parameter_and_secret_alias_references(
    keyword, unsafe_reference,
):
    schema = {'properties': {'password': {
        'type': 'string', 'sensitive': True,
        keyword: _constraint_value(keyword, unsafe_reference),
    }}}

    with pytest.raises(SceneComponentError) as exc_info:
        validate_sensitive_component_data(schema, {})
    assert unsafe_reference not in str(exc_info.value)


def test_non_sensitive_schema_constraints_keep_literal_choices():
    choices = ['synthetic-ready', 'synthetic-paused']
    schema = {'properties': {'status': {
        'type': 'string', 'enum': choices, 'const': choices[0], 'examples': choices,
    }}}

    validate_sensitive_component_data(schema, {})


def test_sensitive_schema_constraint_lists_obey_the_validation_node_budget(monkeypatch):
    from apps.app_automation import scene_component_runtime

    monkeypatch.setattr(scene_component_runtime, '_MAX_VALIDATION_NODES', 5)
    schema = {'properties': {'password': {
        'type': 'string', 'sensitive': True, 'examples': ['', '', '', ''],
    }}}

    with pytest.raises(SceneComponentError):
        validate_sensitive_component_data(schema, {})


@pytest.fixture
def constraint_api_context(db):
    owner = User.objects.create_user(username='schema-constraint-owner')
    member = User.objects.create_user(username='schema-constraint-member')
    project = AppProject.objects.create(name='schema constraint project', owner=owner)
    project.members.add(member)
    return owner, member, project


def _post_component(user, payload):
    request = APIRequestFactory().post('/scene-components/', payload, format='json')
    force_authenticate(request, user=user)
    return ProjectComponentViewSet.as_view({'post': 'create'})(request)


def _post_case(user, project, flow):
    request = APIRequestFactory().post('/app-test-cases/', {
        'name': 'synthetic schema constraint case', 'project': project.pk, 'ui_flow': flow,
    }, format='json')
    force_authenticate(request, user=user)
    return AppTestCaseViewSet.as_view({'post': 'create'})(request)


@pytest.mark.django_db
@pytest.mark.parametrize('keyword', _CONSTRAINTS)
def test_component_case_and_runtime_reject_sensitive_schema_literals(
    constraint_api_context, keyword,
):
    owner, member, project = constraint_api_context
    private_value = f'synthetic-private-create-{keyword}'
    schema = {'properties': {'password': {
        'type': 'string', 'writeOnly': True,
        keyword: _constraint_value(keyword, private_value),
    }}}
    component_type = f'sensitive_constraint_{keyword}'
    component_payload = {
        'project': project.pk, 'name': 'synthetic sensitive schema',
        'type': component_type, 'version': '1', 'schema': schema,
        'default_config': {}, 'steps': [{'type': 'wait'}],
    }

    component_response = _post_component(owner, component_payload)

    assert component_response.status_code == 400
    assert private_value not in str(component_response.data)
    assert not AppProjectComponent.objects.filter(project=project, type=component_type).exists()

    component = AppProjectComponent.objects.create(
        project=project, name='legacy sensitive schema', type=component_type, version='1',
        schema=schema, steps=[{'type': 'wait'}], created_by=owner,
    )
    flow = [{'kind': 'custom', 'type': component.type, 'version': component.version}]

    case_response = _post_case(member, project, flow)

    assert case_response.status_code == 400
    assert private_value not in str(case_response.data)
    assert not AppTestCase.objects.filter(project=project).exists()

    case = AppTestCase.objects.create(
        name='legacy sensitive schema case', project=project, created_by=member, ui_flow=flow,
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)
    with pytest.raises(SceneComponentError) as exc_info:
        expand_project_flow(execution, case, flow)
    assert private_value not in str(exc_info.value)
