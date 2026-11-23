import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.models import (
    AppProject,
    AppProjectComponent,
    AppTestCase,
    AppTestExecution,
)
from apps.app_automation.scene_component_runtime import (
    SceneComponentError,
    expand_project_flow,
)
from apps.app_automation.views.scene_data_views import ProjectComponentViewSet
from apps.app_automation.views.test_case_views import AppTestCaseViewSet
from apps.users.models import User


@pytest.fixture
def nested_secret_context(db):
    owner = User.objects.create_user(username='nested-secret-owner')
    member = User.objects.create_user(username='nested-secret-member')
    project = AppProject.objects.create(name='nested-secret-project', owner=owner)
    project.members.add(member)
    component = AppProjectComponent.objects.create(
        project=project,
        name='nested secret fixture',
        type='nested_secret_fixture',
        version='1',
        schema={
            'properties': {
                'account_data': {
                    'type': 'object',
                    'properties': {
                        'username': {'type': 'string'},
                        'password': {'type': 'string', 'writeOnly': True},
                    },
                },
            },
        },
        steps=[{'type': 'set_variable', 'config': {
            'name': 'captured_account', 'value': '{{params.account_data}}',
        }}],
        created_by=owner,
    )
    return owner, member, project, component


def _post_component(user, payload):
    request = APIRequestFactory().post('/scene-components/', payload, format='json')
    force_authenticate(request, user=user)
    return ProjectComponentViewSet.as_view({'post': 'create'})(request)


def _post_case(user, project, flow):
    request = APIRequestFactory().post('/app-test-cases/', {
        'name': 'synthetic nested secret case', 'project': project.pk, 'ui_flow': flow,
    }, format='json')
    force_authenticate(request, user=user)
    return AppTestCaseViewSet.as_view({'post': 'create'})(request)


def _reference(component, *, params=None, config=None):
    reference = {'kind': 'custom', 'type': component.type, 'version': component.version}
    if params is not None:
        reference['params'] = params
    if config is not None:
        reference['config'] = config
    return reference


def _runtime_rejects(project, member, flow):
    case = AppTestCase.objects.create(
        name='synthetic legacy nested secret case', project=project, created_by=member, ui_flow=flow,
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)
    with pytest.raises(SceneComponentError) as exc_info:
        expand_project_flow(execution, case, flow)
    assert 'synthetic-private' not in str(exc_info.value)


@pytest.mark.django_db
@pytest.mark.parametrize('source_field', ['params', 'config'])
@pytest.mark.parametrize('private_value', [
    'synthetic-private-nested-value',
    'secret://synthetic.password-alias',
])
def test_case_save_and_runtime_reject_nested_write_only_literals(
    nested_secret_context, source_field, private_value,
):
    _, member, project, component = nested_secret_context
    values = {'account_data': {'username': 'synthetic-user', 'password': private_value}}
    flow = [_reference(component, **{source_field: values})]

    response = _post_case(member, project, flow)

    assert response.status_code == 400
    assert private_value not in str(response.data)
    assert not AppTestCase.objects.filter(project=project).exists()
    _runtime_rejects(project, member, flow)


@pytest.mark.django_db
def test_case_save_checks_nested_config_even_when_params_are_safe(nested_secret_context):
    _, member, project, component = nested_secret_context
    private_value = 'synthetic-private-nested-unused-config'
    flow = [_reference(
        component,
        params={'account_data': {'username': 'synthetic-user', 'password': '{{local.password}}'}},
        config={'account_data': {'username': 'synthetic-user', 'password': private_value}},
    )]

    response = _post_case(member, project, flow)

    assert response.status_code == 400
    assert private_value not in str(response.data)
    _runtime_rejects(project, member, flow)


@pytest.mark.django_db
@pytest.mark.parametrize('location', ['schema_default', 'default_config'])
def test_component_save_rejects_nested_sensitive_defaults(nested_secret_context, location):
    owner, _, project, _ = nested_secret_context
    password = {'type': 'string', 'writeOnly': True}
    schema = {'properties': {'account_data': {
        'type': 'object', 'properties': {'username': {'type': 'string'}, 'password': password},
    }}}
    defaults = {}
    private_value = f'synthetic-private-default-{location}'
    if location == 'schema_default':
        password['default'] = private_value
    else:
        defaults = {'account_data': {'username': 'synthetic-user', 'password': private_value}}
    payload = {
        'project': project.pk, 'name': 'nested default fixture',
        'type': f'nested_default_{location}', 'version': '1', 'schema': schema,
        'default_config': defaults, 'steps': [{'type': 'wait'}],
    }

    response = _post_component(owner, payload)

    assert response.status_code == 400
    assert private_value not in str(response.data)
    assert not AppProjectComponent.objects.filter(project=project, type=payload['type']).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('parameter_source', ['params', 'config'])
def test_nested_array_schema_rejects_secret_alias_literals(nested_secret_context, parameter_source):
    owner, member, project, _ = nested_secret_context
    component = AppProjectComponent.objects.create(
        project=project, name='array nested secret fixture', type='nested_array_secret_fixture',
        version='1', schema={'properties': {
            'records': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'api_token': {'type': 'string'}, 'label': {'type': 'string'},
            }}},
        }}, steps=[{'type': 'wait'}], created_by=owner,
    )
    private_value = 'synthetic-private-array-token'
    values = {'records': [{'label': 'synthetic-label', 'api_token': private_value}]}
    flow = [_reference(component, **{parameter_source: values})]

    response = _post_case(member, project, flow)

    assert response.status_code == 400
    assert private_value not in str(response.data)
    _runtime_rejects(project, member, flow)


@pytest.mark.django_db
@pytest.mark.parametrize('sensitive_key', ['password', 'service_token'])
def test_undeclared_sensitive_nested_keys_are_rejected(nested_secret_context, sensitive_key):
    _, member, project, component = nested_secret_context
    private_value = f'synthetic-private-undeclared-{sensitive_key}'
    flow = [_reference(component, params={
        'metadata': {'ordinary': 'synthetic-value', sensitive_key: private_value},
    })]

    response = _post_case(member, project, flow)

    assert response.status_code == 400
    assert private_value not in str(response.data)
    _runtime_rejects(project, member, flow)


@pytest.mark.django_db
@pytest.mark.parametrize('malformed_schema', [
    {'properties': {'account_data': {'type': 'object', 'patternProperties': {
        'password': {'type': 'string', 'writeOnly': True},
    }}}},
    {'properties': {'account_data': {'type': 'array', 'items': [
        {'type': 'object', 'properties': {'password': {'writeOnly': True}}},
    ]}}},
])
def test_malformed_or_ambiguous_sensitive_schemas_fail_closed(
    nested_secret_context, malformed_schema,
):
    owner, member, project, _ = nested_secret_context
    component = AppProjectComponent.objects.create(
        project=project, name='malformed nested schema', type='malformed_nested_schema_fixture',
        version='1', schema=malformed_schema, steps=[{'type': 'wait'}], created_by=owner,
    )
    flow = [_reference(component)]

    _runtime_rejects(project, member, flow)

    response = _post_component(owner, {
        'project': project.pk, 'name': 'malformed schema create',
        'type': f'malformed_create_{len(malformed_schema["properties"])}', 'version': '1',
        'schema': malformed_schema, 'default_config': {}, 'steps': [{'type': 'wait'}],
    })
    assert response.status_code == 400


@pytest.mark.django_db
def test_top_level_additional_parameter_schema_is_checked(nested_secret_context):
    owner, member, project, _ = nested_secret_context
    component = AppProjectComponent.objects.create(
        project=project, name='additional parameter secret fixture',
        type='additional_parameter_secret_fixture', version='1',
        schema={'additionalProperties': {'type': 'string', 'writeOnly': True}},
        steps=[{'type': 'wait'}], created_by=owner,
    )
    private_value = 'synthetic-private-additional-parameter'
    flow = [_reference(component, params={'opaque': private_value})]

    response = _post_case(member, project, flow)

    assert response.status_code == 400
    assert private_value not in str(response.data)
    _runtime_rejects(project, member, flow)


@pytest.mark.django_db
@pytest.mark.parametrize('sensitive_flag', ['sensitive', 'secret', 'writeOnly'])
def test_nested_schema_sensitivity_flags_reject_plaintext(nested_secret_context, sensitive_flag):
    owner, member, project, _ = nested_secret_context
    component = AppProjectComponent.objects.create(
        project=project, name=f'nested {sensitive_flag} fixture',
        type=f'nested_flag_{sensitive_flag.lower()}', version='1',
        schema={'properties': {'account_data': {'type': 'object', 'properties': {
            'opaque': {'type': 'string', sensitive_flag: True},
            'label': {'type': 'string'},
        }}}},
        steps=[{'type': 'wait'}], created_by=owner,
    )
    private_value = f'synthetic-private-flag-{sensitive_flag}'
    flow = [_reference(component, params={'account_data': {
        'label': 'synthetic-label', 'opaque': private_value,
    }})]

    response = _post_case(member, project, flow)

    assert response.status_code == 400
    assert private_value not in str(response.data)
    _runtime_rejects(project, member, flow)


@pytest.mark.django_db
@pytest.mark.parametrize('location', ['schema_default', 'default_config'])
def test_runtime_rejects_nested_sensitive_defaults(nested_secret_context, location):
    owner, member, project, _ = nested_secret_context
    password = {'type': 'string', 'writeOnly': True}
    schema = {'properties': {'account_data': {
        'type': 'object', 'properties': {'password': password},
    }}}
    private_value = f'synthetic-private-runtime-default-{location}'
    defaults = {}
    if location == 'schema_default':
        password['default'] = private_value
    else:
        defaults = {'account_data': {'password': private_value}}
    component = AppProjectComponent.objects.create(
        project=project, name=f'legacy nested default {location}',
        type=f'legacy_nested_default_{location}', version='1', schema=schema,
        default_config=defaults, steps=[{'type': 'wait'}], created_by=owner,
    )

    _runtime_rejects(project, member, [_reference(component)])


@pytest.mark.django_db
def test_schema_depth_is_bounded_at_component_save_and_runtime(nested_secret_context):
    owner, member, project, _ = nested_secret_context
    nested_spec = {'type': 'string', 'writeOnly': True}
    for index in range(18):
        nested_spec = {'type': 'object', 'properties': {f'level_{index}': nested_spec}}
    schema = {'properties': {'account_data': nested_spec}}
    component = AppProjectComponent.objects.create(
        project=project, name='over-depth nested schema', type='over_depth_nested_schema',
        version='1', schema=schema, steps=[{'type': 'wait'}], created_by=owner,
    )

    response = _post_component(owner, {
        'project': project.pk, 'name': 'over-depth schema create',
        'type': 'over_depth_nested_schema_create', 'version': '1', 'schema': schema,
        'default_config': {}, 'steps': [{'type': 'wait'}],
    })

    assert response.status_code == 400
    _runtime_rejects(project, member, [_reference(component)])


@pytest.mark.django_db
def test_nested_non_sensitive_values_and_typed_references_are_preserved(nested_secret_context):
    _, member, project, component = nested_secret_context
    flow = [_reference(component, params={'account_data': {
        'username': 'synthetic-user', 'password': '{{local.account_password}}',
    }})]

    response = _post_case(member, project, flow)

    assert response.status_code == 201, response.data
    case = AppTestCase.objects.get(pk=response.data['id'])
    execution = AppTestExecution.objects.create(test_case=case, user=member)
    expanded = expand_project_flow(execution, case, flow)
    assert expanded == [{'type': 'set_variable', 'config': {
        'name': 'captured_account', 'value': {
            'username': 'synthetic-user', 'password': '{{local.account_password}}',
        },
    }}]


@pytest.mark.django_db
@pytest.mark.parametrize('source_field', ['params', 'config'])
def test_nested_secret_schema_is_transitively_enforced(nested_secret_context, source_field):
    owner, member, project, child = nested_secret_context
    parent = AppProjectComponent.objects.create(
        project=project, name=f'parent nested fixture {source_field}',
        type=f'nested_parent_{source_field}', version='1',
        schema={'properties': {'forwarded_value': {'type': 'object'}}},
        steps=[_reference(child, **{source_field: {'account_data': '{{params.forwarded_value}}'}})],
        created_by=owner,
    )
    private_value = 'synthetic-private-transitive-nested'
    flow = [_reference(parent, params={'forwarded_value': {
        'username': 'synthetic-user', 'password': private_value,
    }})]

    response = _post_case(member, project, flow)

    assert response.status_code == 400
    assert private_value not in str(response.data)
    _runtime_rejects(project, member, flow)


@pytest.mark.django_db
def test_nested_secret_forwarding_preserves_safe_object_fields(nested_secret_context):
    owner, member, project, child = nested_secret_context
    parent = AppProjectComponent.objects.create(
        project=project, name='parent safe nested fixture', type='nested_parent_safe_fixture', version='1',
        schema={'properties': {'forwarded_value': {'type': 'object'}}},
        steps=[_reference(child, params={'account_data': '{{params.forwarded_value}}'})],
        created_by=owner,
    )
    flow = [_reference(parent, params={'forwarded_value': {
        'username': 'synthetic-user', 'password': '${global.account_password}',
    }})]

    response = _post_case(member, project, flow)

    assert response.status_code == 201, response.data
    case = AppTestCase.objects.get(pk=response.data['id'])
    execution = AppTestExecution.objects.create(test_case=case, user=member)
    expanded = expand_project_flow(execution, case, flow)
    assert expanded[0]['config']['value'] == {
        'username': 'synthetic-user', 'password': '${global.account_password}',
    }
